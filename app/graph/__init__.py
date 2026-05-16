"""LangGraph 状态图构建入口。

build_graph() 返回带 MemorySaver checkpointer 的已编译图。
由于使用了 interrupt() 实现 HITL，所有 invoke 调用必须传入 thread_id config：
    graph.invoke(state, config={"configurable": {"thread_id": "..."}})

节点拓扑（对应 DESIGN.md §4.2.2 修订版状态图）：
    START → supervisor → (条件路由) → [cashier | treasury_supervisor | treasury_manager | knowledge | reject_unauthorized]
                                                                       │
                                                              (requires_approval?)
                                                                       │
                                                          是 ──→ hitl ──→ END
                                                           否 ──→ END
"""
from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.graph.routing import route_by_intent
from app.graph.state import TreasuryState

__all__ = ["TreasuryState", "build_graph", "route_by_intent"]


def _maybe_hitl(state: TreasuryState) -> str:
    """treasury_manager 之后的条件边：requires_approval=True 走 HITL，否则结束。"""
    return "hitl" if state.get("requires_approval") else END


def build_graph():
    """编译资金 Agent 状态图（带 MemorySaver）。返回 CompiledGraph。

    Agent 节点延迟导入：app.agents.nodes 依赖 app.graph.state（TreasuryState），
    若顶层 import 会触发循环。
    """
    from app.agents import (
        cashier_node,
        hitl_node,
        knowledge_node,
        reject_unauthorized_node,
        supervisor_node,
        treasury_manager_node,
        treasury_supervisor_node,
    )

    g = StateGraph(TreasuryState)

    g.add_node("supervisor", supervisor_node)
    g.add_node("cashier", cashier_node)
    g.add_node("treasury_supervisor", treasury_supervisor_node)
    g.add_node("treasury_manager", treasury_manager_node)
    g.add_node("knowledge", knowledge_node)
    g.add_node("hitl", hitl_node)
    g.add_node("reject_unauthorized", reject_unauthorized_node)

    g.add_edge(START, "supervisor")
    g.add_conditional_edges(
        "supervisor",
        route_by_intent,
        {
            "cashier": "cashier",
            "treasury_supervisor": "treasury_supervisor",
            "treasury_manager": "treasury_manager",
            "knowledge": "knowledge",
            "reject_unauthorized": "reject_unauthorized",
        },
    )

    # 资金经理之后：requires_approval 决定是否进 HITL
    g.add_conditional_edges("treasury_manager", _maybe_hitl, {"hitl": "hitl", END: END})

    # 其他节点直接 END（HITL 自身也直接 END）
    for terminal in ("cashier", "treasury_supervisor", "knowledge", "reject_unauthorized", "hitl"):
        g.add_edge(terminal, END)

    return g.compile(checkpointer=MemorySaver())
