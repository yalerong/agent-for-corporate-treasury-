"""三个检索调用方（Tool / API / DualTrackRetriever）和线上问答节点都必须套上“现在算数”的过滤。

此前这些调用方的测试语料全是 approved，把任意一处的 `filter=active_filter()` 删掉，
测试依然全绿——真正把关的过滤只在 test_rag_access 里直接测过。这里用带草稿和被取代文档的
语料，保证删掉过滤会立刻红。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from qdrant_client import QdrantClient

from app import api as api_module
from app import llm as llm_module
from app.agents.nodes import knowledge_node
from app.api import app
from app.config import get_settings
from app.rag import DualTrackRetriever, get_store
from app.rag.ingest import Manifest, sync_directory
from app.tools import knowledge as knowledge_module
from app.tools.knowledge import search_enterprise_knowledge, search_industry_knowledge
from tests.test_rag import DeterministicFakeEmbeddings

LIVE = "调拨须经资金经理审批"
DRAFT = "草稿版调拨规则尚未发布"
OLD = "旧裁决调拨须经董事会审批"


@pytest.fixture
def stores(tmp_dir, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_dir / "audit.jsonl"))
    get_settings.cache_clear()
    client = QdrantClient(location=":memory:")
    emb = DeterministicFakeEmbeddings()
    out = {}
    for track in ("industry", "enterprise"):
        root = tmp_dir / track
        root.mkdir()
        (root / "live.md").write_text(f"---\nstatus: approved\n---\n{LIVE}。", "utf-8")
        (root / "draft.md").write_text(f"---\nstatus: draft\n---\n{DRAFT}。", "utf-8")
        (root / "old.md").write_text(f"---\nstatus: approved\ndoc_id: r1\n---\n{OLD}。", "utf-8")
        (root / "new.md").write_text(
            "---\nstatus: approved\ndoc_id: r2\nsupersedes: [r1]\n---\n新裁决调拨由资金经理审批。",
            "utf-8",
        )
        store = get_store(track, emb, client, f"t_callers_{track}")
        sync_directory(root, track, store, Manifest(tmp_dir / f"m-{track}.json"))
        out[track] = store
        knowledge_module._stores[track] = store
    yield out
    knowledge_module.reset_stores()
    get_settings.cache_clear()


def _assert_only_live(text: str) -> None:
    assert LIVE in text or "新裁决" in text
    assert DRAFT not in text, "草稿文档不该被检索到"
    assert OLD not in text, "已被取代的旧文档不该被检索到"


def test_tools_apply_filter(stores):
    for tool in (search_industry_knowledge, search_enterprise_knowledge):
        _assert_only_live(tool.invoke({"query": "调拨审批", "k": 10}))


def test_dual_track_retriever_applies_filter(stores):
    res = DualTrackRetriever(stores["industry"], stores["enterprise"], k=10).search("调拨审批")
    text = "\n".join(d.page_content for docs in res.values() for d in docs)
    _assert_only_live(text)


def test_api_knowledge_endpoint_applies_filter(stores, monkeypatch):
    monkeypatch.setenv("API_CASHIER_TOKEN", "t-cashier")
    get_settings.cache_clear()
    api_module._graph_singleton = None
    r = TestClient(app).get(
        "/api/v1/knowledge", params={"q": "调拨审批", "k": 10},
        headers={"Authorization": "Bearer t-cashier"},
    )
    assert r.status_code == 200
    body = r.json()
    text = "\n".join(h["content"] for hits in body.values() for h in hits)
    _assert_only_live(text)


class _CaptureLLM:
    def __init__(self, reply: str = "资金经理审批。"):
        self.prompts: list[str] = []
        self.reply = reply

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return type("R", (), {"content": self.reply})()


def test_knowledge_node_uses_shared_prompt_with_refusal_rule_and_doc_id(stores, monkeypatch):
    llm = _CaptureLLM()
    monkeypatch.setattr(llm_module, "get_chat_model", lambda: llm)
    knowledge_node({"user_role": "cashier", "messages": [HumanMessage(content="调拨审批")]})
    prompt = llm.prompts[0]
    assert "资料中没有" in prompt              # 与评测回答层同一条拒答指令
    assert "doc_id: r2" in prompt              # 引用带 doc_id，能核对到具体裁决
    assert DRAFT not in prompt and OLD not in prompt  # 草稿与被取代的文档根本没进上下文


def test_knowledge_node_refuses_without_calling_llm_when_nothing_retrieved(tmp_dir, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_dir / "audit.jsonl"))
    get_settings.cache_clear()
    client = QdrantClient(location=":memory:")
    for track in ("industry", "enterprise"):
        knowledge_module._stores[track] = get_store(
            track, DeterministicFakeEmbeddings(), client, f"t_empty_{track}"
        )
    llm = _CaptureLLM()
    monkeypatch.setattr(llm_module, "get_chat_model", lambda: llm)
    try:
        out = knowledge_node({"user_role": "cashier", "messages": [HumanMessage(content="任意问题")]})
    finally:
        knowledge_module.reset_stores()
        get_settings.cache_clear()
    assert not llm.prompts, "没有任何命中时不该去问模型"
    assert "资料中没有" in out["final_output"]
