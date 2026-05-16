"""RAG 模块测试：文档加载、切块、最小双轨检索链路。"""
from __future__ import annotations

import hashlib
import re

import pytest
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from qdrant_client import QdrantClient

from app.rag import (
    DualTrackRetriever,
    chunk_documents,
    get_store,
    index_to_store,
    load_directory,
)


class DeterministicFakeEmbeddings(Embeddings):
    """词袋哈希 → 归一化向量。共享 token 越多 cosine 相似度越高。

    用于测试，避免依赖真实 HF 模型下载（torch 约 750MB）。
    """

    def __init__(self, dim: int = 64):
        self.dim = dim

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return re.findall(r"[A-Za-z]+|[一-龥]", text.lower())

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in self._tokens(text):
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16) % self.dim
            v[h] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / norm for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


@pytest.fixture
def fake_embeddings() -> Embeddings:
    return DeterministicFakeEmbeddings(dim=64)


@pytest.fixture
def in_memory_client() -> QdrantClient:
    return QdrantClient(location=":memory:")


class TestLoadDirectory:
    def test_loads_md_files(self, project_root):
        docs = load_directory(project_root / "knowledge_base" / "industry", category="industry")
        assert len(docs) >= 2, "industry/ 应至少有 2 个 stub 文档"
        assert all(d.metadata["category"] == "industry" for d in docs)
        assert all("industry" in d.metadata["source"] for d in docs)

    def test_ignores_non_text_files(self, tmp_dir):
        (tmp_dir / "doc.md").write_text("# Hello\nworld", encoding="utf-8")
        (tmp_dir / "image.png").write_bytes(b"\x89PNG")
        (tmp_dir / "code.py").write_text("print('hi')", encoding="utf-8")
        docs = load_directory(tmp_dir, category="test")
        assert len(docs) == 1
        assert docs[0].metadata["source"].endswith("doc.md")

    def test_missing_directory_returns_empty(self, tmp_dir):
        assert load_directory(tmp_dir / "does_not_exist", category="test") == []

    def test_subcategory_from_parent_dir(self, project_root):
        docs = load_directory(project_root / "knowledge_base" / "industry", category="industry")
        assert {d.metadata["subcategory"] for d in docs} == {"regulations"}


class TestChunkDocuments:
    def test_chunks_preserve_metadata(self):
        doc = Document(
            page_content="第一段。\n\n第二段。" * 50,
            metadata={"category": "industry", "source": "x.md"},
        )
        chunks = chunk_documents([doc], chunk_size=100, chunk_overlap=10)
        assert len(chunks) > 1
        for c in chunks:
            assert c.metadata["category"] == "industry"
            assert c.metadata["source"] == "x.md"

    def test_chunk_size_approximately_respected(self):
        doc = Document(page_content="abc" * 1000, metadata={"category": "test"})
        chunks = chunk_documents([doc], chunk_size=200, chunk_overlap=20)
        assert all(len(c.page_content) <= 250 for c in chunks)


class TestDualTrackRetrieval:
    """端到端最小检索链路：加载 stub → 索引 → 双轨检索 → 分离验证。"""

    def _build_retriever(self, project_root, fake_embeddings, in_memory_client, suffix=""):
        industry_store = get_store(
            "industry",
            embeddings=fake_embeddings,
            client=in_memory_client,
            collection_name=f"test_industry{suffix}",
        )
        enterprise_store = get_store(
            "enterprise",
            embeddings=fake_embeddings,
            client=in_memory_client,
            collection_name=f"test_enterprise{suffix}",
        )
        n_i = index_to_store(
            industry_store, project_root / "knowledge_base" / "industry", "industry"
        )
        n_e = index_to_store(
            enterprise_store, project_root / "knowledge_base" / "enterprise", "enterprise"
        )
        assert n_i > 0 and n_e > 0
        return DualTrackRetriever(industry_store, enterprise_store, k=2)

    def test_both_tracks_return_separated_results(
        self, project_root, fake_embeddings, in_memory_client
    ):
        retriever = self._build_retriever(project_root, fake_embeddings, in_memory_client, "_both")
        results = retriever.search("反洗钱大额报告", track="both")

        assert "industry" in results and len(results["industry"]) > 0
        assert "enterprise" in results and len(results["enterprise"]) > 0
        assert all(d.metadata["category"] == "industry" for d in results["industry"])
        assert all(d.metadata["category"] == "enterprise" for d in results["enterprise"])

    def test_industry_only(self, project_root, fake_embeddings, in_memory_client):
        retriever = self._build_retriever(project_root, fake_embeddings, in_memory_client, "_io")
        results = retriever.search("外汇", track="industry")
        assert "industry" in results
        assert "enterprise" not in results

    def test_enterprise_only(self, project_root, fake_embeddings, in_memory_client):
        retriever = self._build_retriever(project_root, fake_embeddings, in_memory_client, "_eo")
        results = retriever.search("调拨", track="enterprise")
        assert "enterprise" in results
        assert "industry" not in results
