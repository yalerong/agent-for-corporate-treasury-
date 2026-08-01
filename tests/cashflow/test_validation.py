"""PR4 核验+归因：三态判定、降级状态机（永不自动 refuted）、贡献分解手算、日历对齐。"""
import attribution
import pandas as pd
import pattern_store as ps
import validate
import yaml

COLS = ["date", "entity", "project", "currency", "payee", "amount"]


def pay_df(rows: list[tuple]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=COLS)
    df["date"] = pd.to_datetime(df["date"])
    return df


def empty_pay() -> pd.DataFrame:
    return pd.DataFrame(columns=COLS).astype({"date": "datetime64[ns]", "amount": float})


def rec_pattern(status="candidate", avg=100000.0, day=15) -> dict:
    return ps.upgrade_pattern({
        "type": "recurring", "key": {"payee": "X", "currency": "USD"},
        "claim": "月度固定", "day_of_month": day, "avg_amount": avg,
        "sample_n": 6, "confidence": "high", "status": status})


# ---------- recurring 三态判定 ----------

def test_recurring_hit_then_missing_is_violated():
    # 窗口(70天)含 6/15、7/15 两个完整预期日：6月正常、7月断缴
    pay = pay_df([("2026-06-15", "A", "P", "USD", "X", 100000.0)])
    out = validate.validate_recurring(pay, rec_pattern(), pd.Timestamp("2026-07-30"))
    assert out == ["hit", "violated"]


def test_recurring_amount_band():
    asof = pd.Timestamp("2026-07-01")  # 窗口只含 5/15、6/15
    ok = pay_df([("2026-05-15", "A", "P", "USD", "X", 130000.0),   # 偏差30% → hit
                 ("2026-06-15", "A", "P", "USD", "X", 150000.0)])  # 偏差50% → violated
    out = validate.validate_recurring(ok, rec_pattern(), asof)
    assert out == ["hit", "violated"]


def test_recurring_uncertain_at_data_edge():
    # 预期日 7/15 距 asof 7/16 不足 ±3 天窗口 → uncertain，不冤枉也不放过
    out = validate.validate_recurring(empty_pay(), rec_pattern(), pd.Timestamp("2026-07-16"))
    assert out[-1] == "uncertain"
    assert set(out[:-1]) == {"violated"}


# ---------- weekly 带宽判定 ----------

def test_weekly_band():
    p = ps.upgrade_pattern({
        "type": "weekly_level", "key": {"entity": "A", "project": "P", "currency": "USD"},
        "claim": "周度", "base_weekly": 10000.0, "cv": 0.2, "trend": 1.0,
        "sample_weeks": 10, "confidence": "high"})
    rows = []
    # 近4个完整周（asof=周四 7/30，完整周为 6/29、7/6、7/13、7/20 起的四周）
    for d, amt in (("2026-06-29", 9000.0), ("2026-07-06", 11000.0),
                   ("2026-07-13", 30000.0), ("2026-07-20", 10000.0)):  # 第3周爆表
        rows.append((d, "A", "P", "USD", "V", amt))
    out = validate.validate_weekly(pay_df(rows), p, pd.Timestamp("2026-07-30"), set())
    assert out == ["hit", "hit", "violated", "hit"]  # 带宽=±2·0.2·10000=±4000


def test_weekly_excludes_recurring_payees():
    p = ps.upgrade_pattern({
        "type": "weekly_level", "key": {"entity": "A", "project": "P", "currency": "USD"},
        "claim": "周度", "base_weekly": 1000.0, "cv": 0.2, "trend": 1.0,
        "sample_weeks": 10, "confidence": "high"})
    rows = [(d, "A", "P", "USD", "小额", 1000.0)
            for d in ("2026-06-29", "2026-07-06", "2026-07-13", "2026-07-20")]
    rows += [("2026-07-14", "A", "P", "USD", "X", 100000.0)]  # X 是 recurring，应剔除
    out = validate.validate_weekly(pay_df(rows), p, pd.Timestamp("2026-07-30"), {"X"})
    assert out == ["hit"] * 4


