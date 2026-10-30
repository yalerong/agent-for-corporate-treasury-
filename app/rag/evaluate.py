"""知识库评测：固定题集 + 确定性打分，不靠 LLM 评判 LLM。

两层，可单跑：
- 检索层（离线、免 Key）：该出现的来源在不在前 k 条；不该出现的（草稿、已废止、被取代）有没有混进来
- 回答层（`--answer`，需要配置 LLM）：该答的有没有答对关键词、不该答的有没有拒答、有没有说出旧结论

题集 `eval/questions.jsonl`，一行一题：
  id, split(dev|holdout), category, question,
  expect_sources[], forbid_sources[], expect_text[], forbid_text[], must_refuse
**holdout 默认不跑**：开发集用来调，封存集只在定稿前跑一次（`--holdout`），否则就成了训练集。
`--asof` 钉评测日期——取代/生效期依赖"今天"，不钉住的话同一份题集不同日子结果会变。

用法：
    python -m app.rag.evaluate --asof 2026-10-15                # 开发集，仅检索层
    python -m app.rag.evaluate --asof 2026-10-15 --answer       # 加回答层
    python -m app.rag.evaluate --asof 2026-10-15 --holdout      # 封存集（定稿前才跑）
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app.rag.access import active_filter
from app.rag.answer import REFUSAL_PHRASE, build_answer_fn, format_doc

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Question:
    id: str
    question: str
    category: str = "normal"
    split: str = "dev"
    expect_sources: list[str] = field(default_factory=list)
    forbid_sources: list[str] = field(default_factory=list)
    expect_text: list[str] = field(default_factory=list)
    forbid_text: list[str] = field(default_factory=list)
    must_refuse: bool = False


@dataclass
class Result:
    id: str
    category: str
    passed: bool
    reasons: list[str]


def load_questions(path: Path, *, holdout: bool = False) -> list[Question]:
    want = "holdout" if holdout else "dev"
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        d = json.loads(line)
        unknown = set(d) - set(Question.__dataclass_fields__)
        if unknown:
            raise ValueError(f"{path.name}:{n} 未知字段 {sorted(unknown)}")
        q = Question(**d)
        if q.split not in ("dev", "holdout"):
            raise ValueError(f"{path.name}:{n} split 只能是 dev/holdout")
        if q.split == want:
            out.append(q)
    return out


def score_retrieval(q: Question, sources: list[str]) -> Result:
    """sources: 检索返回的前 k 条的 source（可重复）。"""
    reasons = []
    for s in q.expect_sources:
        if s not in sources:
            reasons.append(f"缺来源 {s}")
    for s in q.forbid_sources:
        if s in sources:
            reasons.append(f"不该出现的来源 {s}")
    return Result(q.id, q.category, not reasons, reasons)


_STRIP = " \t\r\n。.！!，,；;：:\"'“”‘’「」『』（）()[]【】"


def is_refusal(answer: str) -> bool:
    """整段回答就是拒答标记才算拒答（容忍首尾空白、标点、引号）。

    不能用"包含"判断：`资料中没有明确规定，但通常每年发 1000 股` 带着拒答词却在编造，
    按"包含"算会让不支持的回答拿到绿灯，而评测要抓的恰恰是这种。
    """
    return answer.strip(_STRIP) == REFUSAL_PHRASE


def score_answer(q: Question, answer: str) -> Result:
    reasons = []
    refused = is_refusal(answer)
    if q.must_refuse and not refused:
        reasons.append("该拒答却作答")
    if not q.must_refuse and refused:
        reasons.append("不该拒答却拒答")
    if not q.must_refuse:
        for t in q.expect_text:
            if t not in answer:
                reasons.append(f"答案缺关键词 {t!r}")
    for t in q.forbid_text:
        if t in answer:
            reasons.append(f"答案含不该出现的 {t!r}")
    return Result(q.id, q.category, not reasons, reasons)


def run_eval(
    store,
    questions: list[Question],
    *,
    asof: date,
    k: int = 4,
    answer_fn: Callable[[str, list[str]], str] | None = None,
) -> dict[str, list[Result]]:
    """返回 {"retrieval": [...], "answer": [...]}（没传 answer_fn 则无 answer 层）。

    可检索的块数不超过 k 时直接报错：检索会把所有可用块都返回，"来源在不在"恒为真，
    排序坏了也测不出来——这种绿灯比没有评测更危险。
    """
    eligible = store.client.count(
        store.collection_name, count_filter=active_filter(asof), exact=True
    ).count
    if eligible <= k:
        raise ValueError(
            f"{asof} 口径下只有 {eligible} 个可检索块，不超过 k={k}：每题都会返回全部块，"
            "评测没有区分度。请增加干扰文档（语料更大）或调小 --k"
        )
    out: dict[str, list[Result]] = {"retrieval": [], "answer": []}
    for q in questions:
        docs = store.similarity_search(q.question, k=k, filter=active_filter(asof))
        out["retrieval"].append(score_retrieval(q, [d.metadata["source"] for d in docs]))
        if answer_fn is not None:
            out["answer"].append(score_answer(q, answer_fn(q.question, [format_doc(d, i) for i, d in enumerate(docs, 1)])))
    if answer_fn is None:
        del out["answer"]
    return out


def format_report(results: dict[str, list[Result]]) -> str:
    lines = []
    for layer, rs in results.items():
        by_cat: dict[str, list[Result]] = defaultdict(list)
        for r in rs:
            by_cat[r.category].append(r)
        lines.append(f"[{layer}] 通过 {sum(r.passed for r in rs)}/{len(rs)}")
        for cat, items in sorted(by_cat.items()):
            lines.append(f"  {cat}: {sum(r.passed for r in items)}/{len(items)}")
        lines += [f"  ✗ {r.id}: {'；'.join(r.reasons)}" for r in rs if not r.passed]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from app.rag.ingest import Manifest, sync_directory
    from app.rag.store import get_embeddings, get_store

    p = argparse.ArgumentParser(description="知识库评测")
    p.add_argument("--asof", required=True, help="评测日期 YYYY-MM-DD（必填，保证可复现）")
    p.add_argument("--corpus", type=Path, default=ROOT / "eval" / "corpus")
    p.add_argument("--questions", type=Path, default=ROOT / "eval" / "questions.jsonl")
    p.add_argument("--k", type=int, default=4)
    p.add_argument("--answer", action="store_true", help="加回答层（需要配置 LLM）")
    p.add_argument("--holdout", action="store_true", help="跑封存集；定稿前才用")
    args = p.parse_args(argv)

    from qdrant_client import QdrantClient

    store = get_store(
        "enterprise", get_embeddings(), client=QdrantClient(location=":memory:"),
        collection_name="eval",
    )
    with tempfile.TemporaryDirectory() as tmp:
        report = sync_directory(args.corpus, "enterprise", store, Manifest(Path(tmp) / "m.json"))
    if report.blocked:
        print(f"语料摄取被拒绝: {report.blocked}", file=sys.stderr)
        return 2
    if report.errors:
        print(report.errors, file=sys.stderr)
        return 2
    questions = load_questions(args.questions, holdout=args.holdout)
    if not questions:
        # 没有题就是没有跑：不能拿"0/0 全过"当绿灯
        split = "holdout" if args.holdout else "dev"
        print(f"{args.questions} 里没有 split={split} 的题目", file=sys.stderr)
        return 2
    results = run_eval(
        store, questions, asof=date.fromisoformat(args.asof), k=args.k,
        answer_fn=build_answer_fn() if args.answer else None,
    )
    print(format_report(results))
    return 0 if all(r.passed for rs in results.values() for r in rs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
