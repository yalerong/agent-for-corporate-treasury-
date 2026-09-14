"""头寸/调拨路径（余额 → 4周流出 → 缺口 → 调拨 → 外汇残余）的断言。

合成数据设定：SG Co CNY 余额 60,000、recurring 流出 151,880 → 缺口 91,880；
HK Co CNY 富余仅 80,000 → 调拨吃满捐出方后残余 11,880 转外汇购汇。
"""
import sqlite3

import pandas as pd
import yaml
from pipeline_utils import run_script


def read_report(pipeline_root) -> str:
    return (pipeline_root / "runs" / "2026-07-30" / "report.md").read_text(encoding="utf-8")


def clear_approvals(root) -> None:
    pat = root / "patterns" / "patterns.yaml"
    doc = yaml.safe_load(pat.read_text(encoding="utf-8"))
    for p in doc["patterns"]:
        p["status"] = "candidate"
        p["approved_by"] = None
        p["approved_at"] = None
    pat.write_text(yaml.dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")


def test_balances_ingested(pipeline_root):
    con = sqlite3.connect(pipeline_root / "data" / "db" / "treasury.db")
    bal = pd.read_sql("SELECT * FROM balances", con)
    con.close()
    assert len(bal) == 4
    assert bal["as_of"].max() == "2026-07-30"
    assert bal["balance"].sum() == 780000.0


def test_position_table_numbers(pipeline_root):
    report = read_report(pipeline_root)
    assert "、头寸与调拨建议（余额快照 2026-07-30）" in report  # 不绑节号，节次随可选节增减
    # 币种级头寸 = 余额 − high 置信 4 周预测流出
    assert "| CNY | 140,000 | 151,880 | -11,880 | ⚠️ 缺口 |" in report
    assert "| USD | 380,000 | unknown | unknown | unknown |" in report
    assert "| SGD | 260,000 | unknown | unknown | unknown |" in report


def test_transfer_consumes_donor_and_reports_residual(pipeline_root):
    report = read_report(pipeline_root)
    # 调拨额 = 捐出方可用富余(80,000)而非缺口全额(91,880)
    assert "HK Co → SG Co 80,000 CNY" in report
    assert "preview-only，不生成指令" in report
    # 覆盖不了的残余必须明示并转外汇节
    assert "同币种调拨后仍缺 11,880" in report


def test_fx_uses_residual_not_gross(pipeline_root):
    report = read_report(pipeline_root)
    # CNY 购汇建议只针对余额覆盖后的缺口，不是全额流出
    assert "**CNY**: 未来4周购汇需求 11,880" in report
    # USD 还有未批准覆盖，不能输出余额可覆盖结论
    assert "**USD**: 未来4周预测流出 148,487" not in report
    assert "存在未批准预测行，FX 建议为 unknown" in report


def test_empty_official_forecast_makes_position_and_fx_unknown(iso_root):
    clear_approvals(iso_root)
    run_script("engine.py", iso_root)

    report = read_report(iso_root)
    assert "正式预测为空，头寸与调拨建议为 unknown" in report
    assert "正式预测为空，FX 建议为 unknown" in report
    assert "| USD | 380,000 | 0 | 380,000 | 富余 |" not in report
    assert "现有余额头寸可覆盖，无需购汇" not in report