# ---------- 降级状态机 ----------

def test_demotion_two_strikes_and_never_refuted():
    doc = {"meta": {"schema_version": 2, "generated_at": "t",
                    "data_range": ["2026-01-01", "2026-07-30"], "rows": 1},
           "patterns": [rec_pattern(status="approved")]}
    asof = pd.Timestamp("2026-07-30")
    validate.run(doc, empty_pay(), asof)  # 第一次失败：记 streak，不降级
    p = doc["patterns"][0]
    assert p["evidence"]["fail_streak"] == 1
    assert p["status"] == "approved"
    s2 = validate.run(doc, empty_pay(), asof)  # 连续第二次：降回 candidate
    assert p["status"] == "candidate"
    assert p["demoted_at"] and "自动降级" in p["demoted_reason"]
    assert "approved_by" in p  # 审计痕迹字段保留，不被降级抹掉
    assert len(s2["demoted"]) == 1
    validate.run(doc, empty_pay(), asof)  # 再跑不会变 refuted
    assert p["status"] == "candidate"


def test_hit_resets_streak():
    doc = {"meta": {"schema_version": 2, "generated_at": "t",
                    "data_range": ["2026-01-01", "2026-07-30"], "rows": 1},
           "patterns": [rec_pattern(status="approved")]}
    asof = pd.Timestamp("2026-07-30")
    validate.run(doc, empty_pay(), asof)
    assert doc["patterns"][0]["evidence"]["fail_streak"] == 1
    good = pay_df([("2026-06-15", "A", "P", "USD", "X", 100000.0),
                   ("2026-07-15", "A", "P", "USD", "X", 100000.0)])
    validate.run(doc, good, asof)  # 全 hit → 归零，不降级
    p = doc["patterns"][0]
    assert p["evidence"]["fail_streak"] == 0
    assert p["status"] == "approved"


# ---------- 归因两函数 ----------

def test_contrib_manual():
    cur = pay_df([("2026-07-01", "A", "P", "USD", "甲", 100.0),
                  ("2026-07-02", "A", "P", "USD", "乙", 50.0)])
    prev = pay_df([("2026-06-01", "A", "P", "USD", "甲", 40.0),
                   ("2026-06-02", "A", "P", "USD", "丙", 30.0)])
    out = attribution.contrib(cur, prev, ["payee"])
    assert list(out["payee"]) == ["甲", "乙", "丙"]  # |delta| 60 > 50 > 30
    assert list(out["delta"]) == [60.0, 50.0, -30.0]
    assert list(out["cur"]) == [100.0, 50.0, 0.0]


def test_calendar_align():
    aligned = pay_df([("2026-07-15", "A", "P", "USD", "X", 90.0),
                      ("2026-07-03", "A", "P", "USD", "X", 10.0)])
    assert attribution.calendar_align(aligned, {15}, "2026-07") is True   # 90% 在发薪日
    spread = pay_df([("2026-07-03", "A", "P", "USD", "X", 50.0),
                     ("2026-07-08", "A", "P", "USD", "X", 50.0)])
    assert attribution.calendar_align(spread, {15}, "2026-07") is False
    month_end = pay_df([("2026-07-30", "A", "P", "USD", "X", 100.0)])
    assert attribution.calendar_align(month_end, set(), "2026-07") is True  # 月末最后3天


# ---------- 全链路集成 ----------

def test_pipeline_evidence_and_report_sections(pipeline_root):
    doc = yaml.safe_load(
        (pipeline_root / "patterns" / "patterns.yaml").read_text(encoding="utf-8"))
    highs = [p for p in doc["patterns"] if p["confidence"] == "high"]
    assert highs and all((p.get("evidence") or {}).get("last_validated") for p in highs)
    rep = (pipeline_root / "runs" / "2026-07-30" / "report.md").read_text(encoding="utf-8")
    assert "规律核验（人审清单）" in rep
    assert "当月异动归因" in rep
