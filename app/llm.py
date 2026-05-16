"""LLM 客户端工厂。

按 DESIGN.md §4.2.0 LangChain 职责分工：LLM 客户端统一走 LangChain ChatModel 抽象，
不允许节点代码直接 import openai SDK。

provider 选项（OpenAI / DeepSeek / Qwen 兼容模式）通过 settings.llm_base_url 区分，
都用 langchain_openai.ChatOpenAI 实例化。

测试通过 monkeypatch app.llm.get_chat_model 注入 FakeListChatModel。
"""
from __future__ import annotations

from functools import lru_cache

from langchain_core.language_models import BaseChatModel

from app.config import get_settings


@lru_cache(maxsize=1)
def get_chat_model() -> BaseChatModel:
    """构造默认 ChatModel 单例。测试 monkeypatch 覆盖此函数即可。"""
    from langchain_openai import ChatOpenAI

    s = get_settings()
    return ChatOpenAI(
        model=s.llm_model,
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        temperature=s.llm_temperature,
    )


def reset_chat_model_cache() -> None:
    """测试钩子：清空 lru_cache。若 get_chat_model 被 monkeypatch 替换，no-op。"""
    cache_clear = getattr(get_chat_model, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()
