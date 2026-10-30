"""检索侧过滤：未批准 / 未生效 / 已失效 / 已被取代 / 无元数据 的块一律查不到。"""
from __future__ import annotations

from datetime import date

import pytest
from qdrant_client import QdrantClient

from app.rag import get_store, index_to_store
from app.rag.access import active_filter
from app.rag.ingest import Manifest, sync_directory
from tests.test_rag import DeterministicFakeEmbeddings

TODAY = date(2026, 10, 15)


@pytest.fixture
def kb(tmp_dir):
    root = tmp_dir / "kb"
    root.mkdir()
    store = get_store(
        "enterprise", DeterministicFakeEmbeddings(),
        client=QdrantClient(location=":memory:"), collection_name="t_access",
    )

    def put(name: str, front: str, body: str = "调拨制度内容。"):
        (root / name).write_text(f"---\n{front}\n---\n{body}", "utf-8")

    def sync():
        return sync_directory(root, "enterprise", store, Manifest(tmp_dir / "m.json"))

    def visible(today=TODAY) -> set[str]:
        hits = store.similarity_search("调拨制度", k=50, filter=active_filter(today))
        return {h.metadata["source"] for h in hits}

    return put, sync, visible, store, root


def test_only_approved_effective_current_docs_visible(kb):
    put, sync, visible, _, _ = kb
    put("ok.md", "status: approved")
    put("draft.md", "status: draft")
    put("deprecated.md", "status: deprecated")
    put("future.md", "status: approved\neffective_from: 2026-11-01")
    put("expired.md", "status: approved\neffective_to: 2026-10-01")
    put("window.md", "status: approved\neffective_from: 2026-10-01\neffective_to: 2026-10-31")
    sync()
    assert visible() == {"ok.md", "window.md"}
    assert visible(date(2026, 11, 2)) == {"ok.md", "future.md"}  # 同一份索引，换日期结果随之变


def test_superseded_doc_hidden_only_from_effective_date(kb):
    put, sync, visible, _, _ = kb
    put("old.md", "status: approved\ndoc_id: r1")
    put("new.md", "status: approved\ndoc_id: r2\nsupersedes: [r1]\neffective_from: 2026-10-20")
    sync()
    assert visible(date(2026, 10, 15)) == {"old.md"}          # 新的还没生效，旧的仍算数
    assert visible(date(2026, 10, 20)) == {"new.md"}          # 生效日起旧的退场
    assert visible(date(2026, 12, 1)) == {"new.md"}


def test_draft_superseder_does_not_hide_current_doc(kb):
    put, sync, visible, _, _ = kb
    put("old.md", "status: approved\ndoc_id: r1")
    put("new.md", "status: draft\ndoc_id: r2\nsupersedes: [r1]")
    sync()
    assert visible() == {"old.md"}


def test_blocks_without_metadata_fail_closed(kb, tmp_dir):
    _, _, visible, store, _ = kb
    legacy = tmp_dir / "legacy"
    legacy.mkdir()
    (legacy / "x.md").write_text("调拨制度旧索引。", "utf-8")
    index_to_store(store, legacy, "enterprise")  # 旧路径：不带任何元数据
    assert store.similarity_search("调拨制度", k=5)  # 不过滤时确实在库里
    assert visible() == set()                         # 过滤后查不到，而不是默认放行


def test_classification_is_not_filtered_yet(kb):
    """权限先全部放开：restricted 的已批准文档也查得到（以后加角色过滤时改这条）。"""
    put, sync, visible, _, _ = kb
    put("secret.md", "status: approved\nclassification: restricted")
    sync()
    assert visible() == {"secret.md"}
