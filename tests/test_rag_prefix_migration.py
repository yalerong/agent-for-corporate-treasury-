"""source 前缀迁移（无前缀 → <track>/ 前缀）下的 doc_id 稳定性与旧块清理。"""
from __future__ import annotations

import pytest
from qdrant_client import QdrantClient

from app.rag import get_store
from app.rag.docmeta import NEVER
from app.rag.ingest import Manifest, sync_directory
from tests.test_rag import DeterministicFakeEmbeddings

PREFIX = "enterprise/"


@pytest.fixture
def store():
    return get_store(
        "enterprise", DeterministicFakeEmbeddings(),
        client=QdrantClient(location=":memory:"), collection_name="t_prefix",
    )


@pytest.fixture
def root(tmp_dir):
    r = tmp_dir / "kb"
    r.mkdir()
    return r


def _sync(root, store, tmp_dir, **kw):
    m = Manifest(tmp_dir / "m.json")
    return sync_directory(root, "enterprise", store, m, **kw), m


def _sources(store) -> set[str]:
    pts, _ = store.client.scroll(store.collection_name, limit=100, with_payload=True)
    return {p.payload["metadata"]["source"] for p in pts}


def test_implicit_doc_id_survives_prefix_migration(root, store, tmp_dir):
    """没显式写 doc_id 的文档，加前缀后 id 不变，别人 supersedes 它的引用不会断。"""
    (root / "old.md").write_text("---\nstatus: approved\n---\n旧制度。", "utf-8")
    (root / "new.md").write_text(
        "---\nstatus: approved\ndoc_id: n1\nsupersedes: [old.md]\neffective_from: 2026-09-01\n---\n新制度。",
        "utf-8",
    )
    _, m = _sync(root, store, tmp_dir)
    assert m.docs["old.md"]["superseded_on"] == 20260901

    _, m = _sync(root, store, tmp_dir, source_prefix=PREFIX)
    old = m.docs[PREFIX + "old.md"]
    assert old["meta"]["doc_id"] == "old.md"            # id 沿用，没被新 source 顶掉
    assert old["superseded_on"] == 20260901              # 仍然被取代，没有“复活”
    assert m.docs[PREFIX + "new.md"]["superseded_on"] == NEVER

    # 之后改了这份文档的内容，id 依然稳定
    (root / "old.md").write_text("---\nstatus: approved\n---\n旧制度（小改）。", "utf-8")
    _, m = _sync(root, store, tmp_dir, source_prefix=PREFIX)
    assert m.docs[PREFIX + "old.md"]["meta"]["doc_id"] == "old.md"
    assert m.docs[PREFIX + "old.md"]["superseded_on"] == 20260901


def test_explicit_doc_id_unaffected_by_migration(root, store, tmp_dir):
    (root / "a.md").write_text("---\nstatus: approved\ndoc_id: R-1\n---\n内容。", "utf-8")
    _sync(root, store, tmp_dir)
    _, m = _sync(root, store, tmp_dir, source_prefix=PREFIX)
    assert m.docs[PREFIX + "a.md"]["meta"]["doc_id"] == "R-1"


def test_delete_after_failed_migration_cleans_original_chunks(root, store, tmp_dir):
    for name in ("a.md", "b.md", "c.md"):
        (root / name).write_text(f"---\nstatus: approved\n---\n{name} 的内容。", "utf-8")
    _sync(root, store, tmp_dir)
    assert "a.md" in _sources(store)

    # 迁移时 a.md 的元数据恰好写坏：条目改名保留、旧名字下的块还在
    (root / "a.md").write_text("---\nstaus: approved\n---\n坏元数据。", "utf-8")
    r, m = _sync(root, store, tmp_dir, source_prefix=PREFIX)
    assert PREFIX + "a.md" in r.errors
    assert "a.md" in _sources(store)

    # 随后这份文件被删：旧名字下的块不能变成没有账可对的永久孤儿
    (root / "a.md").unlink()
    r, m = _sync(root, store, tmp_dir, source_prefix=PREFIX)
    assert r.deleted == [PREFIX + "a.md"]
    assert "a.md" not in _sources(store) and PREFIX + "a.md" not in _sources(store)
    assert PREFIX + "a.md" not in m.docs
