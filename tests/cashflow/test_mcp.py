"""Phase B MCP Server：十个只读工具直接调函数验证（stdio 协议层由 mcp 库保证）。"""
import json
import shutil
import tempfile
from pathlib import Path

import mcp_server as srv
import pytest


@pytest.fixture()
def mcp_root(pipeline_root, monkeypatch) -> Path:
    dst = Path(tempfile.mkdtemp(prefix="treasury_mcp_"))
    shutil.copytree(pipeline_root, dst, dirs_exist_ok=True)
    monkeypatch.setenv("CASHFLOW_ROOT", str(dst))
    yield dst
    shutil.rmtree(dst, ignore_errors=True)


def test_registered_tool_count():
    assert len(srv.TOOLS) == 10  # 与 factor-analysis 先例同规模，全只读


def test_report_and_sections(mcp_root):
    assert "2026-07-30" in json.loads(srv.list_runs())
    assert "资金月报与滚动预测" in srv.get_report()
    sec = srv.get_report_section("fx_advice")
    assert "外汇交易管控建议" in sec
    assert "预算执行差异" not in sec  # 只取单节
    with pytest.raises(ValueError):
        srv.get_report_section("no_such_metric")


def test_lineage(mcp_root):
    lin = json.loads(srv.get_lineage())
    assert set(lin) >= {"fx_advice", "forecast_4w", "pattern_validation"}


def test_query_forecast(mcp_root):
    out = json.loads(srv.query_forecast(currency="CNY"))
    assert out["total_by_currency"]["CNY"] == pytest.approx(151879.68, abs=0.01)  # 深圳子公司@10日,4周窗口命中1次
    rh = json.loads(srv.query_forecast(review="require_human"))
    assert all(r["review"] == "require_human" for r in rh["detail"])


def test_query_patterns_and_get(mcp_root):
    out = json.loads(srv.query_patterns(confidence="high"))
    assert out["matched"] == 4 and out["total"] == 12
    pid = out["patterns"][0]["id"]
    full = json.loads(srv.get_pattern(pid))
    assert full["id"] == pid and "evidence" in full
    with pytest.raises(ValueError):
        srv.get_pattern("nope")


def test_validation_findings(mcp_root):
    out = json.loads(srv.validation_findings())
    assert out["checked"] == 4
    assert any(v["key"].get("payee") == "Vendor-4" for v in out["violated"])  # 断缴样例


def test_payments_summary_is_aggregate_only(mcp_root):
    out = json.loads(srv.payments_summary(month="2026-07"))
    assert out["groups"] > 0
    row = out["detail"][0]
    assert set(row) == {"entity", "project", "currency", "month", "total", "n"}  # 无单笔字段
    assert "payee" not in row and "date" not in row


def test_balances_summary(mcp_root):
    out = json.loads(srv.balances_summary())
    assert out["as_of"] == "2026-07-30"
    total = sum(p["balance"] for p in out["positions"])
    assert total == pytest.approx(780000.0)
