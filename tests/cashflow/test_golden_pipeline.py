"""golden 回归：合成数据（种子固定）跑全链路，对规律库/预测/报告做精确断言。

引擎是确定性的——数字必须精确复现；任何阈值/口径改动都应让这里变红，
变红后人工确认是"有意变更"才允许更新 golden。
"""
import re
from pathlib import Path

import pandas as pd
import pytest
import yaml
from pipeline_utils import run_script

GOLDEN = Path(__file__).parent / "golden"

TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00")


def normalize(text: str) -> str:
    return TS_RE.sub("<TS>", text)


def load_patterns(root: Path) -> dict:
    return yaml.safe_load((root / "patterns" / "patterns.yaml").read_text(encoding="utf-8"))


def by_key(pats: dict, type_: str, **key) -> dict:
    hits = [p for p in pats["patterns"] if p["type"] == type_ and p["key"] == key]
    assert len(hits) == 1, f"{type_} {key} 命中 {len(hits)} 条"
    return hits[0]


# ---------- 规律库 ----------

def test_pattern_counts(pipeline_root):
    pats = load_patterns(pipeline_root)
    assert pats["meta"]["rows"] == 245
    assert pats["meta"]["data_range"] == ["2026-01-02", "2026-07-30"]
    types = [p["type"] for p in pats["patterns"]]
    assert types.count("weekly_level") == 3
    assert types.count("recurring") == 6
    assert types.count("dom_profile") == 3
    confs = [p["confidence"] for p in pats["patterns"]]
    assert confs.count("high") == 4
    assert confs.count("provisional") == 8


def test_recurring_exact_values(pipeline_root):
    pats = load_patterns(pipeline_root)
    adp = by_key(pats, "recurring", payee="ADP Payroll", currency="USD")
    assert adp["day_of_month"] == 15
    assert adp["avg_amount"] == 118669.86
    assert adp["sample_n"] == 7
    assert adp["confidence"] == "high"
    landlord = by_key(pats, "recurring", payee="Landlord Ltd", currency="USD")
    assert landlord["day_of_month"] == 5
    assert landlord["avg_amount"] == 18000.0
    sz = by_key(pats, "recurring", payee="深圳子公司(关联方)", currency="CNY")
    assert sz["day_of_month"] == 10
    assert sz["avg_amount"] == 151879.68
    assert sz["confidence"] == "high"


def test_weekly_level_exact_values(pipeline_root):
    pats = load_patterns(pipeline_root)
    hk = by_key(pats, "weekly_level", entity="HK Co", project="运营", currency="USD")
    assert hk["base_weekly"] == 23035.05
    assert hk["cv"] == 0.54
    assert hk["sample_weeks"] == 31
    sg = by_key(pats, "weekly_level", entity="SG Co", project="运营", currency="SGD")
    assert sg["base_weekly"] == 10758.35


def test_dom_profile_values(pipeline_root):
    pats = load_patterns(pipeline_root)
    hk = by_key(pats, "dom_profile", entity="HK Co", currency="USD")
    assert hk["hot_days"] == {15: 0.304}  # 发薪日集中


# ---------- 预测 ----------

def test_forecast_csv(pipeline_root):
    runs = list((pipeline_root / "runs").iterdir())
    assert len(runs) == 1 and runs[0].name == "2026-07-30"
    fc = pd.read_csv(runs[0] / "forecast.csv", encoding="utf-8-sig")
    wl = fc[fc["source"] == "weekly_level"]
    assert len(wl) == 12  # 3 组 × 4 周
    rec = fc[fc["source"].str.startswith("recurring")]
    assert len(rec) == 5
    assert wl["forecast"].sum() == pytest.approx(160339.64, abs=0.01)
    assert rec["forecast"].sum() == pytest.approx(388592.64, abs=0.01)
    # 外汇视图口径：high 置信的分币种合计
    high = fc[fc["confidence"] == "high"]
    assert high.groupby("currency")["forecast"].sum().to_dict() == pytest.approx(
        {"CNY": 151879.68, "USD": 148487.24}, abs=0.01)


# ---------- 报告快照 ----------

def test_report_golden_snapshot(pipeline_root):
    report = (pipeline_root / "runs" / "2026-07-30" / "report.md").read_text(encoding="utf-8")
    expected = (GOLDEN / "report_expected.md").read_text(encoding="utf-8")
    assert normalize(report) == normalize(expected)


# ---------- 确定性与 approved 保留 ----------

def test_rerun_is_deterministic(pipeline_root):
    before_p = normalize(
        (pipeline_root / "patterns" / "patterns.yaml").read_text(encoding="utf-8"))
    before_r = normalize(
        (pipeline_root / "runs" / "2026-07-30" / "report.md").read_text(encoding="utf-8"))
    run_script("patterns.py", pipeline_root)
    run_script("engine.py", pipeline_root)
    after_p = normalize(
        (pipeline_root / "patterns" / "patterns.yaml").read_text(encoding="utf-8"))
    after_r = normalize(
        (pipeline_root / "runs" / "2026-07-30" / "report.md").read_text(encoding="utf-8"))
    assert before_p == after_p
    assert before_r == after_r


def test_approved_survives_rerun(pipeline_root):
    pat_file = pipeline_root / "patterns" / "patterns.yaml"
    pats = yaml.safe_load(pat_file.read_text(encoding="utf-8"))
    for p in pats["patterns"]:
        if p["type"] == "recurring" and p["key"]["payee"] == "ADP Payroll":
            p["approved"] = True
    pat_file.write_text(yaml.dump(pats, allow_unicode=True, sort_keys=False), encoding="utf-8")
    run_script("patterns.py", pipeline_root)
    pats2 = load_patterns(pipeline_root)
    adp = by_key(pats2, "recurring", payee="ADP Payroll", currency="USD")
    assert adp["approved"] is True
    landlord = by_key(pats2, "recurring", payee="Landlord Ltd", currency="USD")
    assert landlord["approved"] is False
