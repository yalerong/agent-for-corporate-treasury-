"""头寸/调拨路径（余额 → 4周流出 → 缺口 → 调拨建议）的断言。"""
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
    assert bal["balance"].sum() == 1200000.0


def test_position_table_numbers(pipeline_root):
    report = read_report(pipeline_root)
    assert "## 三、头寸与调拨建议（余额快照 2026-07-30）" in report
    # 币种级头寸 = 余额 − high 置信 4 周预测流出
    assert "| CNY | 560,000 | 151,880 | 408,120 | 富余 |" in report
    assert "| USD | 380,000 | 148,487 | 231,513 | 富余 |" in report
    assert "| SGD | 260,000 | 0 | 260,000 | 富余 |" in report


def test_transfer_suggestion_preview_only(pipeline_root):
    report = read_report(pipeline_root)
    # 主体级: SG Co 的 CNY 余额 60,000，recurring 流出 151,880 → 缺口 91,880；
    # HK Co 同币种富余 → 建议调拨，且必须明示 preview-only
    assert "HK Co → SG Co 91,880 CNY" in report
    assert "preview-only，不生成指令" in report
