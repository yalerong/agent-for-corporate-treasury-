"""knowledge @tool 测试：双轨检索的 LangChain Tool 包装。"""
from __future__ import annotations

import hashlib
import re

import pytest
from langchain_core.embeddings import Embeddings
from qdrant_client import QdrantClient

from app.config import get_settings
from app.rag.store import get_store, index_to_store
from app.tools import knowledge as knowledge_module
from app.tools.knowledge import (
    reset_stores,
    search_enterprise_knowledge,
    search_industry_knowledge,
)


# 复用与 test_rag.py 相同思路的 fake embeddings（不依赖 torch）
class _FakeEmbeddings(Embeddings):
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
def populated_stores(monkeypatch, project_root, tmp_dir):
    """构造内存 Qdrant + 索引 stub KB + 注入 knowledge._stores 缓存。

    同时把 AUDIT_LOG_PATH 指向 tmp_dir，避免污染项目目录。
    """
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_dir / "audit.jsonl"))
    get_settings.cache_clear()

    embeddings = _FakeEmbeddings(dim=64)
    client = QdrantClient(location=":memory:")
    industry = get_store("industry", embeddings, client, "test_industry")
    enterprise = get_store("enterprise", embeddings, client, "test_enterprise")
    index_to_store(industry, project_root / "knowledge_base" / "industry", "industry")
    index_to_store(enterprise, project_root / "knowledge_base" / "enterprise", "enterprise")
    knowledge_module._stores["industry"] = industry
    knowledge_module._stores["enterprise"] = enterprise
    yield tmp_dir
    reset_stores()


class TestToolDecoration:
    """验证 @tool 装饰器正确暴露名称和 schema。"""

    def test_industry_is_langchain_tool(self):
        # @tool 应该把函数包装为有 .name / .description / .args_schema 的对象
        assert hasattr(search_industry_knowledge, "name")
        assert hasattr(search_industry_knowledge, "description")
        assert search_industry_knowledge.name == "search_industry_knowledge"

    def test_industry_description_mentions_regulation(self):
        # docstring 中应包含"法规"等关键词，供 LLM 决定何时调用
        desc = search_industry_knowledge.description
        assert "法规" in desc or "监管" in desc

    def test_enterprise_description_mentions_internal(self):
        desc = search_enterprise_knowledge.description
        assert "内部" in desc or "制度" in desc or "审批" in desc

    def test_args_schema_has_query_field(self):
        schema = search_industry_knowledge.args_schema.model_json_schema()
        assert "query" in schema["properties"]
        assert schema["properties"]["query"]["type"] == "string"


class TestIndustrySearch:
    def test_returns_content_with_source(self, populated_stores):
        result = search_industry_knowledge.invoke({"query": "反洗钱大额报告"})
        assert "no results" not in result
        assert "[source:" in result

    def test_default_k_returns_multiple(self, populated_stores):
        result = search_industry_knowledge.invoke({"query": "外汇"})
        # 至少应有一条 [n] 序号；stub 数据少时可能只 1-2 条
        assert "[1]" in result

    def test_k_parameter(self, populated_stores):
        result = search_industry_knowledge.invoke({"query": "法规", "k": 1})
        assert "[2]" not in result  # k=1 不应有第二条


class TestEnterpriseSearch:
    def test_returns_content_with_source(self, populated_stores):
        result = search_enterprise_knowledge.invoke({"query": "调拨审批"})
        assert "[source:" in result

    def test_isolated_from_industry(self, populated_stores):
        # enterprise 搜索结果的 source 不应包含 industry/ 路径
        result = search_enterprise_knowledge.invoke({"query": "制度"})
        assert "industry/" not in result
        # 应包含 enterprise/ 路径
        assert "enterprise/" in result


class TestAuditIntegration:
    def test_industry_call_writes_audit_log(self, populated_stores):
        log_path = populated_stores / "audit.jsonl"
        search_industry_knowledge.invoke({"query": "反洗钱"})
        content = log_path.read_text(encoding="utf-8")
        assert "search_industry_knowledge" in content

    def test_enterprise_call_writes_audit_log(self, populated_stores):
        log_path = populated_stores / "audit.jsonl"
        search_enterprise_knowledge.invoke({"query": "审批"})
        content = log_path.read_text(encoding="utf-8")
        assert "search_enterprise_knowledge" in content


class TestEmptyResults:
    def test_no_stub_empty_response(self, monkeypatch, tmp_dir):
        """完全空白的 store 应返回 (no results)。"""
        monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_dir / "audit.jsonl"))
        get_settings.cache_clear()
        embeddings = _FakeEmbeddings(dim=64)
        client = QdrantClient(location=":memory:")
        empty_store = get_store("industry", embeddings, client, "empty_industry")
        knowledge_module._stores["industry"] = empty_store
        try:
            result = search_industry_knowledge.invoke({"query": "anything"})
            assert result == "(no results)"
        finally:
            reset_stores()
