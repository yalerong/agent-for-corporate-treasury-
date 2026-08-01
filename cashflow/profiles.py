"""确定性数据摘要层：把付款流水压成分组聚合 profile，供 LLM 归纳环消费。

**绝不含单笔流水**——只有周序列统计量、月内日历直方图、Top 收款方聚合额。
payee 可选代号化（anonymize：LLM 只见 P1/P2...，真实名与代号的映射不出本进程）。

用法: python profiles.py [--out profiles.json] [--anonymize]
"""
import argparse
import json
import sqlite3

import pandas as pd
from constants import GROUP, get_root

ROOT = get_root()
DB = ROOT / "data" / "db" / "treasury.db"


def _weekly_stats(g: pd.DataFrame, all_weeks: pd.DatetimeIndex) -> dict:
    s = (g.groupby("week")["amount"].sum().reindex(all_weeks, fill_value=0.0))
    mean = float(s.mean())
    return {"weeks": int(len(s)), "mean": round(mean, 2), "std": round(float(s.std()), 2),
            "cv": round(float(s.std() / mean), 2) if mean > 0 else None,
            "p50": round(float(s.quantile(0.5)), 2), "p90": round(float(s.quantile(0.9)), 2),
            "last4_mean": round(float(s.tail(4).mean()), 2),
            "prev4_mean": round(float(s.iloc[-8:-4].mean()), 2) if len(s) >= 8 else None}


def build_profiles(pay: pd.DataFrame, top_n: int = 8, anonymize: bool = False) -> list[dict]:
    """按 GROUP(主体×项目×币种) 输出聚合 profile 列表。只做聚合，不带任何单笔记录。"""
    df = pay.copy()
    df["week"] = df["date"].dt.to_period("W-SUN").dt.start_time
    df["dom"] = df["date"].dt.day
    all_weeks = pd.date_range(df["week"].min(), df["week"].max(), freq="7D")
    profiles = []
    for keys, g in df.groupby(GROUP):
        by_dom = g.groupby("dom")["amount"].sum()
        dom_share = {str(int(d)): round(float(v / by_dom.sum()), 3)
                     for d, v in by_dom.items() if v / by_dom.sum() >= 0.05}
        top = g.groupby("payee")["amount"].agg(["sum", "count"]).nlargest(top_n, "sum")
        total = float(g["amount"].sum())
        payees = []
        for i, (name, r) in enumerate(top.iterrows(), 1):
            payees.append({"payee": f"P{i}" if anonymize else str(name),
                           "total": round(float(r["sum"]), 2), "n": int(r["count"]),
                           "share": round(float(r["sum"] / total), 3)})
        profiles.append({**dict(zip(GROUP, keys, strict=True)),
                         "months": int(g["date"].dt.to_period("M").nunique()),
                         "n_payments": int(len(g)), "total": round(total, 2),
                         "weekly": _weekly_stats(g, all_weeks),
                         "dom_share": dom_share, "top_payees": payees})
    return profiles


def resolve_field(profile: dict, path: str):
    """按点路径取 profile 里的数值（如 weekly.mean / dom_share.15 / top_payees.0.share）。
    路径不存在或非数值返回 None。"""
    cur = profile
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        else:
            return None
    return float(cur) if isinstance(cur, int | float) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="输出 json 路径（默认打印 stdout）")
    ap.add_argument("--anonymize", action="store_true", help="payee 代号化（P1/P2...）")
    args = ap.parse_args()
    con = sqlite3.connect(DB)
    pay = pd.read_sql("SELECT * FROM payments", con, parse_dates=["date"])
    con.close()
    if pay.empty:
        raise SystemExit("payments 表为空，先跑 ingest.py")
    profiles = build_profiles(pay, anonymize=args.anonymize)
    text = json.dumps(profiles, ensure_ascii=False, indent=2)
    if args.out:
        (ROOT / args.out).write_text(text, encoding="utf-8")
        print(f"{len(profiles)} 组 profile → {ROOT / args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
