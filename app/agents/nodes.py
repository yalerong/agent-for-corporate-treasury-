"""Agent 节点定义。

Phase 2 完整版：
- supervisor_node：当 current_task 未由调用方提供时，调用 LLM 做意图分类
- knowledge_node：调用双轨检索 Tool 拿上下文，LLM 合成带来源标注的回答
- treasury_manager_node：对高风险意图置 requires_approval 触发 HITL
- 其他节点（cashier / treasury_supervisor / hitl / reject_unauthorized）仍为占位
  或固定逻辑；Phase 3 接入真实业务时按需扩展

LLM 通过 `app.llm.get_chat_model()` 拿；测试 monkeypatch 该函数注入 FakeListChatModel。
"""
from __future__ import annotations

from decimal import Decimal

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import interrupt

import app.llm as llm_module
from app.config import Intent, Thresholds
from app.graph.state import TreasuryState
from app.tools.knowledge import search_enterprise_knowledge, search_industry_knowledge

VALID_INTENTS = [i.value for i in Intent]


def _last_human_message(state: TreasuryState) -> str | None:
    """从 state.messages 取最后一条 HumanMessage 的内容。"""
    msgs = state.get("messages") or []
    for m in reversed(msgs):
        if isinstance(m, HumanMessage):
            return m.content
    return None


def _classify_intent(query: str) -> str:
    """用 LLM 把用户自然语言分类到 Intent 枚举之一。"""
    prompt = f"""把下面这句资金业务请求分类到下列意图之一：
{", ".join(VALID_INTENTS)}

意图说明：
- inquiry: 余额、流水、账户状态查询
- reconciliation: 对账、核对
- transfer: 调拨、付款
- planning: 资金计划、预算
- position: 头寸查询、敞口
- account: 账户管理
- fx: 外汇、汇率、套保
- aml: 反洗钱、可疑交易
- investment: 投资、理财
- knowledge: 法规咨询、制度咨询

请求："{query}"

只回答意图名称（一个英文单词），不要其他解释。"""
    llm = llm_module.get_chat_model()
    response = llm.invoke(prompt)
    raw = response.content if hasattr(response, "content") else str(response)
    first_token = raw.strip().lower().split()[0] if raw.strip() else "knowledge"
    # 去掉可能的标点
    first_token = first_token.strip(".,\"'!?。，！？")
    return first_token if first_token in VALID_INTENTS else "knowledge"


def supervisor_node(state: TreasuryState) -> dict:
    """意图识别 + 权限校验入口。

    - 若 state.current_task 已由调用方提供（CLI --task），passthrough
    - 否则从最后一条 HumanMessage 提取查询，用 LLM 分类到 Intent
    - 都没有 → 默认 knowledge
    """
    if state.get("current_task"):
        return {"current_role": "supervisor"}

    query = _last_human_message(state)
    if not query:
        return {"current_role": "supervisor", "current_task": "knowledge"}

    intent = _classify_intent(query)
    return {"current_role": "supervisor", "current_task": intent}


def cashier_node(state: TreasuryState) -> dict:
    msg = AIMessage(content="[出纳] 收到请求，当前为占位实现。")
    return {"current_role": "cashier", "messages": [msg], "final_output": msg.content}


def treasury_supervisor_node(state: TreasuryState) -> dict:
    msg = AIMessage(content="[资金主管] 收到请求，当前为占位实现。")
    return {
        "current_role": "treasury_supervisor",
        "messages": [msg],
        "final_output": msg.content,
    }


def treasury_manager_node(state: TreasuryState) -> dict:
    """资金经理节点。对高风险意图置位 requires_approval，由下游条件边路由到 HITL。"""
    intent = state.get("current_task", "")
    amount = state.get("amount") or Decimal("0")

    requires_approval = intent in ("fx", "aml", "investment") or (
        intent == "transfer" and amount > Thresholds.LARGE_TRANSFER
    )

    msg = AIMessage(content="[资金经理] 收到请求，当前为占位实现。")
    return {
        "current_role": "treasury_manager",
        "messages": [msg],
        "final_output": msg.content,
        "requires_approval": requires_approval,
    }


def knowledge_node(state: TreasuryState) -> dict:
    """知识问答节点：调用双轨检索 Tool + LLM 合成带来源标注的回答。

    简化版：直接并行调两个 Tool，把上下文塞给 LLM。
    不使用 ReAct 循环，避免 token 浪费和死循环风险。
    """
    query = _last_human_message(state)
    if not query:
        msg = AIMessage(content="[知识库] 未收到查询内容。")
        return {"current_role": "knowledge", "messages": [msg], "final_output": msg.content}

    industry_context = search_industry_knowledge.invoke({"query": query, "k": 3})
    enterprise_context = search_enterprise_knowledge.invoke({"query": query, "k": 3})

    prompt = f"""你是企业资金合规专家。根据下面两类参考资料回答用户问题。

【行业法规参考】
{industry_context}

【企业内部制度参考】
{enterprise_context}

【用户问题】
{query}

要求：
1. 综合两类资料给出回答
2. 每条结论后用 [来源: source 路径] 标注出处
3. 如行业法规与企业制度冲突，以法规为准并明确指出冲突
4. 不要编造未在参考资料中出现的条款
"""
    llm = llm_module.get_chat_model()
    response = llm.invoke(prompt)
    answer = response.content if hasattr(response, "content") else str(response)

    msg = AIMessage(content=f"[知识库] {answer}")
    return {
        "current_role": "knowledge",
        "messages": [msg],
        "final_output": msg.content,
        "industry_context": industry_context,
        "enterprise_context": enterprise_context,
    }


def reject_unauthorized_node(state: TreasuryState) -> dict:
    msg = AIMessage(content="您无权执行此操作或缺少必要审批，请联系上级。")
    return {"current_role": "rejected", "messages": [msg], "final_output": msg.content}


def hitl_node(state: TreasuryState) -> dict:
    """人工审批横切节点。任何 Agent 置 requires_approval=True 后路由到此。

    通过 LangGraph interrupt() 暂停执行，等待外部 Command(resume=payload) 提供决策。
    Resume payload 形如：
        {"approved": True, "instruction_id": "APR-2026-..."}
        {"approved": False, "reason": "金额超出权限"}
    """
    decision = interrupt(
        {
            "kind": "approval_request",
            "task": state.get("current_task"),
            "amount": str(state.get("amount")) if state.get("amount") is not None else None,
            "entity_code": state.get("entity_code"),
            "currency": state.get("currency"),
            "counterparty": state.get("counterparty"),
        }
    )

    if decision and decision.get("approved"):
        instruction_id = decision.get("instruction_id") or ""
        msg = AIMessage(content=f"[HITL] 已批准，指令编号 {instruction_id}。")
        return {
            "approval_status": "approved",
            "approved_instruction_id": instruction_id,
            "messages": [msg],
            "final_output": msg.content,
        }

    reason = (decision or {}).get("reason", "未提供原因")
    msg = AIMessage(content=f"[HITL] 已拒绝：{reason}")
    return {
        "approval_status": "rejected",
        "current_role": "rejected",
        "messages": [msg],
        "final_output": msg.content,
    }
