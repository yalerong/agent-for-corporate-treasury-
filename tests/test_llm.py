"""LLM 工厂测试。

不实际调用 LLM API；只验证：
- get_chat_model() 返回 BaseChatModel 实例
- 缓存语义（lru_cache 单例）+ reset_chat_model_cache() 能清空
- monkeypatch 注入 fake 后，nodes 取到的是 fake
"""
from __future__ import annotations

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from app import llm as llm_module
from app.config import get_settings


@pytest.fixture(autouse=True)
def _set_dummy_api_key(monkeypatch):
    """ChatOpenAI 需要非空 api_key 才能实例化，给个 dummy 即可（不会真调用）。"""
    monkeypatch.setenv("LLM_API_KEY", "sk-dummy-for-test")
    get_settings.cache_clear()
    llm_module.reset_chat_model_cache()
    yield
    llm_module.reset_chat_model_cache()


class TestChatModelFactory:
    def test_returns_base_chat_model(self):
        m = llm_module.get_chat_model()
        assert isinstance(m, BaseChatModel)

    def test_returns_singleton(self):
        a = llm_module.get_chat_model()
        b = llm_module.get_chat_model()
        assert a is b

    def test_reset_cache_creates_new_instance(self):
        a = llm_module.get_chat_model()
        llm_module.reset_chat_model_cache()
        b = llm_module.get_chat_model()
        assert a is not b


class TestMonkeyPatchInjection:
    def test_can_replace_with_fake(self, monkeypatch):
        fake = FakeListChatModel(responses=["hello world"])
        monkeypatch.setattr(llm_module, "get_chat_model", lambda: fake)
        m = llm_module.get_chat_model()
        assert m is fake
        # FakeListChatModel.invoke 返回 AIMessage
        result = m.invoke("anything")
        assert result.content == "hello world"
