"""企业知识库热更新脚本。

按 DESIGN.md §4.1.3：企业库高频迭代，制度修订后立即重建，不影响行业库。

用法：
    python -m scripts.update_enterprise_kb           # 全量重建
    python -m scripts.update_enterprise_kb --check   # 仅清点文件，不重建（dry-run）

退出码：
    0 - 成功
    2 - 参数错误
"""
from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.rag.store import index_track, load_directory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="企业知识库热更新")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只清点企业库目录下的文档数量，不重建索引",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.check:
        docs = load_directory(settings.enterprise_kb_path, category="enterprise")
        print(f"[check] 企业库目录：{settings.enterprise_kb_path}")
        print(f"[check] 文档数：{len(docs)}")
        for d in docs:
            print(f"  - {d.metadata.get('source')}")
        return 0

    n = index_track("enterprise")
    print(f"[update] 企业库已重建，写入 {n} 个 chunk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
