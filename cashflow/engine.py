"""确定性引擎：月度预算执行分析 + 资金预测 + 外汇管控视图 + 关联方合规视图。

输出 runs/<date>/report.md + forecast.csv。
所有数字来自 SQLite + patterns.yaml（钉规律版本），LLM 不参与计算。

审批门控: --strict-approval 下计算只用人工批准(status=approved)的规律（本 PR 默认关）；
approved 为 0 时自动回退按置信度计算并在报告顶部标注"过渡模式"（防空报告）。
refuted 规律任何模式都不参与计算与预测。

用法: python engine.py [--asof 2026-07-31] [--strict-approval]
"""
import argparse
import sqlite3
from collections import Counter
from datetime import timedelta

import pandas as pd
import yaml
from constants import BUDGET_EXCLUDE, GROUP, get_root

ROOT = get_root()
DB = ROOT / "data" / "db" / "treasury.db"
PAT = ROOT / "patterns" / "patterns.yaml"


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


def comparable_actuals(act: pd.DataFrame, agg_mode: bool) -> pd.DataFrame:
    """预算可比口径：两种预算粒度统一剔除内部资金移动项目（BUDGET_EXCLUDE）。
    流水性质=="流程支出" 过滤仅在聚合预算(真实流水)口径下做——明细预算走的
    合成/历史数据 purpose 无"流水性质"段，过滤会误杀全部实际。"""
    act = act[~act["project"].isin(BUDGET_EXCLUDE)]
    if agg_mode:
        act = act[act["purpose"].str.split("|").str[-1] == "流程支出"]
    return act


def budget_variance(pay, bud, month: str):
    # 预算是聚合口径（entity/project=全部）时，按同粒度对比实际
    agg_mode = (bud["entity"] == "全部").all()
    keys = ["currency"] if agg_mode else GROUP
    act = pay[pay["date"].dt.strftime("%Y-%m") == month]
    act = comparable_actuals(act, agg_mode)
    a = act.groupby(keys, as_index=False)["amount"].sum().rename(columns={"amount": "actual"})
    b = bud[bud["month"] == month].groupby(keys, as_index=False)["budget"].sum()
    m = b.merge(a, on=keys, how="outer").fillna(0)
    if agg_mode:
        m["entity"], m["project"] = "全部", "全部"
    m["var"] = m["actual"] - m["budget"]
    m["var_pct"] = (m["var"] / m["budget"].replace(0, float("nan")) * 100).astype(float).round(1)
    # 根因: 每个超支组合找贡献最大的收款方/分类
    causes = {}
    for _, r in m[m["var_pct"].abs() > 10].iterrows():
        g = act[act["currency"] == r["currency"]] if agg_mode else act[
            (act["entity"] == r["entity"]) & (act["project"] == r["project"])
            & (act["currency"] == r["currency"])]
        by = "project" if agg_mode else "payee"
        top = g.groupby(by)["amount"].sum().nlargest(2)
        causes[(r["entity"], r["project"], r["currency"])] = "; ".join(
            f"{p} {v:,.0f}" for p, v in top.items())
    return m.sort_values("var_pct", ascending=False), causes


def status_of(p: dict) -> str:
    """三态状态：v2 读 status；未迁移的 v1 文件按 approved 布尔映射，engine 不因此崩。"""
    return p.get("status") or ("approved" if p.get("approved") else "candidate")


def forecast_4w(pats, asof: pd.Timestamp):
    # refuted（人工否决）的规律不进任何预测行
    usable = [p for p in pats["patterns"] if status_of(p) != "refuted"]
    rows = []
    horizon = [(asof + timedelta(days=1 + 7 * i),
                asof + timedelta(days=7 * (i + 1))) for i in range(4)]
    levels = [p for p in usable if p["type"] == "weekly_level"]
    recs = [p for p in usable if p["type"] == "recurring"]
    for i, (w0, w1) in enumerate(horizon, 1):
        for p in levels:
            amt = p["base_weekly"]
            note = "" if p["confidence"] == "high" else "provisional-仅提示"
            rows.append({**p["key"], "week": f"W+{i}", "start": w0.date(),
                         "forecast": round(amt, 2), "source": "weekly_level",
                         "confidence": p["confidence"], "status": status_of(p),
                         "approved": status_of(p) == "approved", "note": note})
        for p in recs:
            due = [d for d in pd.date_range(w0, w1) if d.day == p["day_of_month"]]
            if due:
                rows.append({"entity": "", "project": "", **p["key"], "week": f"W+{i}",
                             "start": w0.date(), "forecast": p["avg_amount"],
                             "source": f"recurring@{p['day_of_month']}日",
                             "confidence": p["confidence"], "status": status_of(p),
                             "approved": status_of(p) == "approved", "note": ""})
    fc = pd.DataFrame(rows)
    if "payee" not in fc.columns:
        fc["payee"] = ""
    return fc


