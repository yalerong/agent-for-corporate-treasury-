"""问答的提示词与引用格式：线上节点、命令行 ask、评测回答层共用这一份。

之前线上 `knowledge_node` 和评测各写各的提示词：评测里“该拒答就只答拒答词”的绿灯，
在线上并不成立。现在三处都从这里取，评测测到的就是线上跑的。

提示词刻意写得短、不分条、不放示例句——小模型会把规则块或示例整句照抄进回答。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

from langchain_core.documents import Document

REFUSAL_PHRASE = "资料中没有"
NO_RESULTS = "(no results)"
REFUSAL_MESSAGE = f"{REFUSAL_PHRASE}与此问题相关的、现行有效的内容。"


def format_doc(d: Document, i: int) -> str:
    """一条命中：序号、来源（保持 `[source: 路径]` 前缀）、doc_id 与生效期，再接原文。"""
    m = d.metadata
    extra = []
    doc_id = m.get("doc_id")
    if doc_id and doc_id != m.get("source"):
        extra.append(f"doc_id: {doc_id}")
    eff_from, eff_to = m.get("eff_from") or 0, m.get("eff_to") or 99999999
    if eff_from:
        extra.append(f"{eff_from} 起生效")
    if eff_to != 99999999:
        extra.append(f"{eff_to} 止")
    tag = f" [{'｜'.join(extra)}]" if extra else ""
    return f"[{i}] [source: {m.get('source', 'unknown')}]{tag}\n{d.page_content}"


def format_docs(docs: Iterable[Document]) -> str:
    docs = list(docs)
    if not docs:
        return NO_RESULTS
    return "\n\n".join(format_doc(d, i) for i, d in enumerate(docs, 1))


def build_prompt(question: str, sections: list[tuple[str, str]]) -> str:
    """sections: [(标题, 已格式化的资料文本)]。资料文本为空或 (no results) 的也照列出。"""
    rules = "只根据下面的资料回答问题，每个结论后面用 [来源: 路径] 标出处。"
    if len(sections) > 1:
        rules += "行业法规与企业制度冲突时，以法规为准并指出冲突。"
    rules += f"资料里没有答案就只回答“{REFUSAL_PHRASE}”，不要补充其他内容。"
    body = "\n\n".join(f"【{title}】\n{text or '（无）'}" for title, text in sections)
    return f"{rules}\n\n{body}\n\n问题：{question}"


def build_answer_fn() -> Callable[[str, list[str]], str]:
    """评测回答层 / 命令行用的生成函数。contexts 是已用 format_doc 格式化的命中。"""
    from app import llm as llm_module

    def answer(question: str, contexts: list[str]) -> str:
        prompt = build_prompt(question, [("资料", "\n\n".join(contexts))])
        resp = llm_module.get_chat_model().invoke(prompt)
        return resp.content if hasattr(resp, "content") else str(resp)

    return answer
