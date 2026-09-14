"""FastAPI service tests."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app.api import app
from app.config import get_settings


@pytest.fixture
def api_headers(monkeypatch):
    tokens = {
        "cashier": "cashier-token",
        "supervisor": "supervisor-token",
        "manager": "manager-token",
        "admin": "admin-token",
    }
    monkeypatch.setenv("API_CASHIER_TOKEN", tokens["cashier"])
    monkeypatch.setenv("API_SUPERVISOR_TOKEN", tokens["supervisor"])
    monkeypatch.setenv("API_MANAGER_TOKEN", tokens["manager"])
    monkeypatch.setenv("API_ADMIN_TOKEN", tokens["admin"])
    get_settings.cache_clear()

    def _headers(role: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {tokens[role]}"}

    return _headers


@pytest.fixture
def client():
    api_module._graph_singleton = None
    api_module._pending_approvals.clear()
    return TestClient(app)


class TestHealthz:
    def test_healthz(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


class TestAuth:
    def test_api_routes_fail_closed_without_token_config(self, client):
        r = client.get("/api/v1/audit/logs")
        assert r.status_code == 401

    def test_invalid_bearer_token_rejected(self, client, api_headers):
        r = client.get(
            "/api/v1/audit/logs",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert r.status_code == 401

    def test_client_role_is_not_trusted(self, client, api_headers):
        r = client.post(
            "/api/v1/chat",
            headers=api_headers("cashier"),
            json={"role": "admin", "message": "hedge USD", "task": "fx"},
        )
        assert r.status_code == 403
        assert r.json()["detail"] == "task override requires admin role"


class TestChat:
    def test_chat_with_admin_task_override_completes(
        self,
        client,
        api_headers,
        fake_llm,
        populated_stores,
    ):
        fake_llm(["any"])
        r = client.post(
            "/api/v1/chat",
            headers=api_headers("admin"),
            json={"message": "check balance", "task": "inquiry"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "completed"
        assert body["current_role"] == "cashier"
        assert "[出纳]" in body["final_output"]
        assert body["thread_id"]

    def test_chat_with_llm_classification(self, client, api_headers, fake_llm, populated_stores):
        fake_llm(["knowledge", "综合答案 [来源: aml_law.md]"])
        r = client.post(
            "/api/v1/chat",
            headers=api_headers("cashier"),
            json={"message": "反洗钱标准是什么？"},
        )
        body = r.json()
        assert body["status"] == "completed"
        assert body["current_role"] == "knowledge"
        assert "综合答案" in body["final_output"]

    def test_chat_invalid_task_400(self, client, api_headers):
        r = client.post(
            "/api/v1/chat",
            headers=api_headers("admin"),
            json={"message": "x", "task": "evil_task"},
        )
        assert r.status_code == 400

    def test_non_admin_task_override_forbidden(self, client, api_headers):
        r = client.post(
            "/api/v1/chat",
            headers=api_headers("manager"),
            json={"message": "x", "task": "inquiry"},
        )
        assert r.status_code == 403

    def test_chat_unauthorized_returns_rejected(
        self,
        client,
        api_headers,
        fake_llm,
        populated_stores,
    ):
        fake_llm(["fx"])
        r = client.post(
            "/api/v1/chat",
            headers=api_headers("cashier"),
            json={"message": "做个外汇套保"},
        )
        body = r.json()
        assert body["status"] == "rejected"
        assert "无权" in body["final_output"]

    def test_chat_triggers_interrupt(self, client, api_headers, fake_llm, populated_stores):
        fake_llm(["fx"])
        r = client.post(
            "/api/v1/chat",
            headers=api_headers("manager"),
            json={"message": "套保美元"},
        )
        body = r.json()
        assert body["status"] == "interrupted"
        assert body["interrupt_payload"]["kind"] == "approval_request"
        assert body["interrupt_payload"]["task"] == "fx"


class TestApprovals:
    def test_approve_resume(self, client, api_headers, fake_llm, populated_stores):
        fake_llm(["fx"])
        r1 = client.post(
            "/api/v1/chat",
            headers=api_headers("manager"),
            json={"message": "套保"},
        )
        tid = r1.json()["thread_id"]

        r2 = client.post(
            f"/api/v1/approvals/{tid}",
            headers=api_headers("supervisor"),
            json={"approved": True, "instruction_id": "APR-2026-API-001"},
        )
        body = r2.json()
        assert r2.status_code == 200
        assert body["status"] == "completed"
        assert "APR-2026-API-001" in body["final_output"]

    def test_reject_resume(self, client, api_headers, fake_llm, populated_stores):
        fake_llm(["fx"])
        r1 = client.post(
            "/api/v1/chat",
            headers=api_headers("manager"),
            json={"message": "套保"},
        )
        tid = r1.json()["thread_id"]
        r2 = client.post(
            f"/api/v1/approvals/{tid}",
            headers=api_headers("manager"),
            json={"approved": False, "reason": "本月额度已用尽"},
        )
        body = r2.json()
        assert body["status"] == "rejected"
        assert "本月额度已用尽" in body["final_output"]

    def test_cashier_cannot_approve(self, client, api_headers):
        r = client.post(
            "/api/v1/approvals/thread-1",
            headers=api_headers("cashier"),
            json={"approved": True, "instruction_id": "APR-1"},
        )
        assert r.status_code == 403

    def test_anonymous_cannot_approve(self, client):
        r = client.post(
            "/api/v1/approvals/thread-1",
            json={"approved": True, "instruction_id": "APR-1"},
        )
        assert r.status_code == 401

    def test_approval_requires_pending_thread(self, client, api_headers):
        r = client.post(
            "/api/v1/approvals/not-pending",
            headers=api_headers("manager"),
            json={"approved": True, "instruction_id": "APR-1"},
        )
        assert r.status_code == 404

    def test_approval_requires_auditable_decision_details(
        self, client, api_headers, fake_llm, populated_stores
    ):
        fake_llm(["fx"])
        start = client.post(
            "/api/v1/chat",
            headers=api_headers("manager"),
            json={"message": "套保"},
        )
        tid = start.json()["thread_id"]

        approved = client.post(
            f"/api/v1/approvals/{tid}",
            headers=api_headers("manager"),
            json={"approved": True},
        )
        assert approved.status_code == 400

        rejected = client.post(
            f"/api/v1/approvals/{tid}",
            headers=api_headers("manager"),
            json={"approved": False},
        )
        assert rejected.status_code == 400

    def test_approval_rejects_whitespace_only_decision_details(
        self, client, api_headers, fake_llm, populated_stores
    ):
        fake_llm(["fx"])
        start = client.post(
            "/api/v1/chat",
            headers=api_headers("manager"),
            json={"message": "hedge"},
        )
        tid = start.json()["thread_id"]

        approved = client.post(
            f"/api/v1/approvals/{tid}",
            headers=api_headers("manager"),
            json={"approved": True, "instruction_id": "   "},
        )
        assert approved.status_code == 400

        rejected = client.post(
            f"/api/v1/approvals/{tid}",
            headers=api_headers("manager"),
            json={"approved": False, "reason": "\t  "},
        )
        assert rejected.status_code == 400

    def test_approval_strips_decision_details_before_resume(
        self, client, api_headers, fake_llm, populated_stores
    ):
        fake_llm(["fx"])
        start = client.post(
            "/api/v1/chat",
            headers=api_headers("manager"),
            json={"message": "hedge"},
        )
        tid = start.json()["thread_id"]

        approved = client.post(
            f"/api/v1/approvals/{tid}",
            headers=api_headers("manager"),
            json={"approved": True, "instruction_id": "  APR-2026-API-002  "},
        )
        body = approved.json()
        assert approved.status_code == 200
        assert "APR-2026-API-002" in body["final_output"]
        assert "  APR-2026-API-002  " not in body["final_output"]


class TestKnowledgeEndpoint:
    def test_query_both_tracks(self, client, api_headers, populated_stores):
        r = client.get(
            "/api/v1/knowledge",
            headers=api_headers("cashier"),
            params={"q": "反洗钱", "target": "both"},
        )
        body = r.json()
        assert "industry" in body
        assert "enterprise" in body
        assert any("source" in d for d in body["industry"])

    def test_query_industry_only(self, client, api_headers, populated_stores):
        r = client.get(
            "/api/v1/knowledge",
            headers=api_headers("cashier"),
            params={"q": "外汇", "target": "industry"},
        )
        body = r.json()
        assert "industry" in body
        assert "enterprise" not in body

    def test_invalid_k_rejected(self, client, api_headers, populated_stores):
        r = client.get(
            "/api/v1/knowledge",
            headers=api_headers("cashier"),
            params={"q": "x", "k": 0},
        )
        assert r.status_code == 422


class TestAuditLogs:
    def test_audit_logs_admin_only(self, client, api_headers):
        r = client.get("/api/v1/audit/logs", headers=api_headers("manager"))
        assert r.status_code == 403

    def test_audit_logs_returns_records(self, client, api_headers, fake_llm, populated_stores):
        fake_llm(["knowledge", "ok"])
        client.post(
            "/api/v1/chat",
            headers=api_headers("cashier"),
            json={"message": "反洗钱"},
        )
        r = client.get(
            "/api/v1/audit/logs",
            headers=api_headers("admin"),
            params={"limit": 50},
        )
        records = r.json()
        assert isinstance(records, list)
        tools_seen = {rec["tool"] for rec in records}
        assert "search_industry_knowledge" in tools_seen
        assert "search_enterprise_knowledge" in tools_seen

    def test_audit_logs_filter_by_tool(self, client, api_headers, fake_llm, populated_stores):
        fake_llm(["knowledge", "ok"])
        client.post(
            "/api/v1/chat",
            headers=api_headers("cashier"),
            json={"message": "反洗钱"},
        )
        r = client.get(
            "/api/v1/audit/logs",
            headers=api_headers("admin"),
            params={"tool": "search_industry_knowledge"},
        )
        records = r.json()
        assert all(rec["tool"] == "search_industry_knowledge" for rec in records)

    def test_audit_logs_empty_when_no_file(self, client, api_headers, monkeypatch, tmp_dir):
        monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_dir / "nonexistent.jsonl"))
        get_settings.cache_clear()
        r = client.get("/api/v1/audit/logs", headers=api_headers("admin"))
        assert r.json() == []
