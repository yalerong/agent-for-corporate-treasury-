"""指标层：报告口径唯一登记于 metrics.yaml，此处按 id 注册纯函数计算。

compute_all(ctx) 返回 {metric_id: {"value": ..., "lineage": {...}}}；
lineage = {pattern_ids, data_range, db_row_count, patterns_generated_at, computed_at}，
整 run 由 engine 落 runs/<date>/lineage.json，report.md 各节标注 metric_id。
value 是结构化数据（DataFrame/dict/list），排版一律在 engine.build_report。

审批门控在 build_context 定：strict 下计算口径=status==approved 的预测行，
否则=confidence==high；refuted 任何模式不进预测。
"""
import sqlite3
from datetime import timedelta

import pandas as pd
import pattern_store as ps
import yaml
from constants import BUDGET_EXCLUDE, CODE_DIR, GROUP, get_root

ROOT = get_root()
DB = ROOT / "data" / "db" / "treasury.db"
PAT = ROOT / "patterns" / "patterns.yaml"

REGISTRY = {}


def metric(mid: str):
    def deco(fn):
        REGISTRY[mid] = fn
        return fn
    return deco


def load_registry() -> list[dict]:
    doc = yaml.safe_load((CODE_DIR / "metrics.yaml").read_text(encoding="utf-8"))
    return doc["metrics"]


def build_context(asof_arg: str | None, strict_flag: bool) -> dict:
    """加载全部输入并定门控。asof 缺省=规律库数据末日；strict 在 approved=0 时回退过渡模式。"""
    pats = ps.load(PAT)
    seed = pd.Timestamp(asof_arg) if asof_arg else pd.Timestamp(pats["meta"]["data_range"][1])
    con = sqlite3.connect(DB)
    pay = pd.read_sql("SELECT * FROM payments WHERE date<=?", con,
                      params=(seed.strftime("%Y-%m-%d"),), parse_dates=["date"])
    con.close()
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
    asof = pay["date"].max()
    strict, transition = strict_flag, False
    if strict and not any(ps.status_of(p) == "approved" for p in pats["patterns"]):
        strict, transition = False, True
    return {"pay": pay, "pats": pats, "bud": bud, "rp": rp, "asof": asof,
            "month": asof.strftime("%Y-%m"), "strict": strict, "transition": transition}


def compute_all(ctx: dict) -> dict:
    ids = [m["id"] for m in load_registry()]
    if set(ids) != set(REGISTRY):
        raise SystemExit(f"metrics.yaml 与 REGISTRY 不同步: {sorted(set(ids) ^ set(REGISTRY))}")
    out = {}
    for mid in ids:
        value, pattern_ids = REGISTRY[mid](ctx, out)
        out[mid] = {"value": value, "lineage": {
            "pattern_ids": sorted(set(pattern_ids)),
            "data_range": ctx["pats"]["meta"]["data_range"],
            "db_row_count": ctx["pats"]["meta"]["rows"],
            "patterns_generated_at": ctx["pats"]["meta"]["generated_at"],
            "computed_at": ps.now_iso(),
        }}
    return out


def calc_rows(fc: pd.DataFrame, strict: bool) -> pd.DataFrame:
    """进计算的预测行：strict=仅人工批准，否则按置信度。"""
    return fc[fc["status"] == "approved"] if strict else fc[fc["confidence"] == "high"]


# ---------- 预算 ----------

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


@metric("budget_variance")
def budget_variance_metric(ctx, _out):
    bud = ctx["bud"]
    if bud is None:
        return None, []
    month = ctx["month"]
    bud_month = month if (bud["month"] == month).any() else bud["month"].max()
    m, causes = budget_variance(ctx["pay"], bud, bud_month)
    return {"month": bud_month, "is_fallback_month": bud_month != month,
            "table": m, "causes": causes}, []


# ---------- 预测 ----------

@metric("forecast_4w")
def forecast_4w_metric(ctx, _out):
    pats, asof = ctx["pats"], ctx["asof"]
    # refuted（人工否决）的规律不进任何预测行
    usable = [p for p in pats["patterns"] if ps.status_of(p) != "refuted"]
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
                         "confidence": p["confidence"], "status": ps.status_of(p),
                         "pattern_id": p.get("id") or ps.pattern_id(p["type"], p["key"]),
                         "note": note})
        for p in recs:
            due = [d for d in pd.date_range(w0, w1) if d.day == p["day_of_month"]]
            if due:
                rows.append({"entity": "", "project": "", **p["key"], "week": f"W+{i}",
                             "start": w0.date(), "forecast": p["avg_amount"],
                             "source": f"recurring@{p['day_of_month']}日",
                             "confidence": p["confidence"], "status": ps.status_of(p),
                             "pattern_id": p.get("id") or ps.pattern_id(p["type"], p["key"]),
                             "note": ""})
    fc = pd.DataFrame(rows)
    if "payee" not in fc.columns:
        fc["payee"] = ""
    return fc, ([] if fc.empty else list(fc["pattern_id"]))