def load_balances(asof: pd.Timestamp):
    """余额快照：只取 as_of<=报告日 的快照（防前视），每账户取其中最新一天。

    空银行/空账号不丢行：填空串分组；同键同快照日的多行求和（无法区分的并列账户）。
    balances 表不存在/为空时返回 None。
    """
    con = sqlite3.connect(DB)
    try:
        bal = pd.read_sql("SELECT * FROM balances WHERE as_of<=?", con,
                          params=(asof.strftime("%Y-%m-%d"),))
    except Exception:
        return None
    finally:
        con.close()
    if bal.empty:
        return None
    key = ["entity", "bank", "account", "currency"]
    bal[["bank", "account"]] = bal[["bank", "account"]].fillna("")
    latest = bal.groupby(key)["as_of"].transform("max")
    bal = bal[bal["as_of"] == latest]
    return bal.groupby(key + ["as_of"], as_index=False)["balance"].sum()


def split_outflow_by_entity(calc: pd.DataFrame, pay: pd.DataFrame) -> pd.Series:
    """主体级流出: recurring 规律不带主体，按付款历史中该收款方在各主体的
    金额占比拆分预测（不用众数——同收款方多主体付款时不会张冠李戴）。"""
    share = pay[pay["payee"] != ""].groupby(["payee", "entity"])["amount"].sum()
    rows = []
    for _, r in calc.iterrows():
        if r["entity"]:
            rows.append((r["entity"], r["currency"], r["forecast"]))
            continue
        payees = share.index.get_level_values("payee")
        if r["payee"] in payees:
            s = share.loc[r["payee"]]
            for ent, amt in s.items():
                rows.append((ent, r["currency"], r["forecast"] * amt / s.sum()))
        else:
            rows.append(("", r["currency"], r["forecast"]))
    return (pd.DataFrame(rows, columns=["entity", "currency", "forecast"])
            .groupby(["entity", "currency"])["forecast"].sum())


def position_view(bal: pd.DataFrame, calc: pd.DataFrame, pay: pd.DataFrame):
    """头寸视图: 币种余额 − 未来4周预测流出(calc=进计算的预测行) → 缺口/富余 → 调拨建议。

    调拨建议仅为 preview（同币种主体间余缺互补），不生成任何指令；
    分配会扣减捐出方额度，覆盖不了的残余明示并转外汇节。
    返回 (头寸表行, 调拨建议行, 各币种购汇缺口)。
    """
    out_cur = calc.groupby("currency")["forecast"].sum()
    bal_cur = bal.groupby("currency")["balance"].sum()
    lines = ["| 币种 | 余额(最新快照) | 未来4周预测流出 | 头寸 | 状态 |",
             "|---|---:|---:|---:|---|"]
    fx_gap = {}
    for cur in sorted(set(bal_cur.index) | set(out_cur.index)):
        b, o = float(bal_cur.get(cur, 0.0)), float(out_cur.get(cur, 0.0))
        pos = b - o
        fx_gap[cur] = max(0.0, -pos)
        status = "⚠️ 缺口" if pos < 0 else "富余"
        lines.append(f"| {cur} | {b:,.0f} | {o:,.0f} | {pos:,.0f} | {status} |")

    # 主体级余缺 → 同币种调拨建议（贪心分配，扣减捐出方可用额度）
    out_ent = split_outflow_by_entity(calc, pay)
    bal_ent = bal.groupby(["entity", "currency"])["balance"].sum()
    ent_pos = bal_ent.sub(out_ent, fill_value=0.0)
    avail = {k: float(v) for k, v in ent_pos[ent_pos > 0].items()}
    transfers = []
    for (ent, cur), pos in ent_pos[ent_pos < 0].sort_values().items():
        need = -float(pos)
        donors = sorted((k for k in avail if k[1] == cur and avail[k] > 0),
                        key=lambda k: -avail[k])
        for d in donors:
            take = min(need, avail[d])
            transfers.append(f"- **调拨建议（preview-only，不生成指令）**: {d[0]} → {ent} "
                             f"{take:,.0f} {cur}（{ent} 头寸缺口 {need:,.0f}，"
                             f"{d[0]} 可用富余 {avail[d]:,.0f}）")
            avail[d] -= take
            need -= take
            if need <= 0:
                break
        if need > 0:
            transfers.append(f"- **{ent}/{cur}** 同币种调拨后仍缺 {need:,.0f}，"
                             f"需购汇/换汇覆盖（见外汇交易管控节）。")
    return lines, transfers, fx_gap


