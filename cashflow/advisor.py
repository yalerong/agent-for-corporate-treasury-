"""调拨建议引擎 v0：文件投喂 → 确定性缺口计算 + 规则路由 → 带出处的建议单（人审执行）。

哲学与 cashflow 主线一致：LLM 不出现在计算路径上；规则是数据（advisor_rules.yaml，
三态 status，只信 approved）；建议单每个数字可追溯到输入文件。

两道硬门（gate）：
  已付核销门   计划表≠待付清单（2026-08 实战：表内混入已执行单会虚增缺口 82 万）。
               首选 --liushui 给 finweb 流水导出自动核销（币种+金额唯一命中才剔除，
               主体不参与匹配——流水「项目归属」是业务线≠付款主体；含糊命中只提示）；
               paid.yaml 作人工补充（流水覆盖不到的，如虚拟账户出的 USDT）。
               两者都不提供 → 建议单顶部打红旗。
  主体空白门   计划表主体空白的行单列人工确认，绝不静默丢弃也绝不猜测归属。

用法：
  python advisor.py --plan 资金计划表.xlsx --balances 余额总览.xlsx \
      [--week 2026.08.03-2026.08.07] [--liushui 流水查询导出.xlsx] [--paid paid.yaml] \
      [--transfers transfers.yaml] [--rules advisor_rules.yaml] \
      [--map advisor_entity_map.yaml] [--fx-usdmxn 17.5] [--out advice]
"""
import argparse
from pathlib import Path

import pandas as pd
from advisor_inputs import (
    load_balances,
    load_entity_map,
    load_liushui,
    load_plan_week,
    load_rules,
    load_yaml,
)
from constants import get_root

AMT_TOL = 0.01  # paid 按金额匹配时的容差


# ---------- 已付核销 ----------

def auto_net_from_liushui(plan: pd.DataFrame, flows: pd.DataFrame
                          ) -> tuple[pd.DataFrame, list[str], list[str]]:
    """对流水自动核销：币种+金额(±AMT_TOL) 唯一命中才剔除，含糊命中只提示。

    主体不参与匹配（流水「项目归属」是业务线≠付款主体）。一笔流水最多核销一行。
    返回 (剩余计划, 核销记录, 含糊提示)。
    """
    drop, notes, ambig = set(), [], []
    used = set()
    for i, r in plan.iterrows():
        m = flows[(flows["currency"] == r["currency"])
                  & ((flows["amount"] - r["amount"]).abs() <= AMT_TOL)
                  & (~flows.index.isin(used))]
        if len(m) == 1:
            f = m.iloc[0]
            used.add(m.index[0])
            drop.add(i)
            notes.append(f"自动核销：{r['entity']} {r['currency']} {r['amount']:,.2f} ← "
                         f"流水 {f['date']:%m-%d} {str(f['payee'])[:20]}")
        elif len(m) > 1:
            ambig.append(f"{r['entity']} {r['currency']} {r['amount']:,.2f} 在流水里有 "
                         f"{len(m)} 笔同额命中（{'/'.join(m['date'].dt.strftime('%m-%d'))}）"
                         f"——请人工核后写入 paid.yaml")
    return plan.drop(index=list(drop)).reset_index(drop=True), notes, ambig


def net_paid(plan: pd.DataFrame, paid: list[dict]) -> tuple[pd.DataFrame, list[str]]:
    """从计划中剔除已执行单。匹配优先级：lark_no 精确 > 主体+币种+金额。"""
    drop, notes = set(), []
    for p in paid:
        lark = str(p.get("lark_no", "")).strip()
        hit = pd.Series(False, index=plan.index)
        if lark:
            hit = plan["lark_no"] == lark
        if not hit.any() and p.get("amount") is not None:
            hit = ((plan["currency"] == p.get("currency"))
                   & ((plan["amount"] - float(p["amount"])).abs() <= AMT_TOL))
            if p.get("entity"):
                hit &= plan["entity"] == p["entity"]
        idx = [i for i in plan.index[hit] if i not in drop]
        if idx:
            i = idx[0]
            drop.add(i)
            notes.append(f"剔除已付：{plan.at[i, 'entity']} {plan.at[i, 'currency']} "
                         f"{plan.at[i, 'amount']:,.2f}（{p.get('note', '')}）")
        else:
            notes.append(f"⚠️ paid 条目未匹配到计划行：{p}")
    return plan.drop(index=list(drop)).reset_index(drop=True), notes


