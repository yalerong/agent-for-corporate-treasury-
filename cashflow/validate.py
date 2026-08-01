"""证据核验：用最近付款核验规律，三态输出 hit/violated/uncertain，写回 evidence。

口径:
  recurring     检查窗口内每个预期日(day_of_month)，±VALIDATE_DAY_TOL 天找该收款方付款：
                找到且金额在均额±VALIDATE_AMT_TOL 内=hit；无付款或金额出带=violated；
                预期日贴着数据末端(±tol 窗口不完整)=uncertain。
  weekly_level  近 4 个完整周，周额落在 base±WEEKLY_BAND_CVS·cv·base 内=hit，否则 violated
                （对照口径与 patterns.py 一致：剔除 recurring 收款方）。
核验范围: confidence==high 或 status==approved 的规律（refuted 除外）。
evidence = {checked_range, hits, misses, uncertain, hit_rate, last_validated, fail_streak}

降级状态机: 连续 DEMOTE_STREAK 次 hit_rate<DEMOTE_HIT_RATE 的 approved 自动降回
candidate（记 demoted_at/demoted_reason，保留 approved_by 审计痕迹）；
**自动路径永不置 refuted**——refuted 只能人为(approve.py refute)。

用法: python validate.py     # 在 patterns.py 之后、engine.py 之前跑；写盘前自动 .bak
"""
import sqlite3

import pandas as pd
import pattern_store as ps
from constants import (
    DEMOTE_HIT_RATE,
    DEMOTE_STREAK,
    VALIDATE_AMT_TOL,
    VALIDATE_DAY_TOL,
    VALIDATE_WINDOW_DAYS,
    WEEKLY_BAND_CVS,
    get_root,
)

ROOT = get_root()
DB = ROOT / "data" / "db" / "treasury.db"
PAT = ROOT / "patterns" / "patterns.yaml"


def validate_recurring(pay: pd.DataFrame, p: dict, asof: pd.Timestamp) -> list[str]:
    """返回窗口内每个预期日的判定列表（hit/violated/uncertain）。"""
    g = pay[(pay["payee"] == p["key"]["payee"]) & (pay["currency"] == p["key"]["currency"])]
    day, avg = p["day_of_month"], p["avg_amount"]
    start = asof - pd.Timedelta(days=VALIDATE_WINDOW_DAYS)
    tol = pd.Timedelta(days=VALIDATE_DAY_TOL)
    outcomes = []
    for m in pd.period_range(start, asof, freq="M"):
        try:
            due = pd.Timestamp(m.year, m.month, day)
        except ValueError:  # 2月30日之类
            continue
        if due < start or due > asof:
            continue
        if due > asof - tol:  # 数据末端窗口不完整，尚无法判定
            outcomes.append("uncertain")
            continue
        w = g[(g["date"] >= due - tol) & (g["date"] <= due + tol)]
        if w.empty:
            outcomes.append("violated")
        else:
            amt = float(w["amount"].sum())
            outcomes.append("hit" if abs(amt - avg) <= VALIDATE_AMT_TOL * avg else "violated")
    return outcomes


def validate_weekly(pay: pd.DataFrame, p: dict, asof: pd.Timestamp,
                    rec_payees: set[str]) -> list[str]:
    """近 4 个完整周的判定列表。对照口径与 patterns.py 一致：剔除 recurring 收款方。"""
    k = p["key"]
    g = pay[(pay["entity"] == k["entity"]) & (pay["project"] == k["project"])
            & (pay["currency"] == k["currency"]) & (~pay["payee"].isin(rec_payees))].copy()
    g["week"] = g["date"].dt.to_period("W-SUN").dt.start_time
    cur_week = asof.to_period("W-SUN").start_time
    weeks = pd.date_range(end=cur_week - pd.Timedelta(days=7), periods=4, freq="7D")
    wk = g.groupby("week")["amount"].sum()
    base, band = p["base_weekly"], WEEKLY_BAND_CVS * p["cv"] * p["base_weekly"]
    return ["hit" if abs(float(wk.get(w, 0.0)) - base) <= band else "violated" for w in weeks]


def run(doc: dict, pay: pd.DataFrame, asof: pd.Timestamp) -> dict:
    """核验并原地更新 doc 的 evidence/降级状态。返回摘要 {checked, violated, demoted}。"""
    pats = doc["patterns"]
    rec_payees = {p["key"]["payee"] for p in pats if p["type"] == "recurring"}
    now = ps.now_iso()
    start = (asof - pd.Timedelta(days=VALIDATE_WINDOW_DAYS)).date()
    summary = {"checked": 0, "violated": [], "demoted": []}
    for p in pats:
        if ps.status_of(p) == "refuted":
            continue
        if p["confidence"] != "high" and ps.status_of(p) != "approved":
            continue
        if p["type"] == "recurring":
            outcomes = validate_recurring(pay, p, asof)
        elif p["type"] == "weekly_level":
            outcomes = validate_weekly(pay, p, asof, rec_payees)
        else:
            continue
        hits = outcomes.count("hit")
        misses = outcomes.count("violated")
        unc = outcomes.count("uncertain")
        rate = round(hits / (hits + misses), 3) if hits + misses else None
        streak = (p.get("evidence") or {}).get("fail_streak", 0)
        if rate is not None:
            streak = streak + 1 if rate < DEMOTE_HIT_RATE else 0
        p["evidence"] = {"checked_range": [str(start), str(asof.date())],
                         "hits": hits, "misses": misses, "uncertain": unc,
                         "hit_rate": rate, "last_validated": now, "fail_streak": streak}
        summary["checked"] += 1
        if misses:
            summary["violated"].append(p)
        # 自动降级只到 candidate，永不 refuted；approved_by/at 保留作审计痕迹
        if ps.status_of(p) == "approved" and streak >= DEMOTE_STREAK:
            p["status"] = "candidate"
            p["demoted_at"] = now
            p["demoted_reason"] = (f"连续{streak}次核验 hit_rate<{DEMOTE_HIT_RATE}"
                                   f"（自动降级，人工复核后可重批）")
            summary["demoted"].append(p)
    return summary


def main():
    if not PAT.exists():
        raise SystemExit(f"{PAT} 不存在，先跑 patterns.py")
    doc = ps.load(PAT)
    if doc["meta"].get("schema_version", 1) < ps.SCHEMA_VERSION:
        raise SystemExit("patterns.yaml 还是 schema v1，先跑 python migrate_patterns.py")
    con = sqlite3.connect(DB)
    pay = pd.read_sql("SELECT * FROM payments", con, parse_dates=["date"])
    con.close()
    if pay.empty:
        raise SystemExit("payments 表为空，先跑 ingest.py")
    summary = run(doc, pay, pay["date"].max())
    ps.save(PAT, doc, backup=True)
    print(f"核验 {summary['checked']} 条 → {PAT}")
    for p in summary["violated"]:
        ev = p["evidence"]
        print(f"  [violated] {p['id']} {p['type']} {p['key']} :: "
              f"hit {ev['hits']} / violated {ev['misses']}（hit_rate {ev['hit_rate']}）")
    for p in summary["demoted"]:
        print(f"  [demoted ] {p['id']} {p['claim']} :: {p['demoted_reason']}")


if __name__ == "__main__":
    main()