def fx_view(calc: pd.DataFrame, bud, month: str, fx_gap: dict | None = None):
    """外汇管控: 未来4周分币种净流出 → 建议交易区间(不超预算额度)与期限匹配。

    有余额数据时（fx_gap 非 None），购汇建议只针对余额覆盖不了的缺口，
    避免"头寸富余仍建议全额购汇"的自相矛盾；无余额数据时退回全额口径。
    """
    by_cur = calc.groupby("currency")["forecast"].sum()
    lines = []
    for cur, outflow in by_cur.items():
        need = outflow if fx_gap is None else fx_gap.get(cur, 0.0)
        if fx_gap is not None and need <= 0:
            lines.append(f"- **{cur}**: 未来4周预测流出 {outflow:,.0f}，"
                         f"现有余额头寸可覆盖，无需购汇。")
            continue
        cap = None
        if bud is not None:
            cap = bud[(bud["month"] == month) & (bud["currency"] == cur)]["budget"].sum()
        cap_txt = f"，预算额度上限 {cap:,.0f}" if cap else ""
        gap_txt = "" if fx_gap is None else f"（预测流出 {outflow:,.0f}，余额覆盖后缺口）"
        amt_lo, amt_hi = need * 0.8, min(need * 1.1, cap) if cap else need * 1.1
        lines.append(f"- **{cur}**: 未来4周购汇需求 {need:,.0f}{gap_txt}{cap_txt}。"
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


def approvals_view(sec: str):
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
    lines = [f"\n## {sec}、审批流程画像\n"]
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
    ap.add_argument("--strict-approval", action="store_true",
                    help="计算只用人工批准(status=approved)的规律；approved 为 0 时自动回退")
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

    # 审批门控：strict 下计算只用 approved；approved 为 0 时回退置信度口径（过渡模式）
    strict, transition = args.strict_approval, False
    if strict and not any(status_of(p) == "approved" for p in pats["patterns"]):
        strict, transition = False, True
    calc = fc[fc["status"] == "approved"] if strict else fc[fc["confidence"] == "high"]

    L = [f"# 资金月报与滚动预测 — 截至 {asof.date()}",
         f"\n> 数据: {pats['meta']['rows']} 笔付款 ({pats['meta']['data_range'][0]} ~ "
         f"{pats['meta']['data_range'][1]})；规律库版本 {pats['meta']['generated_at']}；"
         f"全部数字由确定性引擎计算，可追溯。\n"]
    if strict:
        L.append("> 门控: strict-approval 开启，计算只用人工批准(approved)的规律。\n")
    elif transition:
        L.append("> ⚠️ 过渡模式: --strict-approval 已开启但 approved 规律为 0，"
                 "本次回退按置信度(high)计算；请先用 approve.py 批准规律。\n")

    # 可选节（预算/头寸/关联方/审批）缺数据时跳过，编号按实际出现顺序连续
    numerals = iter("一二三四五六七")
    sec = lambda: next(numerals)  # noqa: E731

    if bud is not None:
        bud_month = month if (bud["month"] == month).any() else bud["month"].max()
        m, causes = budget_variance(pay, bud, bud_month)
        note = "" if bud_month == month else f"（预算数据止于 {bud_month}，取其为对比月）"
        L.append(f"## {sec()}、{bud_month} 预算执行差异{note}\n")
        L.append("| 主体 | 项目 | 币种 | 预算 | 实际 | 差异% | 差异根因(Top收款方) |")
        L.append("|---|---|---|---:|---:|---:|---|")
        for _, r in m.iterrows():
            c = causes.get((r["entity"], r["project"], r["currency"]), "")
            flag = " ⚠️" if pd.notna(r["var_pct"]) and abs(r["var_pct"]) > 10 else ""
            L.append(f"| {r['entity']} | {r['project']} | {r['currency']} | "
                     f"{r['budget']:,.0f} | {r['actual']:,.0f} | {r['var_pct']}%{flag} | {c} |")

    L.append(f"\n## {sec()}、未来4周资金预测（分主体/项目/币种）\n")
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

    bal = load_balances(asof)
    fx_gap = None
    if bal is not None:
        pos_lines, transfers, fx_gap = position_view(bal, calc, pay)
        L.append(f"\n## {sec()}、头寸与调拨建议（余额快照 {bal['as_of'].max()}）\n")
        L += pos_lines
        if transfers:
            L.append("")
            L += transfers

    L.append(f"\n## {sec()}、外汇交易管控建议\n")
    L += fx_view(calc, bud, month, fx_gap)

    if rp:
        monthly, cur = related_party(pay, rp, month)
        L.append(f"\n## {sec()}、关联方资金往来\n")
        if cur is not None and len(cur):
            for _, r in cur.iterrows():
                L.append(f"- {month} {r['project']}: {r['sum']:,.0f} {r['currency']}"
                         f"（{r['count']}笔），全部逐笔可追溯至源记录。")
            L.append("\n近月趋势（金额，分类|币种）：")
            mp = monthly.assign(col=monthly["project"] + "|" + monthly["currency"])
            L.append(mp.pivot_table(index="month", columns="col",
                                    values="sum").tail(4).to_markdown(floatfmt=",.0f"))

    L += approvals_view(sec())

    st = Counter(status_of(p) for p in pats["patterns"])
    pending_high = sum(1 for p in pats["patterns"]
                       if p["confidence"] == "high" and status_of(p) == "candidate")
    L.append(f"\n---\n*规律库: approved {st.get('approved', 0)} / candidate {st.get('candidate', 0)}"
             f"（其中 high 待批 {pending_high}）/ refuted {st.get('refuted', 0)}；"
             f"provisional 规律未参与计算；批准/否决用 approve.py。*")

    (outdir / "report.md").write_text("\n".join(L), encoding="utf-8")
    print(f"报告 → {outdir / 'report.md'}\n预测明细 → {outdir / 'forecast.csv'}")


if __name__ == "__main__":
    main()
