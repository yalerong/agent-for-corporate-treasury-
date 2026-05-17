"""FastAPI 主入口（Phase 2 接入版）。

按 DESIGN.md §8.2 暴露端点：
- POST /api/v1/chat              通用对话，自动路由到对应 Agent
- POST /api/v1/approvals/{tid}   HITL 人工确认，恢复中断的 thread
- GET  /api/v1/knowledge         双轨知识库直查（绕过 Agent，给 UI 调试用）
- GET  /api/v1/audit/logs        审计日志查询（仅 admin 可调，当前未做鉴权）
- GET  /healthz                  健康检查

启动方式：
    uvicorn app.api:app --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from hmac import compare_digest
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.config import Intent, UserRole, get_settings
from app.graph import build_graph

app = FastAPI(
    title="Treasury Agent API",
    version="0.1.0",
    description="企业资金智能体对外接口（Phase 2 早期，未做鉴权）",
)

# 图实例必须在进程内单例：每次 build_graph() 会新建 MemorySaver，
# 否则 HITL 暂停的 thread 在下次请求里找不到。
_graph_singleton = None


def _graph():
    global _graph_singleton
    if _graph_singleton is None:
        _graph_singleton = build_graph()
    return _graph_singleton


def _extract_interrupts(out: dict) -> list:
    raw = out.get("__interrupt__")
    if not raw:
        return []
    return list(raw) if isinstance(raw, (list, tuple)) else [raw]


def _require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    expected = get_settings().api_auth_token.get_secret_value()
    if not expected:
        raise HTTPException(503, detail="API_AUTH_TOKEN is not configured")
    if x_api_key is None or not compare_digest(x_api_key, expected):
        raise HTTPException(401, detail="invalid API key")


# ── /chat ──────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    role: str = Field(description="操作者角色，对应 UserRole 枚举")
    message: str = Field(description="自然语言请求")
    task: str | None = Field(None, description="可选：显式指定 Intent，跳过 LLM 意图分类")
    thread_id: str | None = Field(None, description="可选：恢复指定 thread；不传则新建")
    amount: Decimal | None = Field(None, description="涉及金额，CNY")
    approved_instruction_id: str | None = Field(None, description="已审批指令编号")
    entity_code: str | None = None
    currency: str | None = None
    counterparty: str | None = None


class ChatResponse(BaseModel):
    thread_id: str
    status: Literal["completed", "interrupted", "rejected"]
    final_output: str | None = None
    current_role: str | None = None
    interrupt_payload: dict[str, Any] | None = None


@app.post("/api/v1/chat", response_model=ChatResponse, dependencies=[Depends(_require_api_key)])
def chat(req: ChatRequest) -> ChatResponse:
    if req.role not in {r.value for r in UserRole}:
        raise HTTPException(400, detail=f"invalid role: {req.role}")
    if req.task and req.task not in {i.value for i in Intent}:
        raise HTTPException(400, detail=f"invalid task: {req.task}")

    state: dict = {"user_role": req.role, "messages": [HumanMessage(content=req.message)]}
    if req.task:
        state["current_task"] = req.task
    for field in (
        "amount",
        "approved_instruction_id",
        "entity_code",
        "currency",
        "counterparty",
    ):
        value = getattr(req, field)
        if value is not None:
            state[field] = value

    tid = req.thread_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": tid}}

    out = _graph().invoke(state, config=config)

    interrupts = _extract_interrupts(out)
    if interrupts:
        first = interrupts[0]
        payload = getattr(first, "value", first)
        return ChatResponse(
            thread_id=tid, status="interrupted", interrupt_payload=payload
        )

    if out.get("current_role") == "rejected":
        return ChatResponse(
            thread_id=tid,
            status="rejected",
            final_output=out.get("final_output"),
            current_role="rejected",
        )

    return ChatResponse(
        thread_id=tid,
        status="completed",
        final_output=out.get("final_output"),
        current_role=out.get("current_role"),
    )


# ── /approvals/{thread_id} ─────────────────────────────────────


class ApprovalRequest(BaseModel):
    approved: bool
    instruction_id: str | None = None
    reason: str | None = None


@app.post(
    "/api/v1/approvals/{thread_id}",
    response_model=ChatResponse,
    dependencies=[Depends(_require_api_key)],
)
def approve(thread_id: str, req: ApprovalRequest) -> ChatResponse:
    config = {"configurable": {"thread_id": thread_id}}
    payload: dict[str, Any] = {"approved": req.approved}
    if req.instruction_id:
        payload["instruction_id"] = req.instruction_id
    if req.reason:
        payload["reason"] = req.reason

    try:
        out = _graph().invoke(Command(resume=payload), config=config)
    except Exception as e:
        raise HTTPException(404, detail=f"thread not found or not interrupted: {e}") from e

    status: Literal["completed", "rejected"] = (
        "rejected" if out.get("current_role") == "rejected" else "completed"
    )
    return ChatResponse(
        thread_id=thread_id,
        status=status,
        final_output=out.get("final_output"),
        current_role=out.get("current_role"),
    )


# ── /knowledge ─────────────────────────────────────────────────


@app.get("/api/v1/knowledge", dependencies=[Depends(_require_api_key)])
def knowledge(
    q: str = Query(description="查询文本"),
    target: Literal["industry", "enterprise", "both"] = "both",
    k: int = Query(default=4, ge=1, le=20),
) -> dict[str, list[dict[str, str]]]:
    from app.tools.knowledge import _store  # internal singleton

    result: dict[str, list[dict[str, str]]] = {}
    if target in ("industry", "both"):
        result["industry"] = [
            {"source": d.metadata.get("source", "unknown"), "content": d.page_content}
            for d in _store("industry").similarity_search(q, k=k)
        ]
    if target in ("enterprise", "both"):
        result["enterprise"] = [
            {"source": d.metadata.get("source", "unknown"), "content": d.page_content}
            for d in _store("enterprise").similarity_search(q, k=k)
        ]
    return result


# ── /audit/logs ────────────────────────────────────────────────


@app.get("/api/v1/audit/logs", dependencies=[Depends(_require_api_key)])
def audit_logs(
    limit: int = Query(default=100, ge=1, le=10000),
    tool: str | None = Query(default=None, description="按 Tool 名筛选"),
) -> list[dict[str, Any]]:
    path = Path(get_settings().audit_log_path)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    records: list[dict[str, Any]] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if tool and rec.get("tool") != tool:
            continue
        records.append(rec)
        if len(records) >= limit:
            break
    return records


# ── /healthz ───────────────────────────────────────────────────


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
