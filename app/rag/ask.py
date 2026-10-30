"""命令行问答：检索（带出处）+ 可选 LLM 作答。

    python -m app.rag.ask "单笔调拨 60 万元需要谁审批？"
    python -m app.rag.ask "..." --track both --k 4 --asof 2026-10-15

- 只返回「现在算数」的文档块（见 access.py）；一条都没有 → 直接说资料中没有，不去问 LLM
- 没配置 LLM（LLM_API_KEY 为空）→ 只列出命中的原文和出处，不生成回答
- 每条命中都带 doc_id / 来源 / 生效期，方便人去核对原文
"""
from __future__ import annotations

import argparse
from datetime import date

from app.config import get_settings
from app.rag.access import active_filter
from app.rag.evaluate import REFUSAL_PHRASE, build_answer_fn


def _citation(d) -> str:
    m = d.metadata
    span = ""
    if m.get("eff_from") or m.get("eff_to", 99999999) != 99999999:
        span = f"｜生效 {m.get('eff_from') or '—'} ~ {m.get('eff_to') if m.get('eff_to') != 99999999 else '—'}"
    return f"{m.get('doc_id', '?')}（{m.get('source', '?')}{span}）"


def ask(question: str, tracks: list[str], k: int, asof: date, *, use_llm: bool) -> str:
    from app.rag.store import get_embeddings, get_store

    emb = get_embeddings()
    docs = []
    for t in tracks:
        docs += get_store(t, emb).similarity_search(question, k=k, filter=active_filter(asof))
    if not docs:
        return f"{REFUSAL_PHRASE}与此问题相关的、现行有效的内容。"
    lines = []
    if use_llm:
        lines.append(build_answer_fn()(question, [d.page_content for d in docs]))
        lines.append("\n出处：")
    else:
        lines.append("（未配置 LLM，仅列出命中的原文）\n")
    for i, d in enumerate(docs, 1):
        lines.append(f"[{i}] {_citation(d)}")
        if not use_llm:
            lines.append(f"    {d.page_content.strip()[:300]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="知识库问答（带出处）")
    p.add_argument("question")
    p.add_argument("--track", choices=["industry", "enterprise", "both"], default="both")
    p.add_argument("--k", type=int, default=4)
    p.add_argument("--asof", default=None, help="按某天的口径回答，缺省今天")
    args = p.parse_args(argv)
    tracks = ["industry", "enterprise"] if args.track == "both" else [args.track]
    asof = date.fromisoformat(args.asof) if args.asof else date.today()
    use_llm = bool(get_settings().llm_api_key.get_secret_value())
    print(ask(args.question, tracks, args.k, asof, use_llm=use_llm))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
