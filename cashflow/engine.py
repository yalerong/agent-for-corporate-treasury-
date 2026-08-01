"""确定性引擎：报告组装（排版层）。全部计算在 metrics.py 指标层完成。

输出 runs/<date>/report.md + forecast.csv + lineage.json。
所有数字来自 SQLite + patterns.yaml（钉规律版本），LLM 不参与计算；
build_report 是纯函数，f-string 只做排版，report.md 各节标注 metric_id。

审批门控: strict-approval 默认开——计算只用人工批准(status=approved)的规律；
approved 为 0 时自动回退按置信度计算并在报告顶部标注"过渡模式"（防空报告）；
--no-strict-approval 为逃生口。refuted 规律任何模式都不参与计算与预测。

用法: python engine.py [--asof 2026-07-31] [--no-strict-approval]
"""
import argparse
import json
from collections import Counter

import pandas as pd
from constants import GROUP, get_root
from metrics import build_context, compute_all
from pattern_store import status_of

ROOT = get_root()


def build_report(ctx: dict, mets: dict) -> str:
    """纯排版：只从 ctx/指标字典取数拼 f-string，不做任何计算与 IO。"""
    pats, month = ctx["pats"], ctx["month"]
    L = [f"# 资金月报与滚动预测 — 截至 {ctx['asof'].date()}",
         f"\n> 数据: {pats['meta']['rows']} 笔付款 ({pats['meta']['data_range'][0]} ~ "
         f"{pats['meta']['data_range'][1]})；规律库版本 {pats['meta']['generated_at']}；"
         f"全部数字由确定性引擎计算，可追溯。\n"]
    if ctx["strict"]:
        L.append("> 门控: strict-approval 开启，计算只用人工批准(approved)的规律。\n")
    elif ctx["transition"]:
        L.append("> ⚠️ 过渡模式: strict-approval（默认开）下 approved 规律为 0，"
                 "本次回退按置信度(high)计算；请用 approve.py 批准规律。\n")

    # 可选节（预算/归因/头寸/关联方/审批/核验）缺数据时跳过，编号按实际出现顺序连续
    numerals = iter("一二三四五六七八")
    sec = lambda: next(numerals)  # noqa: E731

    bv = mets["budget_variance"]["value"]
    if bv is not None:
        note = f"（预算数据止于 {bv['month']}，取其为对比月）" if bv["is_fallback_month"] else ""
        L.append(f"## {sec()}、{bv['month']} 预算执行差异{note} <!-- metric: budget_variance -->\n")
        L.append("| 主体 | 项目 | 币种 | 预算 | 实际 | 差异% | 差异根因(Top收款方) |")
        L.append("|---|---|---|---:|---:|---:|---|")
        for _, r in bv["table"].iterrows():
            c = bv["causes"].get((r["entity"], r["project"], r["currency"]), "")
            flag = " ⚠️" if pd.notna(r["var_pct"]) and abs(r["var_pct"]) > 10 else ""
            L.append(f"| {r['entity']} | {r['project']} | {r['currency']} | "
                     f"{r['budget']:,.0f} | {r['actual']:,.0f} | {r['var_pct']}%{flag} | {c} |")

    ma = mets["mom_attribution"]["value"]
    if ma is not None:
        L.append(f"\n## {sec()}、当月异动归因（环比 {ma['prev_month']}） <!-- metric: mom_attribution -->\n")
        for r in ma["rows"]:
            L.append(f"- **{r['currency']}**: {month} 流出 {r['cur']:,.0f}（环比 {r['delta']:+,.0f}）")
            for mv in r["movers"]:
                who = "/".join(x for x in (mv["entity"], mv["project"], mv["payee"]) if x)
                tag = "（预期内·日历对齐）" if mv["aligned"] else ""
                L.append(f"  - {who}: {mv['cur']:,.0f}（环比 {mv['delta']:+,.0f}）{tag}")

    fc = mets["forecast_4w"]["value"]
    L.append(f"\n## {sec()}、未来4周资金预测（分主体/项目/币种） <!-- metric: forecast_4w -->\n")
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

    pos = mets["position"]["value"]
    if pos is not None:
        L.append(f"\n## {sec()}、头寸与调拨建议（余额快照 {pos['snapshot_date']}）"
                 f" <!-- metric: position -->\n")
        L.append("| 币种 | 余额(最新快照) | 未来4周预测流出 | 头寸 | 状态 |")
        L.append("|---|---:|---:|---:|---|")
        for r in pos["currency_rows"]:
            status = "⚠️ 缺口" if r["position"] < 0 else "富余"
            L.append(f"| {r['currency']} | {r['balance']:,.0f} | {r['outflow']:,.0f} | "
                     f"{r['position']:,.0f} | {status} |")
        if pos["events"]:
            L.append("")
            for e in pos["events"]:
                if e["kind"] == "transfer":
                    L.append(f"- **调拨建议（preview-only，不生成指令）**: {e['donor']} → {e['recipient']} "
                             f"{e['amount']:,.0f} {e['currency']}（{e['recipient']} 头寸缺口 "
                             f"{e['need_before']:,.0f}，{e['donor']} 可用富余 "
                             f"{e['donor_avail_before']:,.0f}）")
                else:
                    L.append(f"- **{e['entity']}/{e['currency']}** 同币种调拨后仍缺 {e['amount']:,.0f}，"
                             f"需购汇/换汇覆盖（见外汇交易管控节）。")

    fxv = mets["fx_advice"]["value"]
    L.append(f"\n## {sec()}、外汇交易管控建议 <!-- metric: fx_advice -->\n")
    for it in fxv["items"]:
        if it["covered"]:
            L.append(f"- **{it['currency']}**: 未来4周预测流出 {it['outflow']:,.0f}，"
                     f"现有余额头寸可覆盖，无需购汇。")
            continue
        cap, need = it["cap"], it["need"]
        cap_txt = f"，预算额度上限 {cap:,.0f}" if cap else ""
        gap_txt = f"（预测流出 {it['outflow']:,.0f}，余额覆盖后缺口）" if fxv["has_balance"] else ""
        amt_lo, amt_hi = need * 0.8, min(need * 1.1, cap) if cap else need * 1.1
        L.append(f"- **{it['currency']}**: 未来4周购汇需求 {need:,.0f}{gap_txt}{cap_txt}。"
                 f"建议购汇/换汇区间 [{amt_lo:,.0f}, {amt_hi:,.0f}]，"
                 f"期限与付款周期匹配（≤4周，忌超额超期）；风险中性，不做方向性判断。")

    rpv = mets["related_party"]["value"]
    if rpv is not None:
        L.append(f"\n## {sec()}、关联方资金往来 <!-- metric: related_party -->\n")
        cur, monthly = rpv["cur"], rpv["monthly"]
        if cur is not None and len(cur):
            for _, r in cur.iterrows():
                L.append(f"- {month} {r['project']}: {r['sum']:,.0f} {r['currency']}"
                         f"（{r['count']}笔），全部逐笔可追溯至源记录。")
            L.append("\n近月趋势（金额，分类|币种）：")
            mp = monthly.assign(col=monthly["project"] + "|" + monthly["currency"])
            L.append(mp.pivot_table(index="month", columns="col",
                                    values="sum").tail(4).to_markdown(floatfmt=",.0f"))

    apv = mets["approvals_profile"]["value"]
    if apv is not None:
        L.append(f"\n## {sec()}、审批流程画像 <!-- metric: approvals_profile -->\n")
        for k in apv["kinds"]:
            L.append(f"- **{k['kind']}**：{k['n']} 单，同意率 {k['ok']:.0%}，审批耗时中位数 "
                     f"{k['med']:.1f} 小时；月均 {k['per_month']:.0f} 单")
            if k["natures"]:
                L.append("  - 性质分布: " + "; ".join(f"{n} {v}单" for n, v in k["natures"]))
        if apv["slow"]:
            L.append("- 耗时 Top3: " + "; ".join(
                f"{r['kind']}{r['apply_no']}({r['elapsed_hours']:.0f}h)" for r in apv["slow"]))

    pv = mets["pattern_validation"]["value"]
    if pv is not None:
        L.append(f"\n## {sec()}、规律核验（人审清单） <!-- metric: pattern_validation -->\n")
        a, b = pv["checked_range"]
        L.append(f"- 核验 {pv['n']} 条（窗口 {a} ~ {b}）：无违反 {pv['n'] - len(pv['violated'])} / "
                 f"违反 {len(pv['violated'])}")
        if pv["violated"]:
            L.append("- ⚠️ 违反明细（需人工复核；违反不自动否决，refuted 只能人为）：")
            for v in pv["violated"]:
                L.append(f"  - `{v['id']}` {v['type']} {v['key']} :: hit {v['hits']} / "
                         f"violated {v['misses']}（hit_rate {v['hit_rate']}，当前 {v['status']}）")
        else:
            L.append("- 全部通过，无待人审项。")
        if pv["demoted"]:
            L.append("- 自动降级（approved→candidate，人工复核后可重批）：")
            for d in pv["demoted"]:
                L.append(f"  - `{d['id']}` {d['claim']}（{d['demoted_at']}）")

    st = Counter(status_of(p) for p in pats["patterns"])
    pending_high = sum(1 for p in pats["patterns"]
                       if p["confidence"] == "high" and status_of(p) == "candidate")
    L.append(f"\n---\n*规律库: approved {st.get('approved', 0)} / candidate {st.get('candidate', 0)}"
             f"（其中 high 待批 {pending_high}）/ refuted {st.get('refuted', 0)}；"
             f"provisional 规律未参与计算；批准/否决用 approve.py。"
             f"各节口径登记于 metrics.yaml，数值血缘见同目录 lineage.json。*")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default=None)
    ap.add_argument("--strict-approval", action=argparse.BooleanOptionalAction, default=True,
                    help="strict: 计算只用人工批准(status=approved)的规律；--no-strict-approval 关闭")
    args = ap.parse_args()

    ctx = build_context(args.asof, args.strict_approval)
    mets = compute_all(ctx)
    outdir = ROOT / "runs" / ctx["asof"].strftime("%Y-%m-%d")
    outdir.mkdir(parents=True, exist_ok=True)
    mets["forecast_4w"]["value"].to_csv(outdir / "forecast.csv", index=False, encoding="utf-8-sig")
    (outdir / "lineage.json").write_text(
        json.dumps({k: v["lineage"] for k, v in mets.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    (outdir / "report.md").write_text(build_report(ctx, mets), encoding="utf-8")
    print(f"报告 → {outdir / 'report.md'}\n预测明细 → {outdir / 'forecast.csv'}\n"
          f"lineage → {outdir / 'lineage.json'}")


if __name__ == "__main__":
    main()
