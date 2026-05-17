"""双轨知识库一次性索引脚本。

用法：
    python -m scripts.build_kb              # 索引 industry + enterprise
    python -m scripts.build_kb --track industry   # 仅索引行业库
    python -m scripts.build_kb --track enterprise # 仅索引企业库
    python -m scripts.build_kb --check      # dry-run 只清点文档

首次运行会触发 BGE 模型下载（约 1.2GB 到 ~/.cache/huggingface/）。
重复运行会先重建对应 collection，再写入当前源文件生成的 chunk。
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import cast

from app.config import get_settings
from app.rag.store import Track, index_track, load_directory


def _check() -> int:
    s = get_settings()
    for track, path in [("industry", s.industry_kb_path), ("enterprise", s.enterprise_kb_path)]:
        docs = load_directory(path, category=track)
        print(f"[check] {track}: 目录={path} 文档数={len(docs)}")
        for d in docs:
            print(f"    - {d.metadata.get('source')}")
    return 0


def _build(track_arg: str) -> int:
    tracks = ["industry", "enterprise"] if track_arg == "both" else [track_arg]
    total = 0
    for track in tracks:
        print(f"[build] 开始索引 {track}（首次会下载 BGE 模型）...", flush=True)
        t0 = time.time()
        n = index_track(cast(Track, track))
        elapsed = time.time() - t0
        print(f"[build] {track} 完成: {n} chunks, 耗时 {elapsed:.1f}s")
        total += n
    print(f"[build] 全部完成: 共 {total} chunks")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="双轨知识库索引")
    parser.add_argument(
        "--track",
        choices=["industry", "enterprise", "both"],
        default="both",
        help="要索引的 track（默认两个都索引）",
    )
    parser.add_argument("--check", action="store_true", help="只清点文档不索引")
    args = parser.parse_args(argv)

    if args.check:
        return _check()
    return _build(args.track)


if __name__ == "__main__":
    sys.exit(main())
