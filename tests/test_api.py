"""FastAPI 服务端测试。

通过 TestClient 直接调用，无需启动 uvicorn。
所有 LLM 调用走 fake_llm，所有 RAG 走 populated_stores。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import app
from app.config import get_settings


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_AUTH_TOKEN", "test-token")
    get_settings.cache_clear()
    return TestClient(app, headers={"X-API-Key": "test-token"})


class TestHealthz:
    def test_healthz(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_api_requires_key_when_configured(self, monkeypatch):
        monkeypatch.setenv("API_AUTH_TOKEN", "test-token")
        get_settings.cache_clear()
        raw_client = TestClient(app)

        r = raw_client.post(
            "/api/v1/chat",
            json={"role": "cashier", "message": "查余额", "task": "inquiry"},
        )

        assert r.status_code == 401


class TestChat:
    def test_chat_with_explicit_task_completes(self, client, fake_llm, populated_stores):
        fake_llm(["any"])
        r = client.post(
            "/api/v1/chat",
            json={"role": "cashier", "message": "查余额", "task": "inquiry"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "completed"
        assert body["current_role"] == "cashier"
        assert "[出纳]" in body["final_output"]
        assert body["thread_id"]

    def test_chat_with_llm_classification(self, client, fake_llm, populated_stores):
        # supervisor 分类 + knowledge 合成
        fake_llm(["knowledge", "综合答案 [来源: aml_law.md]"])
        r = client.post(
            "/api/v1/chat",
            json={"role": "cashier", "message": "反洗钱标准是什么？"},
        )
        body = r.json()
        assert body["status"] == "completed"
        assert body["current_role"] == "knowledge"
        assert "综合答案" in body["final_output"]

    def test_chat_invalid_role_400(self, client):
        r = client.post(
            "/api/v1/chat",
            json={"role": "hacker", "message": "x", "task": "inquiry"},
        )
        assert r.status_code == 400

    def test_chat_invalid_task_400(self, client):
        r = client.post(
            "/api/v1/chat",
            json={"role": "cashier", "message": "x", "task": "evil_task"},
        )
        assert r.status_code == 400

    def test_chat_unauthorized_returns_rejected(self, client, fake_llm, populated_stores):
        fake_llm(["any"])
        r = client.post(
            "/api/v1/chat",
            json={"role": "cashier", "message": "做个外汇套保", "task": "fx"},
        )
        body = r.json()
        assert body["status"] == "rejected"
        assert "无权" in body["final_output"]

    def test_chat_triggers_interrupt(self, client, fake_llm, populated_stores):
        fake_llm(["any"])
        r = client.post(
            "/api/v1/chat",
            json={"role": "treasury_manager", "message": "套保美元", "task": "fx"},
        )
        body = r.json()
        assert body["status"] == "interrupted"
        assert body["interrupt_payload"]["kind"] == "approval_request"
        assert body["interrupt_payload"]["task"] == "fx"

    def test_chat_large_transfer_amount_triggers_interrupt(
        self, client, fake_llm, populated_stores
    ):
        fake_llm(["any"])
        r = client.post(
            "/api/v1/chat",
            json={
                "role": "treasury_supervisor",
                "message": "调拨 600 万",
                "task": "transfer",
                "amount": "6000000",
            },
        )
        body = r.json()
        assert body["status"] == "interrupted"
        assert body["interrupt_payload"]["task"] == "transfer"
        assert body["interrupt_payload"]["amount"] == "6000000"

    def test_chat_cashier_transfer_with_approved_instruction_completes(
        self, client, fake_llm, populated_stores
    ):
        fake_llm(["any"])
        r = client.post(
            "/api/v1/chat",
            json={
                "role": "cashier",
                "message": "执行已审批付款",
                "task": "transfer",
                "approved_instruction_id": "APR-2026-API-001",
            },
        )
        body = r.json()
        assert body["status"] == "completed"
        assert body["current_role"] == "cashier"


class TestApprovals:
    def test_approve_resume(self, client, fake_llm, populated_stores):
        fake_llm(["any"])
        # 先触发 HITL
        r1 = client.post(
            "/api/v1/chat",
            json={"role": "treasury_manager", "message": "套保", "task": "fx"},
        )
        tid = r1.json()["thread_id"]

        # 批准
        r2 = client.post(
            f"/api/v1/approvals/{tid}",
            json={"approved": True, "instruction_id": "APR-2026-API-001"},
        )
        body = r2.json()
        assert r2.status_code == 200
        assert body["status"] == "completed"
        assert "APR-2026-API-001" in body["final_output"]

    def test_reject_resume(self, client, fake_llm, populated_stores):
        fake_llm(["any"])
        r1 = client.post(
            "/api/v1/chat",
            json={"role": "treasury_manager", "message": "套保", "task": "fx"},
        )
        tid = r1.json()["thread_id"]
        r2 = client.post(
            f"/api/v1/approvals/{tid}",
            json={"approved": False, "reason": "本月额度已用尽"},
        )
        body = r2.json()
        assert body["status"] == "rejected"
        assert "本月额度已用尽" in body["final_output"]


class TestKnowledgeEndpoint:
    def test_query_both_tracks(self, client, populated_stores):
        r = client.get("/api/v1/knowledge", params={"q": "反洗钱", "target": "both"})
        body = r.json()
        assert "industry" in body
        assert "enterprise" in body
        assert any("source" in d for d in body["industry"])

    def test_query_industry_only(self, client, populated_stores):
        r = client.get("/api/v1/knowledge", params={"q": "外汇", "target": "industry"})
        body = r.json()
        assert "industry" in body
        assert "enterprise" not in body

    def test_invalid_k_rejected(self, client, populated_stores):
        r = client.get("/api/v1/knowledge", params={"q": "x", "k": 0})
        assert r.status_code == 422


class TestAuditLogs:
    def test_audit_logs_returns_records(self, client, fake_llm, populated_stores):
        fake_llm(["any"])
        # 触发一次 knowledge tool 调用以产生审计记录
        client.get("/api/v1/knowledge", params={"q": "反洗钱"})
        # /api/v1/knowledge 不直接走 audit（绕过 @tool），所以触发 /chat 产生记录
        client.post(
            "/api/v1/chat",
            json={"role": "cashier", "message": "反洗钱", "task": "knowledge"},
        )
        r = client.get("/api/v1/audit/logs", params={"limit": 50})
        records = r.json()
        assert isinstance(records, list)
        # knowledge_node 会调 search_*_knowledge 两个 @tool，至少产生 2 条
        tools_seen = {rec["tool"] for rec in records}
        assert "search_industry_knowledge" in tools_seen
        assert "search_enterprise_knowledge" in tools_seen

    def test_audit_logs_filter_by_tool(self, client, fake_llm, populated_stores):
        fake_llm(["any"])
        client.post(
            "/api/v1/chat",
            json={"role": "cashier", "message": "反洗钱", "task": "knowledge"},
        )
        r = client.get(
            "/api/v1/audit/logs",
            params={"tool": "search_industry_knowledge"},
        )
        records = r.json()
        assert all(rec["tool"] == "search_industry_knowledge" for rec in records)

    def test_audit_logs_empty_when_no_file(self, client, monkeypatch, tmp_dir):
        # 指向不存在的路径
        monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_dir / "nonexistent.jsonl"))
        from app.config import get_settings
        get_settings.cache_clear()
        r = client.get("/api/v1/audit/logs")
        assert r.json() == []
