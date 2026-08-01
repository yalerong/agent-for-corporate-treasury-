"""学习环：从 payments 历史提炼规律，写入 patterns/patterns.yaml。

规律类型:
  weekly_level    每个 主体+项目+币种 的周度付款水平（近4周截尾均值/波动/趋势）
  recurring       固定节奏的收款方（月度固定日 / 周度固定）
  dom_profile     日历规律：月内哪些日子集中付款（发薪/税期/月结）

每条规律带证据(样本周数/变异系数)与置信度:
  high        样本>=8周 且 变异系数<0.5   -> 可进 engine 计算
  provisional 其余                        -> 只作提示，不进计算
人工审批(schema v2 三态): status=candidate/approved/refuted，用 approve.py 批准/否决;
重算按 pattern_id 继承人工状态(refuted 不复活)。strict 门控见 engine.py --strict-approval。
"""
import sqlite3
from datetime import UTC, datetime

import pandas as pd
import pattern_store
from constants import GROUP, get_root

ROOT = get_root()
DB = ROOT / "data" / "db" / "treasury.db"
OUT = ROOT / "patterns" / "patterns.yaml"


def load() -> pd.DataFrame:
    con = sqlite3.connect(DB)
    df = pd.read_sql("SELECT * FROM payments", con, parse_dates=["date"])
    con.close()
    if df.empty:
        raise SystemExit("payments 表为空，先跑 ingest.py")
    df["week"] = df["date"].dt.to_period("W-SUN").dt.start_time  # 周一为界
    df["dom"] = df["date"].dt.day
    return df


def weekly_level(df: pd.DataFrame) -> list[dict]:
    out = []
    all_weeks = pd.date_range(df["week"].min(), df["week"].max(), freq="7D")
    wk = df.groupby(GROUP + ["week"], as_index=False)["amount"].sum()
    for keys, g in wk.groupby(GROUP):
        # 无付款的周计为 0，否则稀疏付款组的周度基准会被严重高估
        s = g.set_index("week")["amount"].reindex(all_weeks, fill_value=0.0)
        n = len(s)
        if n < 3:
            continue
        recent = s.tail(4)
        base = float(recent.drop(recent.idxmax()).mean()) if len(recent) >= 3 else float(recent.mean())
        cv = float(s.std() / s.mean()) if s.mean() > 0 else 99.0
        trend = float(s.tail(4).mean() / s.head(4).mean()) if n >= 8 and s.head(4).mean() > 0 else 1.0
        conf = "high" if n >= 8 and cv < 0.5 else "provisional"
        out.append({
            "type": "weekly_level",
            "key": dict(zip(GROUP, keys, strict=True)),
            "claim": f"周度付款基准 {base:,.0f}（{n}周样本, CV={cv:.2f}, 近4周/前4周={trend:.2f}）",
            "base_weekly": round(base, 2), "cv": round(cv, 2),
            "trend": round(trend, 2), "sample_weeks": n,
            "confidence": conf,
        })
    return out


def recurring(df: pd.DataFrame) -> list[dict]:
    out = []
    for (payee, cur), g in df.groupby(["payee", "currency"]):
        if not payee or len(g) < 3:
            continue
        months = g["date"].dt.to_period("M").nunique()
        if months >= 3 and len(g) / months <= 1.5:  # ~每月一笔
            dom_std = float(g["dom"].std())
            if dom_std <= 4:
                out.append({
                    "type": "recurring",
                    "key": {"payee": payee, "currency": cur},
                    "claim": f"月度固定付款, 约每月{int(g['dom'].median())}日, "
                             f"均额 {g['amount'].mean():,.0f}（{len(g)}笔/{months}月）",
                    "day_of_month": int(g["dom"].median()),
                    "avg_amount": round(float(g["amount"].mean()), 2),
                    "sample_n": len(g),
                    "confidence": "high" if len(g) >= 5 else "provisional",
                })
    return out


def dom_profile(df: pd.DataFrame) -> list[dict]:
    out = []
    for (ent, cur), g in df.groupby(["entity", "currency"]):
        if g["date"].dt.to_period("M").nunique() < 3:
            continue
        by_dom = g.groupby("dom")["amount"].sum()
        share = by_dom / by_dom.sum()
        hot = share[share > 0.15]
        if len(hot):
            days = ", ".join(f"{d}日({p:.0%})" for d, p in hot.items())
            out.append({
                "type": "dom_profile",
                "key": {"entity": ent, "currency": cur},
                "claim": f"月内付款集中日: {days}",
                "hot_days": {int(d): round(float(p), 3) for d, p in hot.items()},
                "confidence": "provisional",
            })
    return out


def main():
    df = load()
    recs = recurring(df)
    # 固定节奏付款单列为 recurring，从周度基线中剔除，避免预测时重复计数
    rec_payees = {p["key"]["payee"] for p in recs}
    pats = weekly_level(df[~df["payee"].isin(rec_payees)]) + recs + dom_profile(df)
    meta = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "data_range": [str(df["date"].min().date()), str(df["date"].max().date())],
        "rows": len(df),
    }
    # 人工状态(approved/refuted/审计字段)按 pattern_id 从老库继承——自动重算永不改人工状态
    old = pattern_store.load(OUT) if OUT.exists() else None
    pats = pattern_store.merge_states(pats, old, default_valid_from=meta["data_range"][1])
    pattern_store.save(OUT, {"meta": meta, "patterns": pats})
    hi = sum(1 for p in pats if p["confidence"] == "high")
    print(f"提炼规律 {len(pats)} 条（high {hi} / provisional {len(pats)-hi}）→ {OUT}")
    for p in pats:
        print(f"  [{p['confidence']:<11}] [{p.get('status', 'candidate'):<9}] "
              f"{p['type']:<12} {p['key']} :: {p['claim']}")


if __name__ == "__main__":
    main()
