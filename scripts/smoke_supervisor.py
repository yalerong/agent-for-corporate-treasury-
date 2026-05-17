"""Supervisor 意图分类冒烟。

直接调 app.agents.nodes._classify_intent，覆盖 5 类典型自然语言输入：
  - 余额查询 → inquiry
  - 外汇套保 → fx
  - 小额调拨 → transfer
  - 大额调拨 → transfer
  - 制度咨询 → knowledge

不走完整 graph，避免触发 knowledge_node 的真实 embedding 调用（Task #2 跳过状态）。
"""
from __future__ import annotations

import sys
from decimal import Decimal
from typing import cast

from langchain_core.messages import HumanMessage

from app.agents.nodes import _classify_intent, supervisor_node
from app.graph.routing import route_by_intent
from app.graph.state import TreasuryState, UserRoleStr

CASES = [
    ("查一下今天工行账户的余额", "inquiry"),
    ("帮我看一下近期人民币兑美元套保策略", "fx"),
    ("从基本户调拨 50 万到 ACC-工行-001", "transfer"),
    ("从基本户调拨 6000 万到 ACC-工行-001 用于并购", "transfer"),
    ("跨境资金池的监管要求是什么", "knowledge"),
    ("最近有一笔可疑交易需要核查", "aml"),
]


def main() -> int:
    print("=" * 60)
    print("PART 1: _classify_intent 直接测试")
    print("=" * 60)

    classify_results = []
    for query, expected in CASES:
        actual = _classify_intent(query)
        status = "OK " if actual == expected else "MISS"
        print(f"[{status}] expected={expected:<14} actual={actual:<14} | {query}")
        classify_results.append((query, expected, actual))

    classify_pass = sum(1 for _, e, a in classify_results if e == a)
    print(f"\n意图分类正确率: {classify_pass}/{len(CASES)}")

    print()
    print("=" * 60)
    print("PART 2: supervisor_node + route_by_intent 端到端")
    print("=" * 60)

    routing_cases = [
        # (query, user_role, amount, expected_route)
        ("查一下今天工行账户的余额", "cashier", None, "cashier"),
        ("从基本户调拨 50 万到 ACC-工行-001", "treasury_supervisor", Decimal("500000"), "treasury_supervisor"),
        ("从基本户调拨 6000 万到 ACC-工行-001 用于并购", "treasury_manager", Decimal("60000000"), "treasury_manager"),
        ("帮我看一下近期人民币兑美元套保策略", "treasury_manager", None, "treasury_manager"),
    ]

    route_pass = 0
    for query, role, amount, expected_route in routing_cases:
        state: TreasuryState = {
            "messages": [HumanMessage(content=query)],
            "user_role": cast(UserRoleStr, role),
        }
        if amount is not None:
            state["amount"] = amount

        sup_out = supervisor_node(state)
        merged: TreasuryState = {**state, **sup_out}
        actual_route = route_by_intent(merged)

        status = "OK " if actual_route == expected_route else "MISS"
        print(
            f"[{status}] role={role:<19} task={sup_out.get('current_task'):<10} "
            f"amount={amount} | route={actual_route} (expected {expected_route}) | {query}"
        )
        if actual_route == expected_route:
            route_pass += 1

    print(f"\n路由正确率: {route_pass}/{len(routing_cases)}")

    total_ok = classify_pass + route_pass
    total = len(CASES) + len(routing_cases)
    print(f"\n=== 总计: {total_ok}/{total} 通过 ===")
    return 0 if total_ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
