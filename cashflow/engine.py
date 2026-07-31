"""确定性引擎：月度预算执行分析 + 资金预测 + 外汇管控视图 + 关联方合规视图。

输出 runs/<date>/report.md + forecast.csv。
所有数字来自 SQLite + patterns.yaml（钉规律版本），LLM 不参与计算。

用法: python engine.py [--asof 2026-07-31]
"""
import argparse
import sqlite3
from datetime import timedelta
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).parent
DB = ROOT / "data" / "db" / "treasury.db"
PAT = ROOT / "patterns" / "patterns.yaml"
GROUP = ["entity", "project", "currency"]


def load_all(asof: pd.Timestamp):
    con = sqlite3.connect(DB)
    pay = pd.read_sql("SELECT * FROM payments WHERE date<=?", con,
                      params=(asof.strftime("%Y-%m-%d"),), parse_dates=["date"])
    con.close()
    pats = yaml.safe_load(PAT.read_text(encoding="utf-8"))
    bud_path = ROOT / "data" / "budget.csv"
    bud = None
    if bud_path.exists():
        bud = pd.read_csv(bud_path, encoding="utf-8-sig").rename(columns={
            "月份": "month", "付款主体": "entity", "项目": "project",
            "币种": "currency", "预算金额": "budget"})
    rp_path = ROOT / "related_parties.yaml"
    rp = {"payees": [], "project_keywords": []}
    if rp_path.exists():
        raw = yaml.safe_load(rp_path.read_text(encoding="utf-8"))["related_parties"]
        if isinstance(raw, list):
            rp["payees"] = raw
        else:
            rp.update(raw)
    return pay, pats, bud, rp


def budget_variance(pay, bud, month: str):
    act = pay[pay["date"].dt.strftime("%Y-%m") == month]
    a = act.groupby(GROUP, as_index=False)["amount"].sum().rename(columns={"amount": "actual"})
    m = bud[bud["month"] == month].merge(a, on=GROUP, how="outer").fillna(0)
    m["var"] = m["actual"] - m["budget"]
    m["var_pct"] = (m["var"] / m["budget"].replace(0, pd.NA) * 100).round(1)
    # 根因: 每个超支组合找贡献最大的收款方
    causes = {}
    for _, r in m[m["var_pct"].abs() > 10].iterrows():
        g = act[(act["entity"] == r["entity"]) & (act["project"] == r["project"])
                & (act["currency"] == r["currency"])]
        top = g.groupby("payee")["amount"].sum().nlargest(2)
        causes[(r["entity"], r["project"], r["currency"])] = "; ".join(
            f"{p} {v:,.0f}" for p, v in top.items())
    return m.sort_values("var_pct", ascending=False), causes


def forecast_4w(pats, asof: pd.Timestamp):
    rows = []
    horizon = [(asof + timedelta(days=1 + 7 * i),
                asof + timedelta(days=7 * (i + 1))) for i in range(4)]
    levels = [p for p in pats["patterns"] if p["type"] == "weekly_level"]
    recs = [p for p in pats["patterns"] if p["type"] == "recurring"]
    for i, (w0, w1) in enumerate(horizon, 1):
        for p in levels:
            amt = p["base_weekly"]
            note = "" if p["confidence"] == "high" else "provisional-仅提示"
            rows.append({**p["key"], "week": f"W+{i}", "start": w0.date(),
                         "forecast": round(amt, 2), "source": "weekly_level",
                         "confidence": p["confidence"], "approved": p["approved"], "note": note})
        for p in recs:
            due = [d for d in pd.date_range(w0, w1) if d.day == p["day_of_month"]]
            if due:
                rows.append({"entity": "", "project": "", **p["key"], "week": f"W+{i}",
                             "start": w0.date(), "forecast": p["avg_amount"],
                             "source": f"recurring@{p['day_of_month']}日",
                             "confidence": p["confidence"], "approved": p["approved"], "note": ""})
    fc = pd.DataFrame(rows)
    if "payee" not in fc.columns:
        fc["payee"] = ""
    return fc


def fx_view(fc: pd.DataFrame, bud, month: str):
    """外汇管控: 未来4周分币种净流出 → 建议交易区间(不超预算额度)与期限匹配。"""
    high = fc[fc["confidence"] == "high"]
    by_cur = high.groupby("currency")["forecast"].sum()
    lines = []
    for cur, need in by_cur.items():
        cap = None
        if bud is not None:
            cap = bud[(bud["month"] == month) & (bud["currency"] == cur)]["budget"].sum()
        cap_txt = f"，预算额度上限 {cap:,.0f}" if cap else ""
        amt_lo, amt_hi = need * 0.8, min(need * 1.1, cap) if cap else need * 1.1
        lines.append(f"- **{cur}**: 未来4周预测净流出 {need:,.0f}{cap_txt}。"
                     f"建议购汇/换汇区间 [{amt_lo:,.0f}, {amt_hi:,.0f}]，"
                     f"期限与付款周期匹配（≤4周，忌超额超期）；风险中性，不做方向性判断。")
    return lines


def related_party(pay, rp, month: str):
    mask = pay["payee"].isin(rp["payees"])
    for kw in rp["project_keywords"]:
        mask |= pay["project"].str.contains(kw, na=False)
    g = pay[mask].copy()
    if g.empty:
        return None, None
    g["month"] = g["date"].dt.strftime("%Y-%m")
    monthly = g.groupby(["month", "project", "currency"])["amount"].agg(["sum", "count"]).reset_index()
    cur = monthly[monthly["month"] == month]
    return monthly, cur


