"""Graph 与路由测试：route_by_intent 边界 + 端到端图执行。"""
from __future__ import annotations

from decimal import Decimal

from app.graph import build_graph
from app.graph.routing import route_by_intent


class TestRouteByIntent:
    """路由函数的单元测试。覆盖 DESIGN.md §4.2.3 的全部分支。"""

    def test_cashier_inquiry_routes_to_cashier(self):
        s = {"user_role": "cashier", "current_task": "inquiry"}
        assert route_by_intent(s) == "cashier"

    def test_cashier_reconciliation_routes_to_cashier(self):
        s = {"user_role": "cashier", "current_task": "reconciliation"}
        assert route_by_intent(s) == "cashier"

    def test_cashier_transfer_without_approval_rejected(self):
        s = {"user_role": "cashier", "current_task": "transfer"}
        assert route_by_intent(s) == "reject_unauthorized"

    def test_cashier_transfer_with_approval_allowed(self):
        s = {
            "user_role": "cashier",
            "current_task": "transfer",
            "approved_instruction_id": "APR-2026-0001",
        }
        assert route_by_intent(s) == "cashier"

    def test_cashier_cannot_fx(self):
        s = {"user_role": "cashier", "current_task": "fx"}
        assert route_by_intent(s) == "reject_unauthorized"

    def test_cashier_cannot_planning(self):
        s = {"user_role": "cashier", "current_task": "planning"}
        assert route_by_intent(s) == "reject_unauthorized"

    def test_supervisor_planning_routes_to_supervisor(self):
        s = {"user_role": "treasury_supervisor", "current_task": "planning"}
        assert route_by_intent(s) == "treasury_supervisor"

    def test_supervisor_cannot_fx(self):
        s = {"user_role": "treasury_supervisor", "current_task": "fx"}
        assert route_by_intent(s) == "reject_unauthorized"

    def test_small_transfer_routes_to_supervisor(self):
        s = {
            "user_role": "treasury_supervisor",
            "current_task": "transfer",
            "amount": Decimal("3000000"),
        }
        assert route_by_intent(s) == "treasury_supervisor"

    def test_large_transfer_routes_to_manager(self):
        s = {
            "user_role": "treasury_supervisor",
            "current_task": "transfer",
            "amount": Decimal("10000000"),
        }
        assert route_by_intent(s) == "treasury_manager"

    def test_transfer_threshold_boundary_inclusive(self):
        # 严格 > 5,000,000 才走经理；等于阈值仍由主管编排
        s = {
            "user_role": "treasury_supervisor",
            "current_task": "transfer",
            "amount": Decimal("5000000"),
        }
        assert route_by_intent(s) == "treasury_supervisor"

    def test_transfer_threshold_just_above(self):
        s = {
            "user_role": "treasury_supervisor",
            "current_task": "transfer",
            "amount": Decimal("5000000.01"),
        }
        assert route_by_intent(s) == "treasury_manager"

    def test_fx_routes_to_manager(self):
        s = {"user_role": "treasury_manager", "current_task": "fx"}
        assert route_by_intent(s) == "treasury_manager"

    def test_aml_routes_to_manager(self):
        s = {"user_role": "treasury_manager", "current_task": "aml"}
        assert route_by_intent(s) == "treasury_manager"

    def test_knowledge_intent_routes_to_knowledge(self):
        s = {"user_role": "cashier", "current_task": "knowledge"}
        assert route_by_intent(s) == "knowledge"

    def test_invalid_intent_rejected(self):
        s = {"user_role": "cashier", "current_task": "hack_the_system"}
        assert route_by_intent(s) == "reject_unauthorized"

    def test_invalid_role_rejected(self):
        s = {"user_role": "anonymous_user", "current_task": "inquiry"}
        assert route_by_intent(s) == "reject_unauthorized"

    def test_empty_state_rejected(self):
        assert route_by_intent({}) == "reject_unauthorized"


class TestGraphExecution:
    """端到端：构建图 + invoke + 检查 final_output / current_role。

    HITL-触发场景（fx/aml/investment/大额 transfer）的测试见 TestHITLRouting；
    本类只覆盖不触发 HITL 的常规路径。
    """

    def test_graph_compiles(self):
        graph = build_graph()
        assert graph is not None

    def test_inquiry_flows_through_cashier(self, thread_config):
        graph = build_graph()
        out = graph.invoke(
            {"user_role": "cashier", "current_task": "inquiry"}, config=thread_config
        )
        assert out["current_role"] == "cashier"
        assert "[出纳]" in out["final_output"]

    def test_planning_flows_through_treasury_supervisor(self, thread_config):
        graph = build_graph()
        out = graph.invoke(
            {"user_role": "treasury_supervisor", "current_task": "planning"},
            config=thread_config,
        )
        assert out["current_role"] == "treasury_supervisor"
        assert "[资金主管]" in out["final_output"]

    def test_knowledge_flows_through_knowledge_node(self, thread_config):
        graph = build_graph()
        out = graph.invoke(
            {"user_role": "cashier", "current_task": "knowledge"}, config=thread_config
        )
        assert out["current_role"] == "knowledge"
        assert "[知识库]" in out["final_output"]

    def test_unauthorized_fx_blocked(self, thread_config):
        graph = build_graph()
        out = graph.invoke({"user_role": "cashier", "current_task": "fx"}, config=thread_config)
        assert out["current_role"] == "rejected"
        assert "无权" in out["final_output"]

    def test_small_transfer_lands_on_supervisor(self, thread_config):
        graph = build_graph()
        out = graph.invoke(
            {
                "user_role": "treasury_supervisor",
                "current_task": "transfer",
                "amount": Decimal("3000000"),
            },
            config=thread_config,
        )
        assert out["current_role"] == "treasury_supervisor"

    def test_cashier_approved_transfer_lands_on_cashier(self, thread_config):
        graph = build_graph()
        out = graph.invoke(
            {
                "user_role": "cashier",
                "current_task": "transfer",
                "approved_instruction_id": "APR-2026-0001",
            },
            config=thread_config,
        )
        assert out["current_role"] == "cashier"
