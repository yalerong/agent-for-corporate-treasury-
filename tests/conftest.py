"""Pytest 公共 fixtures。

核心目标：让 Settings 在测试中不读项目根目录的 .env、不被宿主机环境变量污染。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

import pytest
from langchain_core.embeddings import Embeddings

from app.config import get_settings


@pytest.fixture
def project_root() -> Path:
    """项目根目录的绝对路径。fixture 用 chdir 改变 cwd 后仍可定位真实 KB 文件。"""
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def tmp_dir():
    """pytest 自带 tmp_path 在某些 Windows 环境因 %TEMP%\\pytest-of-* 权限失败，用此替代。"""
    path = Path(tempfile.mkdtemp(prefix="treasury_tmp_"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def thread_config():
    """LangGraph 带 checkpointer 后所有 invoke 必须传 thread_id。每个测试一个独立 thread。"""
    import uuid
    return {"configurable": {"thread_id": f"test-{uuid.uuid4()}"}}


# ── 共享的 fake embeddings + 已索引 stub KB 的 stores ──────────


class _FakeEmbeddings(Embeddings):
    """词袋哈希 → 归一化向量。共享 token 越多 cosine 相似度越高。

    用于测试，避免依赖 HF 模型（torch ~750MB）下载。
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
    """空白 fake embeddings 实例。"""
    return _FakeEmbeddings(dim=64)


@pytest.fixture
def populated_stores(monkeypatch, project_root, tmp_dir):
    """构造 :memory: Qdrant + 索引 stub KB + 注入 knowledge._stores 缓存。

    同时把 AUDIT_LOG_PATH 指向 tmp_dir，避免污染项目目录。
    yield 返回 tmp_dir（含 audit.jsonl 路径）。
    """
    from qdrant_client import QdrantClient

    from app.rag.store import get_store, index_to_store
    from app.tools import knowledge as knowledge_module

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
    try:
        yield tmp_dir
    finally:
        knowledge_module.reset_stores()


@pytest.fixture
def fake_llm(monkeypatch):
    """工厂 fixture：调用 fake_llm(["resp1", "resp2", ...]) 注入 FakeListChatModel。

    覆盖 app.llm.get_chat_model 后，所有节点取到的都是这个 fake。
    """
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from app import llm as llm_module

    def _make(responses):
        fake = FakeListChatModel(responses=list(responses))
        monkeypatch.setattr(llm_module, "get_chat_model", lambda: fake)
        return fake

    return _make

# 所有以这些前缀开头的环境变量都属于本项目配置，测试时一律清除。
_PROJECT_ENV_PREFIXES = (
    "LLM_",
    "EMBEDDING_",
    "QDRANT_",
    "INDUSTRY_",
    "ENTERPRISE_",
    "BANK_",
    "FX_",
    "PAYMENT_",
    "AUDIT_",
)


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    """每个测试：切换到无 .env 的临时目录 + 清空本项目环境变量 + 重置缓存。

    注意：刻意不使用 pytest 的 tmp_path fixture，因其依赖
    %TEMP%\\pytest-of-<user>，在某些 Windows 环境下因权限问题失败。
    """
    tmp = tempfile.mkdtemp(prefix="treasury_test_")
    monkeypatch.chdir(tmp)
    for key in [k for k in os.environ if k.startswith(_PROJECT_ENV_PREFIXES)]:
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    shutil.rmtree(tmp, ignore_errors=True)
