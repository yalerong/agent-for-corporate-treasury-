"""HITL（人工审批）横切节点测试。

覆盖：
- 哪些意图会触发 HITL（fx / aml / investment / 大额 transfer）
- 哪些不会（inquiry / 小额 transfer / 出纳已审批 transfer）
- approve / reject 双路径 resume 后状态正确
"""
from __future__ import annotations

from decimal import Decimal

from langgraph.types import Command

from app.graph import build_graph


def _interrupts(out: dict) -> list:
    raw = out.get("__interrupt__")
    if not raw:
        return []
    return list(raw) if isinstance(raw, (list, tuple)) else [raw]


class TestHITLTrigger:
    """验证只有特定意图触发 HITL 中断。"""

    def test_inquiry_does_not_trigger(self, thread_config):
        graph = build_graph()
        out = graph.invoke(
            {"user_role": "cashier", "current_task": "inquiry"}, config=thread_config
        )
        assert _interrupts(out) == []
        assert out["current_role"] == "cashier"

    def test_small_transfer_does_not_trigger(self, thread_config):
        graph = build_graph()
        out = graph.invoke(
            {
                "user_role": "treasury_supervisor",
                "current_task": "transfer",
                "amount": Decimal("3000000"),
            },
            config=thread_config,
        )
        assert _interrupts(out) == []
        assert out["current_role"] == "treasury_supervisor"

    def test_fx_triggers(self, thread_config):
        graph = build_graph()
        out = graph.invoke(
            {"user_role": "treasury_manager", "current_task": "fx"}, config=thread_config
        )
        ints = _interrupts(out)
        assert len(ints) == 1
        # 经理节点已运行，requires_approval 已置位
        assert out.get("requires_approval") is True

    def test_aml_triggers(self, thread_config):
        graph = build_graph()
        out = graph.invoke(
            {"user_role": "treasury_manager", "current_task": "aml"}, config=thread_config
        )
        assert len(_interrupts(out)) == 1

    def test_investment_triggers(self, thread_config):
        graph = build_graph()
        out = graph.invoke(
            {"user_role": "treasury_manager", "current_task": "investment"},
            config=thread_config,
        )
        assert len(_interrupts(out)) == 1

    def test_large_transfer_triggers(self, thread_config):
        graph = build_graph()
        out = graph.invoke(
            {
                "user_role": "treasury_supervisor",
                "current_task": "transfer",
                "amount": Decimal("10000000"),
            },
            config=thread_config,
        )
        assert len(_interrupts(out)) == 1

    def test_interrupt_payload_contains_task_info(self, thread_config):
        graph = build_graph()
        out = graph.invoke(
            {
                "user_role": "treasury_manager",
                "current_task": "fx",
                "amount": Decimal("1000000"),
                "currency": "USD",
            },
            config=thread_config,
        )
        ints = _interrupts(out)
        payload = ints[0].value if hasattr(ints[0], "value") else ints[0]
        assert payload["kind"] == "approval_request"
        assert payload["task"] == "fx"
        assert payload["currency"] == "USD"


class TestHITLResume:
    """验证 Command(resume=...) 能正确恢复执行并设置审批状态。"""

    def test_approve_resume(self, thread_config):
        graph = build_graph()
        graph.invoke(
            {"user_role": "treasury_manager", "current_task": "fx"}, config=thread_config
        )
        final = graph.invoke(
            Command(resume={"approved": True, "instruction_id": "APR-2026-0001"}),
            config=thread_config,
        )
        assert final["approval_status"] == "approved"
        assert final["approved_instruction_id"] == "APR-2026-0001"
        assert "APR-2026-0001" in final["final_output"]

    def test_reject_resume(self, thread_config):
        graph = build_graph()
        graph.invoke(
            {"user_role": "treasury_manager", "current_task": "fx"}, config=thread_config
        )
        final = graph.invoke(
            Command(resume={"approved": False, "reason": "金额超出本月额度"}),
            config=thread_config,
        )
        assert final["approval_status"] == "rejected"
        assert final["current_role"] == "rejected"
        assert "金额超出本月额度" in final["final_output"]

    def test_reject_without_reason_uses_default(self, thread_config):
        graph = build_graph()
        graph.invoke(
            {"user_role": "treasury_manager", "current_task": "aml"}, config=thread_config
        )
        final = graph.invoke(
            Command(resume={"approved": False}),
            config=thread_config,
        )
        assert final["approval_status"] == "rejected"
        assert "未提供原因" in final["final_output"]

    def test_approve_large_transfer(self, thread_config):
        graph = build_graph()
        graph.invoke(
            {
                "user_role": "treasury_supervisor",
                "current_task": "transfer",
                "amount": Decimal("10000000"),
            },
            config=thread_config,
        )
        final = graph.invoke(
            Command(resume={"approved": True, "instruction_id": "APR-2026-XFER-001"}),
            config=thread_config,
        )
        assert final["approval_status"] == "approved"
        assert final["approved_instruction_id"] == "APR-2026-XFER-001"
