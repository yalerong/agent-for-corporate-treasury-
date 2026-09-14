"""FastAPI 主入口（Phase 2 接入版）。

按 DESIGN.md §8.2 暴露端点：
- POST /api/v1/chat              通用对话，自动路由到对应 Agent
- POST /api/v1/approvals/{tid}   HITL 人工确认，恢复中断的 thread
- GET  /api/v1/knowledge         双轨知识库直查（绕过 Agent，给 UI 调试用）
- GET  /api/v1/audit/logs        审计日志查询（仅 admin Bearer token 可调）
- GET  /healthz                  健康检查

启动方式：
    uvicorn app.api:app --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.auth import role_for_bearer_token
from app.config import Intent, UserRole, get_settings
from app.graph import build_graph

app = FastAPI(
    title="Treasury Agent API",
    version="0.1.0",
    description="企业资金智能体对外接口（Phase 2）",
)

_bearer = HTTPBearer(auto_error=False)
_token_auth = Security(_bearer)

# 图实例必须在进程内单例：每次 build_graph() 会新建 MemorySaver，
# 否则 HITL 暂停的 thread 在下次请求里找不到。
_graph_singleton = None
_pending_approvals: set[str] = set()


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


def current_user_role(
    credentials: HTTPAuthorizationCredentials | None = _token_auth,
) -> UserRole:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    role = role_for_bearer_token(credentials.credentials, get_settings())
    if role is None:
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    return role


def require_role(*allowed: UserRole):
    role_dep = Depends(current_user_role)

    def dependency(role: UserRole = role_dep) -> UserRole:
        if role not in allowed:
            raise HTTPException(status_code=403, detail="insufficient role")
        return role

    return dependency


_current_role = Depends(current_user_role)
_approver_role = Depends(require_role(
    UserRole.TREASURY_SUPERVISOR,
    UserRole.TREASURY_MANAGER,
    UserRole.ADMIN,
))
_admin_role = Depends(require_role(UserRole.ADMIN))


# ── /chat ──────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    message: str = Field(description="自然语言请求")
    task: str | None = Field(None, description="可选：admin 显式指定 Intent，跳过 LLM 意图分类")
    thread_id: str | None = Field(None, description="可选：恢复指定 thread；不传则新建")
    role: str | None = Field(None, description="已废弃：服务端忽略客户端自报角色")


class ChatResponse(BaseModel):
    thread_id: str
    status: Literal["completed", "interrupted", "rejected"]
    final_output: str | None = None
    current_role: str | None = None
    interrupt_payload: dict[str, Any] | None = None


@app.post("/api/v1/chat", response_model=ChatResponse)
def chat(req: ChatRequest, role: UserRole = _current_role) -> ChatResponse:
    if req.task and req.task not in {i.value for i in Intent}:
        raise HTTPException(400, detail=f"invalid task: {req.task}")
    if req.task and role != UserRole.ADMIN:
        raise HTTPException(403, detail="task override requires admin role")

    state: dict = {"user_role": role.value, "messages": [HumanMessage(content=req.message)]}
    if req.task:
        state["current_task"] = req.task

    tid = req.thread_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": tid}}

    out = _graph().invoke(state, config=config)

    interrupts = _extract_interrupts(out)
    if interrupts:
        first = interrupts[0]
        payload = getattr(first, "value", first)
        _pending_approvals.add(tid)
        return ChatResponse(
            thread_id=tid, status="interrupted", interrupt_payload=payload
        )

    _pending_approvals.discard(tid)
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


@app.post("/api/v1/approvals/{thread_id}", response_model=ChatResponse)
def approve(
    thread_id: str,
    req: ApprovalRequest,
    _role: UserRole = _approver_role,
) -> ChatResponse:
    if thread_id not in _pending_approvals:
        raise HTTPException(status_code=404, detail="thread not found or not waiting for approval")
    if req.approved and not req.instruction_id:
        raise HTTPException(status_code=400, detail="approved decision requires instruction_id")
    if not req.approved and not req.reason:
        raise HTTPException(status_code=400, detail="rejected decision requires reason")

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
    _pending_approvals.discard(thread_id)
    return ChatResponse(
        thread_id=thread_id,
        status=status,
        final_output=out.get("final_output"),
        current_role=out.get("current_role"),
    )


# ── /knowledge ─────────────────────────────────────────────────


@app.get("/api/v1/knowledge")
def knowledge(
    q: str = Query(description="查询文本"),
    target: Literal["industry", "enterprise", "both"] = "both",
    k: int = Query(default=4, ge=1, le=20),
    _role: UserRole = _current_role,
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


@app.get("/api/v1/audit/logs")
def audit_logs(
    limit: int = Query(default=100, ge=1, le=10000),
    tool: str | None = Query(default=None, description="按 Tool 名筛选"),
    _role: UserRole = _admin_role,
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