# ---------- 缺口计算 ----------

def entity_needs(plan: pd.DataFrame, emap: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """按主体×币种聚合需求。返回 (needs, 主体空白行)。

    交易账户命中 channel_overrides 的行，需求记到该通道名下（如 刘时英卡）。
    """
    df = plan.copy()
    ov = emap["channel_overrides"]
    df["who"] = df.apply(
        lambda r: str(r["channel"]).strip()
        if str(r.get("channel", "")).strip() in ov else r["entity"], axis=1)
    blank = df[df["who"].isna()].copy()
    known = df[df["who"].notna()]
    needs = (known.groupby(["who", "currency"])["amount"].agg(["sum", "count"])
             .reset_index().rename(columns={"who": "entity", "sum": "need"}))
    return needs, blank


def entity_avail(bal: pd.DataFrame, emap: dict) -> pd.DataFrame:
    """按映射把 finweb 公司余额折到计划表主体名下（主体×币种求和）。"""
    name_of = {}
    for short, company in emap["entities"].items():
        name_of[company] = short
    for channel, company in emap["channel_overrides"].items():
        name_of.setdefault(company, channel)
    df = bal.copy()
    df["entity"] = df["company"].map(name_of)
    df = df[df["entity"].notna()]
    return (df.groupby(["entity", "currency"])["balance"].sum()
            .reset_index().rename(columns={"balance": "avail"}))


def compute_gaps(needs: pd.DataFrame, avail: pd.DataFrame,
                 transfers: list[dict]) -> pd.DataFrame:
    """gap = need − avail − 未到账在途。正数缺口为负值表示（与建议单口径一致用符号区分）。"""
    g = needs.merge(avail, on=["entity", "currency"], how="left")
    g["avail"] = g["avail"].fillna(0.0)
    g["transit"] = 0.0
    for t in transfers:
        if t.get("arrived"):
            continue
        m = (g["entity"] == t["to_entity"]) & (g["currency"] == t["currency"])
        g.loc[m, "transit"] += float(t["amount"])
    g["gap"] = g["need"] - g["avail"] - g["transit"]
    return g


# ---------- 规则路由 ----------

def rules_of(rules: list[dict], typ: str) -> list[dict]:
    return [r for r in rules if r.get("type") == typ]


def build_fx_pools(bal: pd.DataFrame, rules: list[dict]) -> dict:
    """财务控制的换汇资金池：fx_pool 规则按账户名单从余额表逐户取数。"""
    pools = {}
    for r in rules_of(rules, "fx_pool"):
        p = r["params"]
        rows = bal[bal["account"].isin(p["accounts"])]
        pools[p["side"]] = {"currency": p["currency"], "rule": r["id"],
                            "total": float(rows["balance"].sum()),
                            "accounts": dict(zip(rows["account"], rows["balance"], strict=False))}
    return pools


def route(gaps: pd.DataFrame, bal: pd.DataFrame, rules: list[dict],
          fx_usdmxn: float) -> tuple[list[str], list[str]]:
    """把缺口翻成建议动作。返回 (actions, warnings)。规则不覆盖的缺口进 warnings。"""
    actions, warns = [], []
    indep = {r["params"]["entity"] for r in rules_of(rules, "independent_entity")}
    fx_route = {r["params"]["entity"]: r["params"]["side"] for r in rules_of(rules, "fx_route")}
    lenders = [r["params"] for r in rules_of(rules, "lender")]
    pools = build_fx_pools(bal, rules)

    # USDT：全集团一个池（usdt_hub 规则），先抵扣周度回流
    hub_rules = rules_of(rules, "usdt_hub")
    usdt_gap = float(gaps.loc[gaps["currency"] == "USDT", "gap"].clip(lower=0).sum())
    if usdt_gap > 0 and hub_rules:
        p = hub_rules[0]["params"]
        hub_bal = float(bal.loc[bal["account"] == p["hub"], "balance"].sum())
        sweep = bal[bal["account"].isin(p.get("sweep_accounts", []))]
        sweep_amt = float(sweep["balance"].sum())
        inflow = rules_of(rules, "weekly_inflow")
        lo = sum(float(r["params"]["low"]) for r in inflow if r["params"]["currency"] == "USDT")
        need_after = usdt_gap + 0  # gap 已扣各主体自有（USDT 各户已并入 avail）
        actions.append(
            f"[USDT] 全周池需求缺口 {usdt_gap:,.0f}：hub({p['hub']}) 现额 {hub_bal:,.0f}"
            f"，归集 {', '.join(f'{a} {v:,.0f}' for a, v in zip(sweep['account'], sweep['balance'], strict=False)) or '无'}"
            f"（+{sweep_amt:,.0f}），周度回流保守估 +{lo:,.0f}（{hub_rules[0]['id']}）")
        residual = need_after - sweep_amt - lo
        if residual > 0:
            wealth = rules_of(rules, "usdt_wealth_unlocked")
            unlocked = sum(float(r["params"]["amount"]) for r in wealth)
            if unlocked >= residual:
                actions.append(f"[USDT] 回流后仍缺 {residual:,.0f} → 后备：赎回已解锁理财"
                               f"（可用 {unlocked:,.0f}，{'/'.join(r['id'] for r in wealth)}）")
            else:
                warns.append(f"USDT 缺 {residual:,.0f}，已解锁理财 {unlocked:,.0f} 不够，需换汇补")

    for _, g in gaps[gaps["gap"] > AMT_TOL].iterrows():
        ent, ccy, gap = g["entity"], g["currency"], float(g["gap"])
        if ccy == "USDT":
            continue  # 已在池层面处理
        if ent in indep:
            warns.append(f"{ent} {ccy} 缺 {gap:,.0f}——独立主体，只能自筹（规则不许外部调入）")
            continue
        if ccy == "MXN" and ent in fx_route and fx_route[ent] in pools:
            pool = pools[fx_route[ent]]
            tag = "够" if pool["total"] >= gap else f"不够（现额 {pool['total']:,.0f}）"
            actions.append(f"[MXN] {ent} 缺 {gap:,.0f} → {fx_route[ent]} 侧财务池直付，{tag}"
                           f"（{pool['rule']}）")
            pool["total"] -= gap
            continue
        if ccy == "USD" and ent in fx_route and fx_route[ent] in pools:
            pool = pools[fx_route[ent]]
            mxn = gap * fx_usdmxn
            tag = "够" if pool["total"] >= mxn else f"不够（现额 {pool['total']:,.0f}）"
            actions.append(f"[USD换汇] {ent} 缺 {gap:,.0f} USD ≈ {mxn:,.0f} MXN → "
                           f"{fx_route[ent]} 侧池换汇，{tag}（{pool['rule']}，汇率 {fx_usdmxn}）")
            pool["total"] -= mxn
            continue
        lender = next((ln for ln in lenders if ccy in ln.get("currencies", [])), None)
        if lender:
            surplus = -float(gaps.loc[(gaps["entity"] == lender["entity"])
                                      & (gaps["currency"] == ccy), "gap"].sum())
            tag = "富余够" if surplus >= gap else f"⚠️ 富余 {surplus:,.0f} 不够"
            actions.append(f"[{ccy}拆借] {ent} 缺 {gap:,.0f} → {lender['entity']} 拆借/往来"
                           f"（{tag}）")
            continue
        warns.append(f"{ent} {ccy} 缺 {gap:,.0f}——无规则覆盖，需人工定路由")
    return actions, warns


# ---------- 建议单 ----------

def render(week: str, gaps: pd.DataFrame, blank: pd.DataFrame, actions: list[str],
           warns: list[str], paid_notes: list[str], sources: dict,
           paid_provided: bool) -> str:
    lines = [f"# 调拨建议单（引擎 v0）· 付款周 {week}", ""]
    if not paid_provided:
        lines += ["> 🚩 **未提供已付核销清单（--paid）**：计划表≠待付清单，下列缺口可能虚高。",
                  "> 请对主体银行日记账核已付后填 paid.yaml 重跑。", ""]
    for k, v in sources.items():
        lines.append(f"> {k}: {v}")
    lines.append("")
    if paid_notes:
        lines += ["## 已付核销", *[f"- {n}" for n in paid_notes], ""]
    lines += ["## 主体×币种 缺口表", "",
              "| 主体 | 币种 | 待付 | 现余额 | 在途 | 缺口(+缺/−余) |", "|---|---|---:|---:|---:|---:|"]
    for _, r in gaps.sort_values(["gap"], ascending=False).iterrows():
        lines.append(f"| {r['entity']} | {r['currency']} | {r['need']:,.0f} "
                     f"| {r['avail']:,.0f} | {r['transit']:,.0f} | {r['gap']:,.0f} |")
    lines.append("")
    if len(blank):
        lines += ["## ⚠️ 主体空白行（人工确认后补进 entity_map 或计划表）", ""]
        for _, r in blank.iterrows():
            lines.append(f"- {r['amount']:,.0f} {r['currency']}｜{str(r['memo'])[:60]}"
                         f"｜截止 {r['deadline']}｜lark {r['lark_no'] or '未提'}")
        lines.append("")
    lines += ["## 建议动作", *[f"- {a}" for a in actions], ""]
    if warns:
        lines += ["## 需人工裁决", *[f"- {w}" for w in warns], ""]
    lines += ["---", "*引擎只做确定性计算与规则路由；执行前人审。规则库与映射表 gitignore，方法开源。*"]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="调拨建议引擎 v0（文件投喂）")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--balances", required=True)
    ap.add_argument("--week")
    ap.add_argument("--paid")
    ap.add_argument("--liushui", help="finweb 流水查询导出，自动核销已付（唯一命中才剔除）")
    ap.add_argument("--liushui-days", type=int, default=14,
                    help="流水回看窗口：周起始日往前 N 天（默认 14）")
    ap.add_argument("--transfers")
    ap.add_argument("--rules", default="advisor_rules.yaml")
    ap.add_argument("--map", dest="emap", default="advisor_entity_map.yaml")
    ap.add_argument("--fx-usdmxn", type=float, default=17.5)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    week, plan = load_plan_week(a.plan, a.week)
    bal = load_balances(a.balances)
    rules = load_rules(a.rules)
    emap = load_entity_map(a.emap)
    paid = load_yaml(a.paid, "paid") if a.paid else []
    transfers = load_yaml(a.transfers, "transfers") if a.transfers else []

    plan, paid_notes = net_paid(plan, paid)  # 人工确认的优先核销
    ambig: list[str] = []
    if a.liushui:
        start = pd.Timestamp(week.split("-")[0].replace(".", "-"))
        since = (start - pd.Timedelta(days=a.liushui_days)).strftime("%Y-%m-%d")
        flows = load_liushui(a.liushui, since=since)
        plan, auto_notes, ambig = auto_net_from_liushui(plan, flows)
        paid_notes += auto_notes
    needs, blank = entity_needs(plan, emap)
    avail = entity_avail(bal, emap)
    gaps = compute_gaps(needs, avail, transfers)
    actions, warns = route(gaps, bal, rules, a.fx_usdmxn)
    warns += ambig
    md = render(week, gaps, blank, actions, warns, paid_notes,
                {"计划表": a.plan, "余额": a.balances, "规则": a.rules,
                 "流水": a.liushui or "未提供（自动核销未启用）",
                 "approved 规则数": len(rules)},
                paid_provided=bool(a.paid or a.liushui))
    out_dir = Path(a.out) if a.out else get_root() / "advice"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"advice-{week.replace('.', '')[:8]}.md"
    out.write_text(md, encoding="utf-8")
    print(f"建议单 → {out}")
    for w in warns:
        print(f"  [人工] {w}")


if __name__ == "__main__":
    main()
