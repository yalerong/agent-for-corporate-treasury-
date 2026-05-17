"""双轨知识库检索 Tool。

把 app.rag 的 DualTrackRetriever 包装为两个 LangChain @tool，
让 Agent（Phase 2 接 LLM 后）能通过名称 + docstring 决定何时调用。

按 CLAUDE.md §4.1 Tool 铁律：
- docstring 完整描述"做什么 / 何时用 / 参数 / 返回"
- 参数有类型注解，LangChain 自动生成 schema
- @audit 装饰记录调用日志
"""
from __future__ import annotations

from collections.abc import Iterable

from langchain_core.documents import Document
from langchain_core.tools import tool

from app.rag.store import get_embeddings, get_store
from app.tools.audit import audit

# 模块级缓存：第一次调用时实例化，测试可通过 monkeypatch 覆盖。
# 注意是 dict 而不是 lru_cache，便于测试 setitem 注入 fake store。
_stores: dict[str, object] = {}


def _store(track: str):
    if track not in _stores:
        _stores[track] = get_store(track, get_embeddings())  # type: ignore[arg-type]
    return _stores[track]


def reset_stores() -> None:
    """测试钩子：清空 store 缓存。"""
    _stores.clear()


def _format(docs: Iterable[Document]) -> str:
    docs = list(docs)
    if not docs:
        return "(no results)"
    parts = []
    for i, d in enumerate(docs, 1):
        source = d.metadata.get("source", "unknown")
        parts.append(f"[{i}] [source: {source}]\n{d.page_content}")
    return "\n\n".join(parts)


@tool
@audit()
def search_industry_knowledge(query: str, k: int = 4) -> str:
    """检索行业法规、监管政策、通用案例知识库（外汇管理条例、反洗钱法等）。

    何时调用：用户提问涉及法规要求、监管标准、合规底线、行业惯例时。
    例如："反洗钱大额报告标准是多少？" "外汇登记的条件是什么？"

    Args:
        query: 自然语言查询或法规关键词
        k: 返回前 k 条最相关结果，默认 4 条

    Returns:
        拼接的多段文本，每段前缀 `[i] [source: 文件路径]` 便于回溯。
        无结果时返回 "(no results)"。
    """
    docs = _store("industry").similarity_search(query, k=k)  # type: ignore[union-attr]
    return _format(docs)


@tool
@audit()
def search_enterprise_knowledge(query: str, k: int = 4) -> str:
    """检索企业内部制度、操作流程、审批规范知识库（资金调拨办法、内控制度等）。

    何时调用：用户提问涉及内部金额阈值、审批权限、岗位职责、业务流程时。
    例如："500 万以上调拨需要谁审批？" "跨境调拨内部要求是什么？"

    Args:
        query: 自然语言查询或内部制度关键词
        k: 返回前 k 条最相关结果，默认 4 条

    Returns:
        拼接的多段文本，格式同 search_industry_knowledge。
    """
    docs = _store("enterprise").similarity_search(query, k=k)  # type: ignore[union-attr]
    return _format(docs)
