"""Gradio 本地网页界面。

启动：
    python -m app.web
然后浏览器访问 http://127.0.0.1:7860

功能：
- 多轮聊天，自动用 Supervisor LLM 把自然语言分类到 Intent
- 同会话保持 thread_id，HITL 中断/恢复在 UI 内闭环
- HITL 触发时弹出审批面板（批准/拒绝 + 指令编号 / 拒绝原因）
- "knowledge" 意图走双轨检索：从 ./qdrant_data 拉行业法规 + 企业制度，由 LLM 合成带 [来源:] 标注的答案

不依赖 FastAPI 层（直接调 graph 单例）。生产部署用 app.api。
"""
from __future__ import annotations

import re
import uuid
from decimal import Decimal

import gradio as gr
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from app.agents.nodes import supervisor_node
from app.config import UserRole
from app.graph import build_graph

_graph = None

_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(亿|千万|百万|万)")
_UNIT_MULTIPLIER = {
    "亿": Decimal("100000000"),
    "千万": Decimal("10000000"),
    "百万": Decimal("1000000"),
    "万": Decimal("10000"),
}


def _get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def _extract_amount(text: str) -> Decimal | None:
    """从中文消息抽金额。只接受带单位的写法（避免误抓 ID 里的数字）。"""
    m = _AMOUNT_RE.search(text)
    if not m:
        return None
    return Decimal(m.group(1)) * _UNIT_MULTIPLIER[m.group(2)]


def _hide_panel():
    return gr.update(visible=False), gr.update(value=""), gr.update(value="")


def _show_panel():
    return gr.update(visible=True), gr.update(value=""), gr.update(value="")


def _format_interrupt(payload: dict) -> str:
    lines = ["⚠️ **触发人工审批**", ""]
    for k, label in [
        ("task", "任务"),
        ("amount", "金额"),
        ("entity_code", "实体"),
        ("currency", "币种"),
        ("counterparty", "对手方"),
    ]:
        v = payload.get(k)
        if v:
            lines.append(f"- **{label}**: {v}")
    lines.append("")
    lines.append("👇 请用下方按钮决策。")
    return "\n".join(lines)


def respond(message, history, role, state):
    """主聊天回调。"""
    message = (message or "").strip()
    if not message:
        return history, state, *_hide_panel()

    thread_id = state.get("thread_id") or str(uuid.uuid4())
    state["thread_id"] = thread_id

    if state.get("pending_interrupt"):
        history = history + [
            {"role": "user", "content": message},
            {
                "role": "assistant",
                "content": "⚠️ 当前有待审批的请求，请先点击下方的 **批准** 或 **拒绝** 按钮。",
            },
        ]
        return history, state, *_show_panel()

    # 用 supervisor_node 做意图分类（复用 Phase 2 逻辑）
    sup_state = {"messages": [HumanMessage(content=message)], "user_role": role}
    try:
        sup_out = supervisor_node(sup_state)
    except Exception as e:
        history = history + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": f"❌ LLM 调用失败：`{type(e).__name__}: {e}`"},
        ]
        return history, state, *_hide_panel()

    intent = sup_out.get("current_task") or "knowledge"

    g_state = {
        "user_role": role,
        "current_task": intent,
        "messages": [HumanMessage(content=message)],
    }
    amount = _extract_amount(message)
    if amount is not None:
        g_state["amount"] = amount

    config = {"configurable": {"thread_id": thread_id}}
    try:
        out = _get_graph().invoke(g_state, config=config)
    except Exception as e:
        history = history + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": f"❌ Agent 执行失败：`{type(e).__name__}: {e}`"},
        ]
        return history, state, *_hide_panel()

    raw = out.get("__interrupt__")
    if raw:
        first = list(raw)[0] if isinstance(raw, (list, tuple)) else raw
        payload = getattr(first, "value", first)
        state["pending_interrupt"] = payload
        history = history + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": _format_interrupt(payload)},
        ]
        return history, state, *_show_panel()

    final = out.get("final_output") or "(no output)"
    label = f"_意图: **{intent}**_"
    if amount is not None:
        label += f"  ·  _金额抽取: {amount:,}_"
    history = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": f"{label}\n\n{final}"},
    ]
    return history, state, *_hide_panel()