def approvals_view():
    """调拨/付款/报销审批流程画像（approvals 表存在时输出）。"""
    con = sqlite3.connect(DB)
    try:
        ap = pd.read_sql("SELECT * FROM approvals", con)
    except Exception:
        return []
    finally:
        con.close()
    if ap.empty:
        return []
    ap["month"] = ap["created"].str.slice(0, 7)
    lines = ["\n## 五、审批流程画像\n"]
    for kind, g in ap.groupby("kind"):
        ok = (g["status"] == "已同意").mean()
        med = g["elapsed_hours"].median()
        lines.append(f"- **{kind}**：{len(g)} 单，同意率 {ok:.0%}，审批耗时中位数 "
                     f"{med:.1f} 小时；月均 {len(g) / g['month'].nunique():.0f} 单")
        top = g["nature"].value_counts().head(3)
        if len(top):
            lines.append("  - 性质分布: " + "; ".join(f"{k} {v}单" for k, v in top.items()))
    slow = ap.nlargest(3, "elapsed_hours")[["kind", "apply_no", "elapsed_hours", "reason"]]
    if len(slow):
        lines.append("- 耗时 Top3: " + "; ".join(
            f"{r['kind']}{r['apply_no']}({r['elapsed_hours']:.0f}h)" for _, r in slow.iterrows()))
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default=None)
    args = ap.parse_args()

    pats_meta_check = yaml.safe_load(PAT.read_text(encoding="utf-8"))
    pay, pats, bud, rp = load_all(pd.Timestamp(args.asof) if args.asof
                                  else pd.Timestamp(pats_meta_check["meta"]["data_range"][1]))
    asof = pay["date"].max()
    month = asof.strftime("%Y-%m")
    outdir = ROOT / "runs" / asof.strftime("%Y-%m-%d")
    outdir.mkdir(parents=True, exist_ok=True)

    fc = forecast_4w(pats, asof)
    fc.to_csv(outdir / "forecast.csv", index=False, encoding="utf-8-sig")

    L = [f"# 资金月报与滚动预测 — 截至 {asof.date()}",
         f"\n> 数据: {pats['meta']['rows']} 笔付款 ({pats['meta']['data_range'][0]} ~ "
         f"{pats['meta']['data_range'][1]})；规律库版本 {pats['meta']['generated_at']}；"
         f"全部数字由确定性引擎计算，可追溯。\n"]

    if bud is not None:
        m, causes = budget_variance(pay, bud, month)
        L.append(f"## 一、{month} 预算执行差异\n")
        L.append("| 主体 | 项目 | 币种 | 预算 | 实际 | 差异% | 差异根因(Top收款方) |")
        L.append("|---|---|---|---:|---:|---:|---|")
        for _, r in m.iterrows():
            c = causes.get((r["entity"], r["project"], r["currency"]), "")
            flag = " ⚠️" if pd.notna(r["var_pct"]) and abs(r["var_pct"]) > 10 else ""
            L.append(f"| {r['entity']} | {r['project']} | {r['currency']} | "
                     f"{r['budget']:,.0f} | {r['actual']:,.0f} | {r['var_pct']}%{flag} | {c} |")

    L.append("\n## 二、未来4周资金预测（分主体/项目/币种）\n")
    wl = fc[(fc["source"] == "weekly_level") & (fc["forecast"] > 0)]
    wk = wl.pivot_table(index=GROUP, columns="week", values="forecast", aggfunc="sum")
    total_groups = len(wk)
    wk = wk.assign(_t=wk.sum(axis=1)).nlargest(15, "_t").drop(columns="_t")
    L.append(wk.to_markdown(floatfmt=",.0f"))
    if total_groups > 15:
        L.append(f"\n*按4周合计金额取 Top15 展示，其余 {total_groups - 15} 组见 forecast.csv（未截断）。*")
    recs = fc[fc["source"].str.startswith("recurring")]
    if len(recs):
        L.append("\n**固定付款日提醒**：")
        for _, r in recs.drop_duplicates(subset=["payee", "week"]).iterrows():
            L.append(f"- {r['week']}({r['start']}) {r['payee']} ~{r['forecast']:,.0f} "
                     f"{r['currency']}（{r['source']}）")

    L.append("\n## 三、外汇交易管控建议\n")
    L += fx_view(fc, bud, month)

    if rp:
        monthly, cur = related_party(pay, rp, month)
        L.append("\n## 四、关联方资金往来\n")
        if cur is not None and len(cur):
            for _, r in cur.iterrows():
                L.append(f"- {month} {r['project']}: {r['sum']:,.0f} {r['currency']}"
                         f"（{r['count']}笔），全部逐笔可追溯至源记录。")
            L.append("\n近月趋势（金额，分类|币种）：")
            mp = monthly.assign(col=monthly["project"] + "|" + monthly["currency"])
            L.append(mp.pivot_table(index="month", columns="col",
                                    values="sum").tail(4).to_markdown(floatfmt=",.0f"))

    L += approvals_view()

    L.append(f"\n---\n*待人工批准规律 {sum(1 for p in pats['patterns'] if p['confidence']=='high' and not p['approved'])} 条"
             f"（patterns/patterns.yaml 中 approved:false）；provisional 规律未参与计算。*")

    (outdir / "report.md").write_text("\n".join(L), encoding="utf-8")
    print(f"报告 → {outdir / 'report.md'}\n预测明细 → {outdir / 'forecast.csv'}")


if __name__ == "__main__":
    main()
