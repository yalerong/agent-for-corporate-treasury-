"""审批 UI（cashflow/ui.py）：页面渲染与批准/否决动作（与 CLI 共用同一套语义）。

写操作用例在 pipeline_root 的隔离副本上跑，通过 CASHFLOW_ROOT 注入（ui 的路径每请求现算）。
"""
import shutil
import tempfile
from pathlib import Path

import pytest
import ui
import yaml
from fastapi.testclient import TestClient

client = TestClient(ui.app)


@pytest.fixture()
def ui_root(pipeline_root, monkeypatch) -> Path:
    dst = Path(tempfile.mkdtemp(prefix="treasury_ui_"))
    shutil.copytree(pipeline_root, dst, dirs_exist_ok=True)
    monkeypatch.setenv("CASHFLOW_ROOT", str(dst))
    yield dst
    shutil.rmtree(dst, ignore_errors=True)


def load_doc(root: Path) -> dict:
    return yaml.safe_load((root / "patterns" / "patterns.yaml").read_text(encoding="utf-8"))


def adp_id(root: Path) -> str:
    return next(p["id"] for p in load_doc(root)["patterns"]
                if p["type"] == "recurring" and p["key"].get("payee") == "ADP Payroll")


def test_report_page_renders(ui_root):
    r = client.get("/")
    assert r.status_code == 200
    assert "资金月报与滚动预测" in r.text
    assert "头寸与调拨建议" in r.text
    assert "<!-- metric" not in r.text  # 注释不进画面
    assert "## " not in r.text          # markdown 已渲染成 HTML


def test_patterns_page_lists_and_filters(ui_root):
    r = client.get("/patterns")
    assert r.status_code == 200
    assert adp_id(ui_root) in r.text
    r2 = client.get("/patterns", params={"confidence": "high"})
    assert r2.text.count("badge b-provisional") == 0  # 行内徽章为 0（内嵌 CSS 定义不算）


def test_approve_via_ui(ui_root):
    pid = adp_id(ui_root)
    r = client.post("/patterns/action",
                    data={"action": "approve", "by": "tester", "ids": [pid]},
                    follow_redirects=False)
    assert r.status_code == 303
    p = next(x for x in load_doc(ui_root)["patterns"] if x["id"] == pid)
    assert p["status"] == "approved"
    assert p["approved_by"] == "tester"
    assert (ui_root / "patterns" / "patterns.yaml.bak").exists()


def test_refute_requires_reason(ui_root):
    pid = adp_id(ui_root)
    r = client.post("/patterns/action",
                    data={"action": "refute", "by": "tester", "ids": [pid]},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "必须" in r.headers["location"] or "%E5%BF%85%E9%A1%BB" in r.headers["location"]
    assert next(x for x in load_doc(ui_root)["patterns"]
                if x["id"] == pid)["status"] != "refuted"

    r2 = client.post("/patterns/action",
                     data={"action": "refute", "by": "tester", "reason": "UI 测试否决",
                           "ids": [pid]},
                     follow_redirects=False)
    assert r2.status_code == 303
    p = next(x for x in load_doc(ui_root)["patterns"] if x["id"] == pid)
    assert p["status"] == "refuted"
    assert "UI 测试否决" in p["refuted_reason"]


def test_forecast_page_filters(ui_root):
    r = client.get("/forecast", params={"review": "require_human"})
    assert r.status_code == 200
    assert "深圳子公司(关联方)" in r.text
    assert "auto_report" not in r.text.split("</form>")[-1]  # 表格区不含其它档
