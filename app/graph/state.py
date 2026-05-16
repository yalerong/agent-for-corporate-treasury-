"""LangGraph 状态定义。

任何 Agent 节点的输入和输出都必须符合 TreasuryState 形状。
节点间不可直接调用，只能通过 state 字段传值。
字段命名与 app.config.UserRole / Intent 严格对齐，重命名必须同步两处。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal, Optional, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

CurrentRole = Literal[
    "supervisor",
    "cashier",
    "treasury_supervisor",
    "treasury_manager",
    "knowledge",
    "rejected",
]
UserRoleStr = Literal["cashier", "treasury_supervisor", "treasury_manager", "admin"]
ApprovalStatus = Literal["none", "pending", "approved", "rejected"]


class TreasuryState(TypedDict, total=False):
    # 对话与任务
    messages: Annotated[list[BaseMessage], add_messages]
    current_task: str

    # 路由与权限
    current_role: CurrentRole
    user_role: UserRoleStr
    requires_approval: bool
    approval_status: ApprovalStatus
    approved_instruction_id: Optional[str]

    # 业务数据
    entity_code: Optional[str]
    amount: Optional[Decimal]
    currency: Optional[str]
    counterparty: Optional[str]

    # 知识检索
    industry_context: str
    enterprise_context: str

    # 执行与输出
    tool_calls: list[dict[str, Any]]
    execution_result: dict[str, Any]
    final_output: str
