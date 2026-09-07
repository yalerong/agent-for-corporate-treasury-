"""调拨建议引擎 v0：文件投喂 → 确定性缺口计算 + 规则路由 → 带出处的建议单（人审执行）。

哲学与 cashflow 主线一致：LLM 不出现在计算路径上；规则是数据（advisor_rules.yaml，
三态 status，只信 approved）；建议单每个数字可追溯到输入文件。

硬门（gate，v0.1 五道；每道都是一次实战挑错换来的）：
  已付核销门   计划表≠待付清单。--liushui 给 finweb 原始流水：lark 编号命中优先（跨币种也销）；
               币种+金额只认计划周窗口内且付款主体一致（account_prefixes 守门，USDT 例外）；
               含糊只提示。paid.yaml 作人工补充。两者都不提供 → 建议单顶部打红旗。
  主体空白门   主体空白的行单列人工确认，绝不静默丢弃也绝不猜测归属。
  金额空白门   金额空白但有主体/说明的行单列（2026-09-06：8 行静默消失，最贵的一次）。
  在途单门     --transfers 直接吃 Lark 调拨申请导出：对流水判已执行（状态滞后点名）、
               付款账户余额不够的判疑似过时、换汇/理财单不计到账、早于流水窗口的不判。
  项目户门     R-033：项目行由该项目户自付（IDR 只算 GIRO），不进集团缺口。
另：carryover.yaml 滚存台账并入计划；规则内嵌数字带 as_of 超 21 天提醒重核；
配置缺省从 CASHFLOW_ROOT/rules/ 读（私有知识仓单源）。

用法：
  python advisor.py --plan 资金计划表.xlsx --balances 余额总览.xlsx \
      [--week 2026.09.07-2026.09.11] [--liushui 流水查询_原始流水.xlsx] [--paid paid.yaml] \
      [--transfers 调拨申请导出.xlsx|transfers.yaml] [--rules ...] [--map ...] \
      [--fx-usdmxn 17.5] [--out advice]
"""
import argparse
import re
from pathlib import Path

import advisor_accounts as acct
import pandas as pd
from advisor_inputs import (
    cfg_path,
    load_alias,
    load_balances,
    load_entity_map,
    load_liushui,
    load_plan_week,
    load_rules,
    load_transfers_any,
    load_yaml,
)
from constants import get_root

AMT_TOL = 0.01  # paid 按金额匹配时的容差
# 自动核销只看"对外出款"类流水；内部划转/提现/放款等不参与核销但保留给在途单判定与账户质检。
# 2026-08-24 挑错：「本金出款」（基金退出）不在名单里漏看 36.5 万；2026-08-31 加「基金」。
PAY_CLASS_PREFIX = ("账单出款", "税费出款", "工资薪酬", "注资款出款", "员工报销/福利",
                    "房租物业水电出款", "利息出款", "本金出款", "基金")


def payment_flows(flows: pd.DataFrame, rules: list[dict] | None = None) -> pd.DataFrame:
    """核销候选 = 实质分类命中白名单的出账流水（liushui_whitelist 规则可覆盖默认名单）。"""
    prefixes = PAY_CLASS_PREFIX
    for r in rules or []:
        if r.get("type") == "liushui_whitelist":
            prefixes = tuple(r["params"]["prefixes"])
    if "classification" not in flows.columns:
        return flows
    cls = flows["classification"].astype(str)
    keep = cls.str.startswith(prefixes) | (cls.str.strip() == "")  # 老导出没有分类列时不过滤
    return flows[keep]


# ---------- 已付核销 ----------

