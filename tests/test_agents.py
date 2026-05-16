"""Agent 节点（LLM 接入版）测试。

覆盖：
- supervisor_node 用 LLM 做意图分类（current_task 未显式提供时）
- supervisor_node 当 current_task 已提供时跳过 LLM
- knowledge_node 调双轨检索 + LLM 合成答案
- 端到端：自由文本 → supervisor 分类 → knowledge → 真实答复
"""
from __future__ import annotations

from langchain_core.messages import HumanMessage

from app.agents.nodes import knowledge_node, supervisor_node
from app.graph import build_graph


class TestSupervisorClassification:
    def test_passthrough_when_task_already_set(self, fake_llm):
        # fake_llm 提供响应但不应被调用
        fake_llm(["fx"])  # 如果被调用会返回 fx，但 test 期望 current_task 不变
        out = supervisor_node({"user_role": "cashier", "current_task": "inquiry"})
        assert out == {"current_role": "supervisor"}

    def test_classify_from_human_message(self, fake_llm):
        fake_llm(["inquiry"])
        out = supervisor_node(
            {
                "user_role": "cashier",
                "messages": [HumanMessage(content="今天总部账户还有多少钱？")],
            }
        )
        assert out["current_role"] == "supervisor"
        assert out["current_task"] == "inquiry"

    def test_classify_fx_intent(self, fake_llm):
        fake_llm(["fx"])
        out = supervisor_node(
            {
                "user_role": "treasury_manager",
                "messages": [HumanMessage(content="美元兑人民币现在汇率是多少？需要做套保")],
            }
        )
        assert out["current_task"] == "fx"

    def test_invalid_llm_response_defaults_to_knowledge(self, fake_llm):
        fake_llm(["这不是一个有效意图哦"])
        out = supervisor_node(
            {"user_role": "cashier", "messages": [HumanMessage(content="任意问题")]}
        )
        assert out["current_task"] == "knowledge"

    def test_llm_response_with_punctuation_stripped(self, fake_llm):
        fake_llm(["inquiry."])  # 带句号
        out = supervisor_node(
            {"user_role": "cashier", "messages": [HumanMessage(content="查余额")]}
        )
        assert out["current_task"] == "inquiry"

    def test_no_message_no_task_defaults_to_knowledge(self, fake_llm):
        # fake_llm 不应被消费（早早返回默认值）
        fake_llm([])
        out = supervisor_node({"user_role": "cashier"})
        assert out["current_task"] == "knowledge"


class TestKnowledgeAgent:
    def test_calls_both_tools_and_synthesizes(self, fake_llm, populated_stores):
        fake_llm(["综合两条参考资料的回答 [来源: aml_law.md]"])
        out = knowledge_node(
            {
                "user_role": "cashier",
                "messages": [HumanMessage(content="反洗钱大额报告标准是多少？")],
            }
        )
        assert out["current_role"] == "knowledge"
        assert "[知识库]" in out["final_output"]
        assert "综合两条参考资料的回答" in out["final_output"]
        # 两类上下文都已写入 state，便于审计回溯
        assert out["industry_context"]
        assert out["enterprise_context"]
        assert "[source:" in out["industry_context"]

    def test_empty_query_returns_no_query_message(self, fake_llm, populated_stores):
        fake_llm(["should not be used"])
        out = knowledge_node({"user_role": "cashier"})
        assert "[知识库] 未收到查询内容" in out["final_output"]

    def test_audit_log_records_tool_calls(self, fake_llm, populated_stores):
        fake_llm(["any answer"])
        knowledge_node(
            {"user_role": "cashier", "messages": [HumanMessage(content="查询")]}
        )
        log_content = (populated_stores / "audit.jsonl").read_text(encoding="utf-8")
        assert "search_industry_knowledge" in log_content
        assert "search_enterprise_knowledge" in log_content


class TestEndToEndWithLLM:
    """从自由文本经 supervisor 分类、路由到对应 Agent。"""

    def test_natural_language_inquiry(self, fake_llm, thread_config, populated_stores):
        # 第一次调用：supervisor 分类；第二次：knowledge_node 合成
        # 这里期望被分类为 inquiry → cashier 节点（占位）
        fake_llm(["inquiry"])
        graph = build_graph()
        out = graph.invoke(
            {
                "user_role": "cashier",
                "messages": [HumanMessage(content="今天总部账户还有多少钱？")],
            },
            config=thread_config,
        )
        assert out["current_task"] == "inquiry"
        assert out["current_role"] == "cashier"
        assert "[出纳]" in out["final_output"]

    def test_natural_language_knowledge(self, fake_llm, thread_config, populated_stores):
        # supervisor 分类为 knowledge → knowledge_node 调 LLM 合成
        fake_llm(["knowledge", "根据法规和内部制度，标准如下 [来源: aml_law.md]"])
        graph = build_graph()
        out = graph.invoke(
            {
                "user_role": "cashier",
                "messages": [HumanMessage(content="反洗钱大额交易报告的法定标准是多少？")],
            },
            config=thread_config,
        )
        assert out["current_role"] == "knowledge"
        assert "根据法规和内部制度" in out["final_output"]
