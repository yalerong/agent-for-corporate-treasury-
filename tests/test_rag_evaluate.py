"""评测骨架：题集格式、打分口径、合成语料上的陷阱行为。"""
from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from app.rag import get_store
from app.rag.access import active_filter
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


def test_default_corpus_has_more_eligible_chunks_than_default_k(eval_store):
    """默认 k 下评测必须有区分度：可检索块数要明显多于 k，来源检查才取决于排序。"""
    eligible = eval_store.client.count(
        eval_store.collection_name, count_filter=active_filter(ASOF), exact=True
    ).count
    assert eligible > 4 + 2


def test_eval_refuses_to_run_when_k_covers_every_eligible_chunk(eval_store):
    with pytest.raises(ValueError, match="没有区分度"):
        run_eval(eval_store, load_questions(QFILE), asof=ASOF, k=500)


def test_refusal_must_be_standalone_not_substring():
    trap = Question(id="t03", question="?", must_refuse=True)
    fabricated = "资料中没有明确规定，但公司的股票期权通常每年向员工发放 1000 股。"
    assert not score_answer(trap, fabricated).passed  # 带着拒答词却在编造：不能拿绿灯
    for ok in (REFUSAL_PHRASE, f"  {REFUSAL_PHRASE}。\n", f"“{REFUSAL_PHRASE}”"):
        assert score_answer(trap, ok).passed, ok  # 只有标记本身（容忍首尾标点引号）
    normal = Question(id="n", question="?", expect_text=["资金经理"])
    assert score_answer(normal, "资料中没有明确规定，但须资金经理审批。").passed  # 普通题不被误判成拒答


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


def test_empty_split_is_error_not_green(monkeypatch, tmp_path, capsys):
    """题集里没有该 split 的题时不能报"0/0 全过"——高分不等于通过。"""
    from types import SimpleNamespace

    import app.rag.ingest as ingest_module
    import app.rag.store as store_module
    from app.rag import evaluate as ev

    only_dev = tmp_path / "q.jsonl"
    only_dev.write_text('{"id":"d1","split":"dev","question":"?"}\n', "utf-8")
    monkeypatch.setattr(store_module, "get_embeddings", lambda: object())
    monkeypatch.setattr(store_module, "get_store", lambda *a, **k: object())
    monkeypatch.setattr(
        ingest_module, "sync_directory",
        lambda *a, **k: SimpleNamespace(errors={}, blocked=None),
    )

    rc = ev.main(["--asof", "2026-10-15", "--holdout", "--questions", str(only_dev)])
    assert rc == 2 and "holdout" in capsys.readouterr().err
