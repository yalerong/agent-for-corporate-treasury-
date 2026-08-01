"""头寸/调拨路径（余额 → 4周流出 → 缺口 → 调拨 → 外汇残余）的断言。

合成数据设定：SG Co CNY 余额 60,000、recurring 流出 151,880 → 缺口 91,880；
HK Co CNY 富余仅 80,000 → 调拨吃满捐出方后残余 11,880 转外汇购汇。
"""
import sqlite3

import pandas as pd


def read_report(pipeline_root) -> str:
    return (pipeline_root / "runs" / "2026-07-30" / "report.md").read_text(encoding="utf-8")


def test_balances_ingested(pipeline_root):
    con = sqlite3.connect(pipeline_root / "data" / "db" / "treasury.db")
    bal = pd.read_sql("SELECT * FROM balances", con)
    con.close()
    assert len(bal) == 4
    assert bal["as_of"].max() == "2026-07-30"
    assert bal["balance"].sum() == 780000.0


def test_position_table_numbers(pipeline_root):
    report = read_report(pipeline_root)
    assert "## 三、头寸与调拨建议（余额快照 2026-07-30）" in report
    # 币种级头寸 = 余额 − high 置信 4 周预测流出
    assert "| CNY | 140,000 | 151,880 | -11,880 | ⚠️ 缺口 |" in report
    assert "| USD | 380,000 | 148,487 | 231,513 | 富余 |" in report
    assert "| SGD | 260,000 | 0 | 260,000 | 富余 |" in report


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
    # USD 余额可覆盖 → 不给购汇区间
    assert "**USD**: 未来4周预测流出 148,487，现有余额头寸可覆盖，无需购汇。" in report
