"""知识库初始化 / 重建脚本。

用法：
    python -m app.rag.knowledge_base
    python -m app.rag.knowledge_base --industry-only
    python -m app.rag.knowledge_base --enterprise-only

行业库低频更新，每次执行会读取 knowledge_base/industry/ 下全部 .md / .txt 重建索引；
企业库高频更新建议改用 scripts/update_enterprise_kb.py 做增量热更新（Phase 2 实现）。
"""
from __future__ import annotations

import argparse
import sys

from app.rag.store import index_track


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="初始化双轨知识库")
    parser.add_argument("--industry-only", action="store_true", help="只索引行业库")
    parser.add_argument("--enterprise-only", action="store_true", help="只索引企业库")
    args = parser.parse_args(argv)

    if args.industry_only and args.enterprise_only:
        print("error: --industry-only 与 --enterprise-only 互斥", file=sys.stderr)
        return 2

    if not args.enterprise_only:
        n = index_track("industry")
        print(f"[industry] indexed {n} chunks")
    if not args.industry_only:
        n = index_track("enterprise")
        print(f"[enterprise] indexed {n} chunks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