# ---------- 头寸与调拨 ----------

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


@metric("position")
def position_metric(ctx, out):
    """币种级头寸 + 主体级余缺贪心互补（preview-only，扣减捐出方额度，残余明示）。"""
    bal = load_balances(ctx["asof"])
    if bal is None:
        return None, []
    fc = out["forecast_4w"]["value"]
    calc = calc_rows(fc, ctx["strict"])
    out_cur = calc.groupby("currency")["forecast"].sum()
    bal_cur = bal.groupby("currency")["balance"].sum()
    currency_rows, fx_gap = [], {}
    for cur in sorted(set(bal_cur.index) | set(out_cur.index)):
        b, o = float(bal_cur.get(cur, 0.0)), float(out_cur.get(cur, 0.0))
        fx_gap[cur] = max(0.0, o - b)
        currency_rows.append({"currency": cur, "balance": b, "outflow": o, "position": b - o})

    out_ent = split_outflow_by_entity(calc, pay=ctx["pay"])
    bal_ent = bal.groupby(["entity", "currency"])["balance"].sum()
    ent_pos = bal_ent.sub(out_ent, fill_value=0.0)
    avail = {k: float(v) for k, v in ent_pos[ent_pos > 0].items()}
    events = []
    for (ent, cur), pos in ent_pos[ent_pos < 0].sort_values().items():
        need = -float(pos)
        donors = sorted((k for k in avail if k[1] == cur and avail[k] > 0),
                        key=lambda k: -avail[k])
        for d in donors:
            take = min(need, avail[d])
            events.append({"kind": "transfer", "donor": d[0], "recipient": ent,
                           "currency": cur, "amount": take, "need_before": need,
                           "donor_avail_before": avail[d]})
            avail[d] -= take
            need -= take
            if need <= 0:
                break
        if need > 0:
            events.append({"kind": "residual", "entity": ent, "currency": cur, "amount": need})
    value = {"snapshot_date": bal["as_of"].max(), "currency_rows": currency_rows,
             "events": events, "fx_gap": fx_gap}
    return value, ([] if calc.empty else list(calc["pattern_id"]))


# ---------- 外汇 ----------

@metric("fx_advice")
def fx_advice_metric(ctx, out):
    """有余额数据时只对余额覆盖不了的缺口给购汇建议；无余额数据退回全额口径。"""
    fc = out["forecast_4w"]["value"]
    calc = calc_rows(fc, ctx["strict"])
    pos = out["position"]["value"]
    fx_gap = None if pos is None else pos["fx_gap"]
    bud, month = ctx["bud"], ctx["month"]
    items = []
    for cur, outflow in calc.groupby("currency")["forecast"].sum().items():
        need = float(outflow) if fx_gap is None else fx_gap.get(cur, 0.0)
        if fx_gap is not None and need <= 0:
            items.append({"currency": cur, "outflow": float(outflow), "covered": True,
                          "need": 0.0, "cap": None})
            continue
        cap = None
        if bud is not None:
            cap = bud[(bud["month"] == month) & (bud["currency"] == cur)]["budget"].sum()
        items.append({"currency": cur, "outflow": float(outflow), "covered": False,
                      "need": float(need), "cap": float(cap) if cap else None})
    return {"items": items, "has_balance": fx_gap is not None}, \
        ([] if calc.empty else list(calc["pattern_id"]))


# ---------- 关联方 ----------

@metric("related_party")
def related_party_metric(ctx, _out):
    pay, rp, month = ctx["pay"], ctx["rp"], ctx["month"]
    mask = pay["payee"].isin(rp["payees"])
    for kw in rp["project_keywords"]:
        mask |= pay["project"].str.contains(kw, na=False)
    g = pay[mask].copy()
    if g.empty:
        return {"monthly": None, "cur": None}, []
    g["month"] = g["date"].dt.strftime("%Y-%m")
    monthly = (g.groupby(["month", "project", "currency"])["amount"]
               .agg(["sum", "count"]).reset_index())
    cur = monthly[monthly["month"] == month]
    return {"monthly": monthly, "cur": cur}, []


# ---------- 审批画像 ----------

@metric("approvals_profile")
def approvals_profile_metric(_ctx, _out):
    con = sqlite3.connect(DB)
    try:
        ap = pd.read_sql("SELECT * FROM approvals", con)
    except Exception:
        return None, []
    finally:
        con.close()
    if ap.empty:
        return None, []
    ap["month"] = ap["created"].str.slice(0, 7)
    kinds = []
    for kind, g in ap.groupby("kind"):
        kinds.append({"kind": kind, "n": len(g),
                      "ok": float((g["status"] == "已同意").mean()),
                      "med": float(g["elapsed_hours"].median()),
                      "per_month": len(g) / g["month"].nunique(),
                      "natures": list(g["nature"].value_counts().head(3).items())})
    slow = (ap.nlargest(3, "elapsed_hours")[["kind", "apply_no", "elapsed_hours"]]
            .to_dict("records"))
    return {"kinds": kinds, "slow": slow}, []
