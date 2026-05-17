"""Knowledge Agent 端到端冒烟：BGE 双轨检索 + DeepSeek 合成 + [来源:] 标注。

跑之前确保：
1. 已 build_kb（qdrant_data 里有 industry / enterprise 两个 collection）
2. .env 配好 DeepSeek 凭证
3. Gradio 进程已停或不会撞 Qdrant 锁
"""
from __future__ import annotations

import sys

from langchain_core.messages import HumanMessage

from app.agents.nodes import knowledge_node
from app.graph.state import TreasuryState


def main() -> int:
    queries = [
        "跨境资金池的监管要求和我们公司的内部规定有什么差别？",
        "大额交易报告标准是什么？",
    ]

    for q in queries:
        print("=" * 60)
        print(f"Q: {q}")
        print("=" * 60)
        state: TreasuryState = {
            "messages": [HumanMessage(content=q)],
            "user_role": "treasury_manager",
        }
        out = knowledge_node(state)
        print(out.get("final_output", "(no output)"))
        print()
        print(f"  industry_context length: {len(out.get('industry_context', ''))}")
        print(f"  enterprise_context length: {len(out.get('enterprise_context', ''))}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
