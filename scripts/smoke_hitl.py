"""HITL 端到端冒烟（不需要真实 LLM，路径完全由 amount + intent 决定）。

三场景：
1. 大额 transfer (6000 万) → treasury_manager → interrupt
2. 同 thread 用 Command(resume={"approved": True, ...}) 恢复 → approval_status=approved
3. 新 thread 跑同流程，用 resume({"approved": False, ...}) → approval_status=rejected
"""
from __future__ import annotations

import json
import sys
import uuid
from decimal import Decimal

from langgraph.types import Command

from app.graph import build_graph


def _print_interrupt(out: dict) -> dict | None:
    raw = out.get("__interrupt__")
    if not raw:
        return None
    first = list(raw)[0] if isinstance(raw, (list, tuple)) else raw
    payload = getattr(first, "value", first)
    print("  [interrupt payload]")
    print("  " + json.dumps(payload, ensure_ascii=False, indent=2, default=str).replace("\n", "\n  "))
    return payload


def scenario_approve(graph) -> bool:
    print("\n--- 场景 A: 大额 transfer → HITL → 批准 ---")
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    state = {
        "user_role": "treasury_manager",
        "current_task": "transfer",
        "amount": Decimal("60000000"),
        "entity_code": "ENTITY-001",
        "currency": "CNY",
        "counterparty": "ACC-工行-001",
    }
    out = graph.invoke(state, config=config)
    payload = _print_interrupt(out)
    if not payload:
        print(f"  [FAIL] 未触发中断，final={out.get('final_output')!r}")
        return False
    print(f"  [thread_id] {thread_id}")

    resumed = graph.invoke(
        Command(resume={"approved": True, "instruction_id": "APR-2026-0001"}),
        config=config,
    )
    print(f"  [resume final] {resumed.get('final_output')!r}")
    print(f"  [approval_status] {resumed.get('approval_status')!r}")
    print(f"  [approved_instruction_id] {resumed.get('approved_instruction_id')!r}")

    ok = (
        resumed.get("approval_status") == "approved"
        and resumed.get("approved_instruction_id") == "APR-2026-0001"
    )
    print(f"  [{'OK' if ok else 'FAIL'}]")
    return ok


def scenario_reject(graph) -> bool:
    print("\n--- 场景 B: 大额 transfer → HITL → 拒绝 ---")
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    state = {
        "user_role": "treasury_manager",
        "current_task": "transfer",
        "amount": Decimal("80000000"),
        "entity_code": "ENTITY-001",
        "currency": "CNY",
        "counterparty": "ACC-中行-002",
    }
    out = graph.invoke(state, config=config)
    payload = _print_interrupt(out)
    if not payload:
        print(f"  [FAIL] 未触发中断，final={out.get('final_output')!r}")
        return False
    print(f"  [thread_id] {thread_id}")

    resumed = graph.invoke(
        Command(resume={"approved": False, "reason": "超出本季度调拨额度"}),
        config=config,
    )
    print(f"  [resume final] {resumed.get('final_output')!r}")
    print(f"  [approval_status] {resumed.get('approval_status')!r}")
    print(f"  [current_role] {resumed.get('current_role')!r}")

    ok = resumed.get("approval_status") == "rejected" and resumed.get("current_role") == "rejected"
    print(f"  [{'OK' if ok else 'FAIL'}]")
    return ok


def scenario_no_hitl_small_transfer(graph) -> bool:
    print("\n--- 场景 C: 小额 transfer (500万) → treasury_supervisor → 直接 END ---")
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    state = {
        "user_role": "treasury_supervisor",
        "current_task": "transfer",
        "amount": Decimal("5000000"),
    }
    out = graph.invoke(state, config=config)
    if out.get("__interrupt__"):
        print("  [FAIL] 不应该触发中断")
        return False
    print(f"  [final] {out.get('final_output')!r}")
    print(f"  [current_role] {out.get('current_role')!r}")

    ok = out.get("current_role") == "treasury_supervisor"
    print(f"  [{'OK' if ok else 'FAIL'}]")
    return ok


def main() -> int:
    graph = build_graph()
    results = [
        scenario_approve(graph),
        scenario_reject(graph),
        scenario_no_hitl_small_transfer(graph),
    ]
    passed = sum(results)
    print(f"\n=== HITL 总计: {passed}/{len(results)} 通过 ===")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
