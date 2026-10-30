"""ask：无命中直接拒答（不调 LLM）、无 LLM 时列原文+出处、有 LLM 时附出处。"""
from __future__ import annotations

from datetime import date

import pytest
from qdrant_client import QdrantClient

from app.rag import ask as ask_module
from app.rag import get_store
from app.rag.evaluate import REFUSAL_PHRASE, ROOT
from app.rag.ingest import Manifest, sync_directory
from tests.test_rag import DeterministicFakeEmbeddings


@pytest.fixture
def wired(monkeypatch, tmp_dir):
    store = get_store(
        "enterprise", DeterministicFakeEmbeddings(),
        client=QdrantClient(location=":memory:"), collection_name="t_ask",
    )
    sync_directory(ROOT / "eval" / "corpus", "enterprise", store, Manifest(tmp_dir / "m.json"))
    monkeypatch.setattr("app.rag.store.get_embeddings", lambda: DeterministicFakeEmbeddings())
    monkeypatch.setattr("app.rag.store.get_store", lambda track, emb: store)
    return store


def test_no_llm_lists_passages_with_citation(wired):
    out = ask_module.ask("关联方借款", ["enterprise"], 3, date(2026, 10, 15), use_llm=False)
    assert "未配置 LLM" in out and "rulings/R-002.md" in out
    assert "rulings/R-001.md" not in out  # 被取代的旧裁决不作为出处出现


def test_no_hits_refuses_without_calling_llm(wired, monkeypatch, tmp_dir):
    empty = get_store(
        "enterprise", DeterministicFakeEmbeddings(),
        client=QdrantClient(location=":memory:"), collection_name="t_ask_empty",
    )
    monkeypatch.setattr("app.rag.store.get_store", lambda track, emb: empty)

    def boom():
        raise AssertionError("不应调用 LLM")

    monkeypatch.setattr(ask_module, "build_answer_fn", boom)
    out = ask_module.ask("x", ["enterprise"], 3, date(2026, 10, 15), use_llm=True)
    assert out.startswith(REFUSAL_PHRASE)


def test_with_llm_appends_sources(wired, monkeypatch):
    monkeypatch.setattr(ask_module, "build_answer_fn", lambda: lambda q, ctx: "资金经理审批。")
    out = ask_module.ask("关联方借款", ["enterprise"], 3, date(2026, 10, 15), use_llm=True)
    assert out.startswith("资金经理审批。") and "出处：" in out and "R-002" in out
