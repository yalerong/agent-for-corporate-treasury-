"""想废止一份文档但元数据写坏时，报告要明说“旧版本仍在服务”，不能只报一个格式错误。"""
from __future__ import annotations

from qdrant_client import QdrantClient

from app.rag import get_store
from app.rag.ingest import Manifest, sync_directory
from tests.test_rag import DeterministicFakeEmbeddings


def test_failed_deprecation_says_old_version_still_serves(tmp_dir):
    root = tmp_dir / "kb"
    root.mkdir()
    doc = root / "a.md"
    doc.write_text("---\nstatus: approved\n---\n旧制度。", "utf-8")
    store = get_store(
        "enterprise", DeterministicFakeEmbeddings(),
        client=QdrantClient(location=":memory:"), collection_name="t_stale",
    )
    m = Manifest(tmp_dir / "m.json")
    sync_directory(root, "enterprise", store, m)
    doc.write_text("---\nstatus: deprecatd\n---\n旧制度。", "utf-8")  # 想废止，但拼错了
    r = sync_directory(root, "enterprise", store, Manifest(tmp_dir / "m.json"))
    msg = r.errors["a.md"]
    assert "取值非法" in msg and "仍是已批准状态" in msg and "没有生效" in msg
