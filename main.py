"""企业资金智能体 CLI 入口（Phase 1/2 早期单轮调用版）。

用法示例：
    python main.py --role cashier --task inquiry "查一下今天的余额"
    python main.py --role treasury_supervisor --task transfer --amount 3000000
    python main.py --role cashier --task transfer --approved-id APR-2026-0001

退出码：
    0 - 成功完成
    2 - 参数错误（argparse）
    3 - 路由拒绝（越权或缺少审批）
    4 - 触发人工审批（HITL 中断）；payload 与 thread_id 已打印，需通过 API 层调用
        graph.invoke(Command(resume=...), config={"configurable": {"thread_id": "..."}})
        恢复执行。CLI 当前不支持 resume，下一版可补 --resume / --approve 选项。
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from decimal import Decimal

from langchain_core.messages import HumanMessage

from app.config import Intent, UserRole
from app.graph import build_graph


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="treasury-agent",
        description="企业资金智能体 CLI（Phase 1/2 早期占位实现）",
    )
    parser.add_argument(
        "--role",
        required=True,
        choices=[r.value for r in UserRole],
        help="操作者角色",
    )
    parser.add_argument(
        "--task",
        required=True,
        choices=[i.value for i in Intent],
        help="任务意图",
    )
    parser.add_argument(
        "--amount",
        type=Decimal,
        default=None,
        help="涉及金额（CNY），transfer / fx 等场景需提供",
    )
    parser.add_argument(
        "--approved-id",
        dest="approved_id",
        default=None,
        help="已审批指令编号；出纳执行已审批 transfer 时必填",
    )
    parser.add_argument(
        "message",
        nargs="?",
        default="",
        help="自由文本消息（可选，Phase 1 节点暂不消费）",
    )
    return parser


def _extract_interrupts(out: dict) -> list:
    """从 invoke 返回值中提取 interrupt 列表。LangGraph 不同版本的位置有差异。"""
    raw = out.get("__interrupt__")
    if not raw:
        return []
    return list(raw) if isinstance(raw, (list, tuple)) else [raw]


def run(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    state: dict = {"user_role": args.role, "current_task": args.task}
    if args.amount is not None:
        state["amount"] = args.amount
    if args.approved_id is not None:
        state["approved_instruction_id"] = args.approved_id
    if args.message:
        state["messages"] = [HumanMessage(content=args.message)]

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    graph = build_graph()
    out = graph.invoke(state, config=config)

    # HITL 中断：打印 payload + thread_id，退出码 4
    interrupts = _extract_interrupts(out)
    if interrupts:
        first = interrupts[0]
        payload = getattr(first, "value", first)
        print("[等待人工审批]")
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        print(f"[Thread ID] {thread_id}")
        return 4

    print(out.get("final_output", "(no output)"))
    return 3 if out.get("current_role") == "rejected" else 0


if __name__ == "__main__":
    sys.exit(run())
