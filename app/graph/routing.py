"""Supervisor 的条件路由函数。

实现 DESIGN.md §4.2.3 中 route_by_intent 的修正版：
- 出纳的 transfer 受 approved_instruction_id 二次校验
- transfer 非大额走 treasury_supervisor（修复原版的路由缺口）
- 阈值从 app.config.Thresholds 读取，禁止硬编码
- 命名与 TreasuryState.current_role 枚举严格一致
"""
from __future__ import annotations

from decimal import Decimal

from app.config import Intent, Thresholds, UserRole, role_can
from app.graph.state import TreasuryState

# 路由可能的返回值。所有值必须在 graph builder 的 conditional_edges 映射中登记。
RouteTarget = str


def route_by_intent(state: TreasuryState) -> RouteTarget:
    """根据 current_task + user_role + amount 决定下一节点。

    返回值是 build_graph() 注册的节点名，或控制流标记 "reject_unauthorized"。
    """
    intent_str = state.get("current_task", "")
    user_str = state.get("user_role", "")
    amount = state.get("amount") or Decimal("0")

    # 任何非法枚举值都视为越权（防御性）
    try:
        intent = Intent(intent_str)
        user = UserRole(user_str)
    except ValueError:
        return "reject_unauthorized"

    if not role_can(user, intent):
        return "reject_unauthorized"

    # 出纳的 TRANSFER 必须凭已审批指令执行
    if intent == Intent.TRANSFER and user == UserRole.CASHIER:
        if not state.get("approved_instruction_id"):
            return "reject_unauthorized"
        return "cashier"

    # 调拨路由：大额走经理审批，小额由主管编排（头寸→合规→执行）
    if intent == Intent.TRANSFER:
        if amount > Thresholds.LARGE_TRANSFER:
            return "treasury_manager"
        return "treasury_supervisor"

    if intent in (Intent.FX, Intent.AML, Intent.INVESTMENT):
        return "treasury_manager"

    if intent in (Intent.PLANNING, Intent.POSITION, Intent.ACCOUNT):
        return "treasury_supervisor"

    if intent in (Intent.INQUIRY, Intent.RECONCILIATION):
        return "cashier"

    return "knowledge"
