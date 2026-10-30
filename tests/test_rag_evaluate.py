"""评测骨架：题集格式、打分口径、合成语料上的陷阱行为。"""
from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from app.rag import get_store
from app.rag.evaluate import (
    REFUSAL_PHRASE,
    ROOT,
    Question,
    format_report,
    load_questions,
    run_eval,
    score_answer,
    score_retrieval,
)
from app.rag.ingest import Manifest, sync_directory
from tests.test_rag import DeterministicFakeEmbeddings

ASOF = date(2026, 10, 15)
QFILE = ROOT / "eval" / "questions.jsonl"


@pytest.fixture(scope="module")
def eval_store():
    store = get_store(
        "enterprise", DeterministicFakeEmbeddings(),
        client=QdrantClient(location=":memory:"), collection_name="t_eval",
    )
    with tempfile.TemporaryDirectory() as tmp:
        r = sync_directory(ROOT / "eval" / "corpus", "enterprise", store, Manifest(Path(tmp) / "m.json"))
    assert not r.errors
    return store


def test_split_dev_and_holdout_are_disjoint_and_nonempty():
    dev, hold = load_questions(QFILE), load_questions(QFILE, holdout=True)
    assert dev and hold and not ({q.id for q in dev} & {q.id for q in hold})
    assert all(q.split == "dev" for q in dev)


def test_unknown_field_rejected(tmp_dir):
    f = tmp_dir / "q.jsonl"
    f.write_text('{"id":"x","question":"?","expct_text":["a"]}\n', "utf-8")
    with pytest.raises(ValueError, match="未知字段"):
        load_questions(f)


def test_score_retrieval():
    q = Question(id="a", question="?", expect_sources=["x.md"], forbid_sources=["bad.md"])
    assert score_retrieval(q, ["x.md", "y.md"]).passed
    assert "缺来源 x.md" in score_retrieval(q, ["y.md"]).reasons
    assert "不该出现的来源 bad.md" in score_retrieval(q, ["x.md", "bad.md"]).reasons[0]


def test_score_answer_refusal_and_keywords():
    normal = Question(id="n", question="?", expect_text=["资金经理"], forbid_text=["不论金额"])
    assert score_answer(normal, "须资金经理审批").passed
    assert not score_answer(normal, "资料中没有").passed            # 不该拒答
    assert not score_answer(normal, "资金经理，且不论金额").passed  # 说了旧结论
    trap = Question(id="t", question="?", must_refuse=True, forbid_text=["800"])
    assert score_answer(trap, REFUSAL_PHRASE).passed
    assert not score_answer(trap, "每晚 800 元").passed               # 该拒答却作答


def test_synthetic_corpus_traps_hold_at_retrieval_level(eval_store):
    """草稿、已废止、被取代的文档，在评测日期下绝不出现在检索结果里。"""
    results = run_eval(eval_store, load_questions(QFILE), asof=ASOF, k=6)
    leaked = [r for r in results["retrieval"] if any("不该出现" in x for x in r.reasons)]
    assert not leaked, format_report(results)


def test_asof_changes_superseded_outcome(eval_store):
    q = Question(id="s", question="关联方借款审批", expect_sources=["rulings/R-001.md"],
                 forbid_sources=["rulings/R-002.md"])
    before = run_eval(eval_store, [q], asof=date(2026, 8, 1), k=6)["retrieval"][0]
    after = run_eval(eval_store, [q], asof=date(2026, 10, 15), k=6)["retrieval"][0]
    assert before.passed        # R-002 9/1 才生效：8 月仍按 R-001
    assert not after.passed     # 10 月 R-001 已退场


def test_answer_layer_with_stub_fn(eval_store):
    qs = load_questions(QFILE)
    always_refuse = run_eval(eval_store, qs, asof=ASOF, answer_fn=lambda q, c: REFUSAL_PHRASE)
    ans = always_refuse["answer"]
    traps = [r for r in ans if r.category.startswith("trap")]
    normals = [r for r in ans if not r.category.startswith("trap")]
    assert all(r.passed for r in traps) and not any(r.passed for r in normals)
    assert "[answer]" in format_report(always_refuse)