def auto_net_from_liushui(plan: pd.DataFrame, flows: pd.DataFrame,
                          week_start: pd.Timestamp | None = None,
                          emap: dict | None = None
                          ) -> tuple[pd.DataFrame, list[str], list[str]]:
    """对流水自动核销（v0.1 三改，2026-08-17→09-06 连错四周后定的口径）：

    1. lark 编号优先：计划行「已提交lark流程编号」命中流水「流水审批号」即核销，
       不看币种金额（USD 计划行常被 CHANNELX 以 IDR 直付——跨币种失明两连击）。
    2. 金额匹配只认计划周窗口内（date ≥ week_start）的流水；更早的流水只能靠 lark 编号
       （拉美出资 50K 被上周尾款销掉四连击）。
    3. 主体守门：流水「我方账户」推付款主体 ≠ 计划主体 → 降为含糊，不销
       （DELTA 5,000/20,000 被 EPS/ZETA 付阿里云误销）。USDT 不守门（R-007 hub 代付全集团）。
    一笔流水最多核销一行。返回 (剩余计划, 核销记录, 含糊提示)。
    """
    from advisor_inputs import entity_of_account

    drop, notes, ambig = set(), [], []
    used = set()
    has_appr = "approval_no" in flows.columns
    has_acct = "account" in flows.columns
    # 1) lark 编号
    if has_appr:
        appr = flows["approval_no"].astype(str)
        for i, r in plan.iterrows():
            lark = str(r.get("lark_no", "")).strip()
            if not lark or lark == "nan":
                continue
            m = flows[appr.str.contains(re.escape(lark), na=False) & (~flows.index.isin(used))]
            if len(m):
                # 同一 lark 常挂多行计划（两家各付一笔广告费）：按付款主体挑那一笔，且只消费一笔
                if has_acct and emap and pd.notna(r["entity"]):
                    mine = m[m["account"].map(lambda a: entity_of_account(a, emap)) == r["entity"]]
                    if len(mine):
                        m = mine
                f = m.iloc[0]
                used.add(m.index[0])
                drop.add(i)
                extra = f"{f['currency']} {f['amount']:,.0f}" if f["currency"] != r["currency"] else ""
                tag = "（滚存项）" if str(r.get("kind", "")) == "滚存" else ""
                notes.append(f"自动核销：{r['entity']} {r['currency']} {r['amount']:,.2f}{tag} ← "
                             f"流水 {f['date']:%m-%d} lark {lark} 命中 {str(f['payee'])[:16]}"
                             f"{'（实付 ' + extra + '，跨币种）' if extra else ''}")
    # 2) 币种+金额（周窗口内）+ 3) 主体守门
    win = flows if week_start is None else flows[flows["date"] >= week_start]
    for i, r in plan.iterrows():
        if i in drop:
            continue
        m = win[(win["currency"] == r["currency"])
                & ((win["amount"] - r["amount"]).abs() <= AMT_TOL)
                & (~win.index.isin(used))]
        if len(m) == 0:
            continue
        who = r["entity"] if pd.notna(r["entity"]) else None
        if has_acct and emap and r["currency"] != "USDT" and who:
            payers = m["account"].map(lambda a: entity_of_account(a, emap))
            mism = m[payers.notna() & (payers != who)]
            m = m[~m.index.isin(mism.index)]
            for _, f in mism.iterrows():
                ambig.append(f"{who} {r['currency']} {r['amount']:,.2f} 同额但付款方是 "
                             f"{entity_of_account(f['account'], emap)}（{f['account']} {f['date']:%m-%d}）"
                             f"——主体不一致，未核销；若是代付请写 paid.yaml")
        if len(m) == 1:
            f = m.iloc[0]
            used.add(m.index[0])
            drop.add(i)
            tag = "（滚存项）" if str(r.get("kind", "")) == "滚存" else ""
            notes.append(f"自动核销：{r['entity']} {r['currency']} {r['amount']:,.2f}{tag} ← "
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


def mark_executed(transfers: list[dict], flows: pd.DataFrame | None) -> list[str]:
    """在途单 vs 流水：Lark「审批中/已同意」状态滞后于实际执行（2026-08-31/09-06 两周各抓到 9 单）。

    判定执行：流水「流水审批号」含该 lark 编号（首选）；否则 付款账户+币种+金额 命中。
    原地写 t['executed']/t['executed_by']；返回给人看的提示行。
    """
    notes = []
    if flows is None or not len(flows):
        return notes
    appr = flows["approval_no"].astype(str)
    for t in transfers:
        if t.get("arrived") or t.get("executed"):
            continue
        hit = flows[appr.str.contains(re.escape(str(t.get("lark_no", ""))), na=False)] if t.get("lark_no") else flows.iloc[0:0]
        how = "lark 编号"
        if not len(hit) and t.get("from_account"):
            hit = flows[(flows["account"].str.casefold() == str(t["from_account"]).casefold())
                        & (flows["currency"] == t.get("currency"))
                        & ((flows["amount"] - float(t["amount"])).abs() <= AMT_TOL)]
            how = "付款账户+金额"
        if len(hit):
            t["executed"] = True
            t["executed_by"] = how
            d = hit["date"].min()
            tag = "⚠️ 状态仍是审批中" if str(t.get("status", "")).startswith("审批中") else "状态已同意"
            notes.append(f"{t['lark_no']} {t.get('currency')} {float(t['amount']):,.0f} → {t.get('to_entity') or t.get('to_company', '')[:14]}"
                         f"：流水 {d:%m-%d} 已执行（{how}；{tag}）")
    return notes


def project_tag(project: str, memo: str, alias: dict, known: set[str]) -> str | None:
    """计划行属于哪个项目：「所属项目」列或款项说明命中 项目短码/长名（大小写不敏感）。

    2026-08-31 挑错：projectone 顾问费「所属项目」空白但 memo 写着 projectone → 1.1 亿假缺口。
    """
    text = f"{project or ''} {memo or ''}".casefold()
    if not text.strip():
        return None
    for short in sorted(known, key=len, reverse=True):
        names = {short.casefold(), str(alias.get(short, "")).casefold()} - {""}
        if any(n in text for n in names):
            return short
    return None


def project_self_funded(plan: pd.DataFrame, bal: pd.DataFrame, rules: list[dict],
                        emap: dict, alias: dict) -> tuple[pd.DataFrame, list[str]]:
    """R-033 项目户自足判定：计划行属于项目 P，且付款主体名下 P 项目户（IDR 只算 GIRO，R-031：RDL
    是过账通道不算弹药）同币种余额够付 → 该行由项目户出，不进集团缺口表。返回 (剩余计划, 说明行)。"""
    ann = acct.annotate(bal, rules)
    name_of = {c: s for s, c in emap["entities"].items()}
    proj = ann[(ann["scope"] == "project") & ann["company"].map(name_of).notna()].copy()
    proj["entity"] = proj["company"].map(name_of)
    is_giro = proj["account"].str.contains("GIRO", case=False) | (proj["currency"] != "IDR")
    proj = proj[is_giro]
    pool = proj.groupby(["entity", "currency", "scope_detail"])["balance"].sum().to_dict()
    known = {k[2] for k in pool}
    drop, lines = [], []
    for i, r in plan.iterrows():
        if pd.isna(r["entity"]) or pd.isna(r["amount"]):
            continue
        tag = project_tag(r.get("project"), r.get("memo"), alias, known)
        if not tag:
            continue
        key = (r["entity"], r["currency"], tag)
        have = pool.get(key, 0.0)
        if have >= float(r["amount"]):
            pool[key] = have - float(r["amount"])
            drop.append(i)
            lines.append(f"{r['entity']} {r['currency']} {r['amount']:,.0f}｜{str(r['memo'])[:40]}"
                         f" → {tag} 项目户自付（该项目户可动用 {have:,.0f}，付后余 {pool[key]:,.0f}）")
        elif have > 0:
            lines.append(f"⚠️ {r['entity']} {r['currency']} {r['amount']:,.0f}｜{str(r['memo'])[:40]}"
                         f" 属 {tag} 项目但项目户只有 {have:,.0f}，差额进集团缺口")
    return plan.drop(index=drop).reset_index(drop=True), lines


def flag_stale_transfers(pending: list[dict], bal: pd.DataFrame) -> list[str]:
    """未执行的在途单：付款账户现余额 < 单上金额 → 疑似过时/重复（2026-09-06：P1 16.3 万单挂着，
    账上只剩 4.7 万，实际已按 7.2 万另单归集）。原地标 t['suspect']=True，compute_gaps 不再计入。"""
    if not len(bal):
        return []
    by_acct = {str(a).casefold(): float(b) for a, b in
               bal.groupby("account")["balance"].sum().items()}
    out = []
    for t in pending:
        src = str(t.get("from_account") or "").casefold()
        if not src or src not in by_acct:
            continue
        have = by_acct[src]
        if have < float(t["amount"]) * 0.98:
            t["suspect"] = True
            out.append(f"{t['lark_no']} {t.get('currency')} {float(t['amount']):,.0f}"
                       f"（{t['from_account']} 现 {have:,.0f}）")
    return out


def append_carryover(plan: pd.DataFrame, carry: list[dict]) -> tuple[pd.DataFrame, list[str]]:
    """跨周滚存台账（rules/carryover.yaml）：上周该付没付、本周计划表又没写的项，作为计划行并入。

    2026-08-31/09-06 挑错：DELTA 代付 NORTH 40.7 万滚了四周计划表都不见，引擎看不见就等于不存在。
    行的 memo 前缀 [滚存自 YYYY-MM-DD]；若本周流水已付会被核销并提示可删。
    """
    if not carry:
        return plan, []
    rows, notes = [], []
    for c in carry:
        rows.append({"entity": c.get("entity"), "amount": float(c["amount"]), "currency": c["currency"],
                     "kind": "滚存", "pub_priv": "", "channel": c.get("channel", ""),
                     "memo": f"[滚存自 {c.get('since', '?')}] {c.get('memo', '')}",
                     "deadline": c.get("deadline", ""), "dept": "", "project": c.get("project", ""),
                     "submitter": "", "lark_submitted": "", "lark_no": str(c.get("lark_no", "") or "")})
        notes.append(f"{c.get('entity')} {c['currency']} {float(c['amount']):,.0f}｜{c.get('memo', '')[:50]}"
                     f"｜自 {c.get('since', '?')}")
    add = pd.DataFrame(rows)
    for col in plan.columns:
        if col not in add.columns:
            add[col] = ""
    return pd.concat([plan, add[plan.columns]], ignore_index=True), notes


def stale_rule_data(rules: list[dict], max_age_days: int = 21,
                    today: pd.Timestamp | None = None) -> list[str]:
    """规则里内嵌的静态数字（理财已解锁额、周度回流区间、汇率）带 as_of 采集日；超期就提醒重核。
    2026-08-31/09-06 三连击：R-009 写 105 万，实际 WEALTHX 172 万全可赎。"""
    today = today or pd.Timestamp.today().normalize()
    out = []
    for r in rules:
        as_of = r.get("as_of") or (r.get("params") or {}).get("as_of")
        if not as_of:
            continue
        try:
            age = (today - pd.Timestamp(str(as_of))).days
        except (ValueError, TypeError):
            continue
        if age > max_age_days:
            out.append(f"{r['id']} 内嵌数据采集于 {as_of}（{age} 天前），超过 {max_age_days} 天——用前重核")
    return out


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


def entity_avail(bal: pd.DataFrame, emap: dict, rules: list[dict] | None = None) -> pd.DataFrame:
    """按映射把 finweb 公司余额折到计划表主体名下（主体×币种求和）。

    v0.1：只计入 scope==group 的账户——项目户只能付同项目、专用户有 KYC 摩擦、
    通道户是过路的、业务控制户留放款周转，四类都不是"可动用"（R-012/014/015/017）。
    不传 rules 则退回整体加总（旧行为，仅测试用）。
    """
    name_of = {}
    for short, company in emap["entities"].items():
        name_of[company] = short
    for channel, company in emap["channel_overrides"].items():
        name_of.setdefault(company, channel)
    df = acct.usable(acct.annotate(bal, rules)) if rules else bal.copy()
    df = df.copy()
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
        if (t.get("arrived") or t.get("executed") or t.get("suspect") or t.get("unjudgeable")
                or not t.get("to_entity")):
            continue
        if str(t.get("status", "")) not in ("", "审批中", "已同意", "PENDING", "APPROVED"):
            continue  # 已撤回/已拒绝不是在途
        if "理财" in str(t.get("kind", "")):
            continue  # 资金理财（定存/申购）是钱出去，不是到账在途
        to_ccy = t.get("to_currency") or t["currency"]
        if to_ccy != t["currency"]:
            continue  # 换汇类在途（MXN 出 → USD 进）金额未折算，不计入，只在在途清单里提示
        m = (g["entity"] == t["to_entity"]) & (g["currency"] == to_ccy)
        g.loc[m, "transit"] += float(t["amount"])
    g["gap"] = g["need"] - g["avail"] - g["transit"]
    return g


# ---------- 规则路由 ----------

def rules_of(rules: list[dict], typ: str) -> list[dict]:
    return [r for r in rules if r.get("type") == typ]


def restricted_summary(bal: pd.DataFrame, emap: dict, rules: list[dict]) -> list[str]:
    """受限资金一览：明确写出"有钱但不能用"，避免看着余额充裕却做不到。"""
    ann = acct.annotate(bal, rules)
    name_of = {c: s for s, c in emap["entities"].items()}
    ann = ann[ann["company"].map(name_of).notna() & (ann["balance"].abs() > 0.5)]
    ann = ann.assign(entity=ann["company"].map(name_of))
    label = {"project": "项目户(只能付同项目)", "earmarked": "专用户",
             "channel": "通道户(过路,余额≈0)", "business": "业务控制户"}
    lines = []
    for (ent, ccy, scope), g in ann[ann["scope"] != "group"].groupby(
            ["entity", "currency", "scope"]):
        detail = "/".join(sorted({d for d in g["scope_detail"] if d}))[:40]
        lines.append(f"{ent} {ccy} {g['balance'].sum():,.0f} —— {label.get(scope, scope)}"
                     f"{'：' + detail if detail else ''}")
    return sorted(lines)


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

    # payment_path 规则：某主体某币种有明确的付款/兜底路径（R-020 控台代付、R-021 两跳），
    # 优先于 fx_route 的"侧池直付"。2026-08-31/09-06 两周 MX EPS 都被路由到 SOUTH 池直付，违 R-020。
    paths = {}
    for r in rules_of(rules, "payment_path"):
        p = r["params"]
        if p.get("entity"):
            paths[p["entity"]] = (r["id"], p)

    for _, g in gaps[gaps["gap"] > AMT_TOL].iterrows():
        ent, ccy, gap = g["entity"], g["currency"], float(g["gap"])
        if ccy == "USDT":
            continue  # 已在池层面处理
        if ent in indep:
            warns.append(f"{ent} {ccy} 缺 {gap:,.0f}——独立主体，只能自筹（规则不许外部调入）")
            continue
        if ent in paths:
            rid, p = paths[ent]
            way = p.get(f"fallback_{ccy.lower()}") or p.get("path")
            if way:
                status = f"；{p['localbank_status']}" if p.get("localbank_status") else ""
                actions.append(f"[{ccy}] {ent} 缺 {gap:,.0f} → {way}（{rid}{status}）")
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
           paid_provided: bool, restricted: list[str] | None = None,
           acct_findings: list[dict] | None = None,
           transit_notes: list[str] | None = None,
           blank_amt: pd.DataFrame | None = None,
           project_lines: list[str] | None = None,
           carry_notes: list[str] | None = None) -> str:
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
    if carry_notes:
        lines += ["## 滚存项（carryover.yaml：上周未付、本周计划表没写，已并入缺口表）", "",
                  *[f"- {ln}" for ln in carry_notes], ""]
    if project_lines:
        lines += ["## 项目户自足（R-033：项目成本由该项目户出，不计集团缺口；IDR 只算 GIRO）", "",
                  *[f"- {ln}" for ln in project_lines], ""]
    if blank_amt is not None and len(blank_amt):
        lines += ["## ⚠️ 金额空白行（计划表没填金额，缺口表里没有它们——人工补数或找提交人）", ""]
        for _, r in blank_amt.iterrows():
            ent = r["entity"] if pd.notna(r["entity"]) else "（主体也空白）"
            lines.append(f"- {ent}｜{r['currency']}｜{str(r['memo'])[:60]}｜截止 {r['deadline']}"
                         f"｜lark {r['lark_no'] or '未提'}")
        lines.append("")
    if transit_notes:
        lines += ["## 在途调拨（Lark 单 vs 流水）", "", *[f"- {n}" for n in transit_notes], ""]
    if restricted:
        lines += ["## 受限资金（有钱但不可动用）", "",
                  "> 缺口表的「现余额」只含集团可自由动用账户；以下不计入。", ""]
        lines += [f"- {r}" for r in restricted]
        lines.append("")
    lines += ["## 建议动作", *[f"- {a}" for a in actions], ""]
    if warns:
        lines += ["## 需人工裁决", *[f"- {w}" for w in warns], ""]
    if acct_findings:
        warn_n = sum(1 for f in acct_findings if f["level"] == "warn")
        lines += [f"## 账户质检（{warn_n} 项需确认）", ""]
        for f in acct_findings:
            lines.append(f"- {'⚠️' if f['level'] == 'warn' else 'ℹ️'} {f['msg']}")
        lines.append("")
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
    ap.add_argument("--check-accounts", action="store_true",
                    help="账户质检：流水 vs 余额一致性（VA 轮换静默归一，真新账户报警）")
    ap.add_argument("--transfers", help="在途调拨：transfers.yaml 或 Lark「调拨申请」导出 xlsx（手动/API 版皆可）")
    ap.add_argument("--rules", default=None, help="缺省 CASHFLOW_ROOT/rules/advisor_rules.yaml")
    ap.add_argument("--map", dest="emap", default=None, help="缺省 CASHFLOW_ROOT/rules/advisor_entity_map.yaml")
    ap.add_argument("--fx-usdmxn", type=float, default=17.5)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    rules_path = cfg_path("advisor_rules.yaml", a.rules)
    emap_path = cfg_path("advisor_entity_map.yaml", a.emap)
    week, plan = load_plan_week(a.plan, a.week, keep_blank_amount=True)
    blank_amt = plan[plan["amount"].isna()].reset_index(drop=True)   # 金额空白门：单列，不计缺口
    plan = plan[plan["amount"].notna()].reset_index(drop=True)
    bal = load_balances(a.balances)
    rules = load_rules(rules_path)
    emap = load_entity_map(emap_path)
    paid = load_yaml(a.paid, "paid") if a.paid else []
    transfers = load_transfers_any(a.transfers, emap) if a.transfers else []

    plan, paid_notes = net_paid(plan, paid)  # 人工确认的优先核销
    ambig: list[str] = []
    flows = None
    if a.liushui:
        start = pd.Timestamp(week.split("-")[0].replace(".", "-"))
        since = (start - pd.Timedelta(days=a.liushui_days)).strftime("%Y-%m-%d")
        flows = load_liushui(a.liushui, since=since)   # 全量出账：在途判定/账户质检用
        plan, auto_notes, ambig = auto_net_from_liushui(
            plan, payment_flows(flows, rules), week_start=start, emap=emap)
        paid_notes += auto_notes
    transit_notes = mark_executed(transfers, flows)
    if flows is not None:
        # 发起时间早于流水窗口的单：执行与否无从判定（可能已在更早的流水里执行），不计在途只提示
        since_ts = flows["date"].min() if len(flows) else None
        for t in transfers:
            d = t.get("date")
            if since_ts is not None and d is not None and pd.notna(d) and d < since_ts and not t.get("executed"):
                t["unjudgeable"] = True
    flag_stale_transfers([t for t in transfers if not t.get("executed") and not t.get("arrived")
                          and not t.get("unjudgeable")], bal)
    alias = load_alias(cfg_path("entity_alias.yaml"))
    carry = load_yaml(cfg_path("carryover.yaml"), "carryover")
    plan, carry_notes = append_carryover(plan, carry)
    if flows is not None and carry:
        plan, c_auto, c_amb = auto_net_from_liushui(plan, payment_flows(flows, rules), week_start=start, emap=emap)
        paid_notes += [n + "——滚存项已付，可从 carryover.yaml 删除" for n in c_auto if "滚存" in n]
        ambig += c_amb
    plan, project_lines = project_self_funded(plan, bal, rules, emap, alias)
    needs, blank = entity_needs(plan, emap)
    avail = entity_avail(bal, emap, rules)
    gaps = compute_gaps(needs, avail, transfers)
    actions, warns = route(gaps, bal, rules, a.fx_usdmxn)
    warns += ambig
    warns += stale_rule_data(rules)
    stale = [t for t in transfers if t.get("executed") and str(t.get("status", "")).startswith("审批中")]
    if stale:
        warns.append(f"Lark 状态滞后：{len(stale)} 单「审批中」流水已执行——{'、'.join(t['lark_no'] for t in stale)}；"
                     "在途意图以流水为准，别重复执行")
    pending = [t for t in transfers if not t.get("executed") and not t.get("arrived")
               and str(t.get("status", "")) in ("审批中", "已同意")]
    suspects = [f"{t['lark_no']} {t.get('currency')} {float(t['amount']):,.0f}（{t['from_account']}）"
                for t in pending if t.get("suspect")]
    if suspects:
        warns.append("在途单疑似过时（付款账户现余额 < 单上金额，已不计在途）："
                     + "；".join(suspects))
    old = [t for t in pending if t.get("unjudgeable")]
    if old:
        transit_notes.append(f"ℹ️ {len(old)} 单发起时间早于流水窗口（{flows['date'].min():%m-%d} 前），执行与否无法判定，"
                             f"未计在途：{'、'.join(t['lark_no'] for t in old)}")
    for t in pending:
        if t.get("unjudgeable"):
            continue
        if t.get("suspect"):
            tag = "（疑似过时，未计入）"
        elif "理财" in str(t.get("kind", "")):
            tag = "（理财申购，不是到账）"
        elif (t.get("to_currency") or t["currency"]) != t["currency"]:
            tag = f"（换汇单，到账 {t.get('to_currency')} 未折算，未计入）"
        elif t.get("to_entity"):
            tag = "（已计入在途）"
        else:
            tag = "（收款方不在映射表，未计入）"
        transit_notes.append(f"{t['lark_no']} {t.get('currency')} {float(t['amount']):,.0f} → "
                             f"{t.get('to_entity') or t.get('to_company', '')[:14]}｜{t.get('status')}｜未见流水"
                             f"{tag}｜{t.get('reason', '')[:50]}")
    findings = []
    if a.check_accounts:
        if flows is None:
            warns.append("--check-accounts 需要同时给 --liushui（流水），本次跳过质检")
        else:
            findings = acct.check(bal, flows, rules)
    md = render(week, gaps, blank, actions, warns, paid_notes,
                {"计划表": a.plan, "余额": a.balances, "规则": str(rules_path),
                 "流水": a.liushui or "未提供（自动核销未启用）",
                 "在途单": a.transfers or "未提供",
                 "approved 规则数": len(rules)},
                paid_provided=bool(a.paid or a.liushui),
                restricted=restricted_summary(bal, emap, rules),
                acct_findings=findings, transit_notes=transit_notes, blank_amt=blank_amt,
                project_lines=project_lines, carry_notes=carry_notes)
    out_dir = Path(a.out) if a.out else get_root() / "advice"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"advice-{week.replace('.', '')[:8]}.md"
    out.write_text(md, encoding="utf-8")
    print(f"建议单 → {out}")
    for w in warns:
        print(f"  [人工] {w}")
    for f in findings:
        if f["level"] == "warn":
            print(f"  [账户] {f['msg']}")


if __name__ == "__main__":
    main()