def approve(history, state, instruction_id):
    if not state.get("pending_interrupt"):
        return history, state, *_hide_panel()

    config = {"configurable": {"thread_id": state["thread_id"]}}
    iid = (instruction_id or "").strip() or f"APR-{uuid.uuid4().hex[:8].upper()}"
    payload = {"approved": True, "instruction_id": iid}

    try:
        out = _get_graph().invoke(Command(resume=payload), config=config)
    except Exception as e:
        history = history + [
            {"role": "assistant", "content": f"❌ 审批恢复失败：`{type(e).__name__}: {e}`"},
        ]
        state["pending_interrupt"] = None
        return history, state, *_hide_panel()

    final = out.get("final_output") or "(no output)"
    history = history + [{"role": "assistant", "content": f"✅ {final}"}]
    state["pending_interrupt"] = None
    return history, state, *_hide_panel()


def reject(history, state, reason):
    if not state.get("pending_interrupt"):
        return history, state, *_hide_panel()

    config = {"configurable": {"thread_id": state["thread_id"]}}
    reason = (reason or "").strip() or "未提供原因"
    payload = {"approved": False, "reason": reason}

    try:
        out = _get_graph().invoke(Command(resume=payload), config=config)
    except Exception as e:
        history = history + [
            {"role": "assistant", "content": f"❌ 审批恢复失败：`{type(e).__name__}: {e}`"},
        ]
        state["pending_interrupt"] = None
        return history, state, *_hide_panel()

    final = out.get("final_output") or "(no output)"
    history = history + [{"role": "assistant", "content": f"🚫 {final}"}]
    state["pending_interrupt"] = None
    return history, state, *_hide_panel()


def new_session(state):
    state["thread_id"] = str(uuid.uuid4())
    state["pending_interrupt"] = None
    return [], state, *_hide_panel()


def _clear_input():
    return ""


def build_demo() -> gr.Blocks:
    with gr.Blocks(title="资金 Agent (本地)") as demo:
        gr.Markdown(
            "# 💼 企业资金智能体 (本地)\n"
            "Phase 2 完整版 · DeepSeek v4-flash · MemorySaver checkpointer · HITL 大额审批"
        )

        state = gr.State({"thread_id": None, "pending_interrupt": None})

        with gr.Row():
            role = gr.Radio(
                choices=[r.value for r in UserRole],
                value="treasury_manager",
                label="操作角色",
                scale=4,
            )
            new_btn = gr.Button("🔄 新会话", scale=1)

        chatbot = gr.Chatbot(
            height=500,
            label="对话",
        )

        with gr.Row():
            msg = gr.Textbox(
                placeholder="例：查工行余额 / 调拨 50 万到 ACC-工行-001 / 调拨 6000 万到 ACC-工行-001",
                show_label=False,
                scale=5,
            )
            send_btn = gr.Button("发送", variant="primary", scale=1)

        with gr.Group(visible=False) as panel:
            gr.Markdown("### 🛡️ 人工审批面板")
            with gr.Row():
                approval_id = gr.Textbox(
                    label="指令编号（批准时填，留空自动生成）",
                    placeholder="APR-2026-0001",
                )
                approval_reason = gr.Textbox(
                    label="拒绝原因（拒绝时填）",
                    placeholder="超出额度",
                )
            with gr.Row():
                approve_btn = gr.Button("✅ 批准", variant="primary")
                reject_btn = gr.Button("❌ 拒绝", variant="stop")

        gr.Markdown(
            "💡 **小贴士**\n"
            "- 角色限制了你能下达的意图（出纳只能查询/执行；经理可下决策）\n"
            "- 调拨 > 5000 万自动触发审批；外汇/AML/投资意图也走审批\n"
            "- '新会话' 重置 thread_id 和审批状态\n"
            "- 知识库咨询：行业法规 + 企业制度双轨检索（BGE-large-zh）"
        )

        outputs = [chatbot, state, panel, approval_id, approval_reason]

        msg.submit(respond, [msg, chatbot, role, state], outputs).then(
            _clear_input, None, msg
        )
        send_btn.click(respond, [msg, chatbot, role, state], outputs).then(
            _clear_input, None, msg
        )
        approve_btn.click(approve, [chatbot, state, approval_id], outputs)
        reject_btn.click(reject, [chatbot, state, approval_reason], outputs)
        new_btn.click(new_session, [state], outputs)

    return demo


if __name__ == "__main__":
    demo = build_demo()
    demo.launch(
        server_name="127.0.0.1",
        server_port=7860,
        share=False,
        inbrowser=True,
        show_error=True,
        theme=gr.themes.Soft(),
    )
