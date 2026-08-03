"""调拨建议引擎 v0：解析纪律（主体空白保留）、已付核销门、缺口、规则路由。"""
import advisor
import advisor_inputs as ai
import pandas as pd
import pytest
import yaml

RULES = [
    {"id": "R-001", "type": "independent_entity", "status": "approved",
     "params": {"entity": "HK ALPHA"}},
    {"id": "R-002", "type": "fx_pool", "status": "approved",
     "params": {"side": "NORTH", "currency": "MXN",
                "accounts": ["pay_NORTH_CW", "opmPay-NORTH-fintek1"]}},
    {"id": "R-003", "type": "fx_route", "status": "approved",
     "params": {"entity": "MX BETA", "side": "NORTH"}},
    {"id": "R-004", "type": "usdt_hub", "status": "approved",
     "params": {"hub": "HUB_Ledger", "sweep_accounts": ["P1_Ledger"]}},
    {"id": "R-005", "type": "weekly_inflow", "status": "approved",
     "params": {"currency": "USDT", "low": 100000, "high": 150000}},
    {"id": "R-006", "type": "usdt_wealth_unlocked", "status": "approved",
     "params": {"amount": 100000}},
    {"id": "R-007", "type": "lender", "status": "approved",
     "params": {"entity": "HK GAMMA", "currencies": ["USD"]}},
]
EMAP = {"entities": {"HK GAMMA": "GAMMA LTD", "MX BETA": "BETA SA",
                     "HK ALPHA": "ALPHA LTD", "MX NORTH": "NORTH SA"},
        "channel_overrides": {"某某卡": "个人账户X"}}


def plan_df(rows):
    cols = list(ai.PLAN_COLS.values())
    df = pd.DataFrame(rows, columns=cols)
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df["lark_no"] = df["lark_no"].fillna("").astype(str)
    return df


def row(entity, amount, ccy, channel="公户", memo="x", lark=""):
    return [entity, amount, ccy, "对外支付", "公对公", channel, memo,
            "", "部门", "项目", "人", "否", lark]


def bal_df(rows):
    return pd.DataFrame(rows, columns=["company", "account", "currency", "balance"])


# ---------- 解析纪律 ----------

def test_plan_parser_keeps_blank_entity(tmp_dir):
    """2026-08-02 实战教训：主体空白行（cosmio 175万）绝不静默丢弃。"""
    grid = [["填制说明", *[""] * 15]]
    grid.append(["2026.08.03-2026.08.07", "*付款主体", "*预算金额", "*币种", *[""] * 12])
    grid.append(["", "HK GAMMA", 1000, "USD", "对外支付", "公对公", "公户", "a",
                 "", "d", "p", "s", "否", "", "", ""])
    grid.append(["", None, 1747550, "MXN", "对外支付", "公对公", "公户", "cosmio",
                 "严格8/3", "d", "p", "s", "是", "202607300012", "", ""])
    grid.append(["2026.07.27-2026.07.31", *[""] * 15])
    grid.append(["", "OLD", 5, "USD", *[""] * 11])
    xlsx = tmp_dir / "plan.xlsx"
    with pd.ExcelWriter(xlsx) as w:
        pd.DataFrame(grid).to_excel(w, sheet_name="资金预算-周预估", header=False, index=False)
    week, blk = ai.load_plan_week(xlsx)
    assert week == "2026.08.03-2026.08.07"
    assert len(blk) == 2  # 空白主体行保留
    assert blk["entity"].isna().sum() == 1
    assert blk.loc[blk["entity"].isna(), "lark_no"].iloc[0] == "202607300012"
    # 指定旧周也能取
    week2, blk2 = ai.load_plan_week(xlsx, "2026.07.27-2026.07.31")
    assert week2.startswith("2026.07.27") and len(blk2) == 1


# ---------- 已付核销门 ----------

def test_net_paid_by_lark_and_amount():
    plan = plan_df([row("HK GAMMA", 294381.72, "USD", lark="202607270017"),
                    row("HK GAMMA", 530000, "USDT"),
                    row("HK GAMMA", 300000, "USD")])
    out, notes = advisor.net_paid(plan, [
        {"lark_no": "202607270017", "note": "日记账7/29核实"},
        {"entity": "HK GAMMA", "currency": "USDT", "amount": 530000, "note": "上周已付"},
        {"lark_no": "999", "note": "不存在"}])
    assert len(out) == 1 and float(out["amount"].iloc[0]) == 300000
    assert sum("剔除已付" in n for n in notes) == 2
    assert any("未匹配" in n for n in notes)


def test_auto_net_from_liushui():
    """流水自动核销：唯一命中剔除；同额多笔只提示；一笔流水不核两行。"""
    plan = plan_df([row("HK GAMMA", 294381.72, "USD"),   # 流水唯一命中 → 核销
                    row("HK GAMMA", 5000, "USD"),        # 流水两笔同额 → 含糊
                    row("MX BETA", 5000, "USD"),         # 同上（且不许复用同一笔流水）
                    row("HK GAMMA", 777, "HKD")])        # 无命中 → 保留
    flows = pd.DataFrame({
        "date": pd.to_datetime(["2026-07-29", "2026-07-30", "2026-07-31"]),
        "currency": ["USD", "USD", "USD"],
        "amount": [294381.72, 5000.0, 5000.0],
        "payee": ["葛某", "甲", "乙"], "memo": ["", "", ""]})
    out, notes, ambig = advisor.auto_net_from_liushui(plan, flows)
    assert len(out) == 3 and 294381.72 not in out["amount"].values
    assert sum("自动核销" in n for n in notes) == 1
    assert len(ambig) == 2 and all("同额命中" in s for s in ambig)


# ---------- 缺口与在途 ----------

def test_gaps_with_transit_and_channel_override():
    plan = plan_df([row("HK GAMMA", 400000, "USD"),
                    row("HK GAMMA", 250000, "CNY", channel="某某卡")])
    needs, blank = advisor.entity_needs(plan, EMAP)
    assert len(blank) == 0
    avail = advisor.entity_avail(
        bal_df([("GAMMA LTD", "g1", "USD", 300000.0), ("个人账户X", "k1", "CNY", 1000000.0)]),
        EMAP)
    gaps = advisor.compute_gaps(needs, avail, [
        {"to_entity": "HK GAMMA", "currency": "USD", "amount": 50000, "arrived": False},
        {"to_entity": "HK GAMMA", "currency": "USD", "amount": 99999, "arrived": True}])
    g_usd = gaps[(gaps["entity"] == "HK GAMMA") & (gaps["currency"] == "USD")].iloc[0]
    assert g_usd["gap"] == pytest.approx(400000 - 300000 - 50000)  # arrived 的不重复计
    g_card = gaps[gaps["entity"] == "某某卡"].iloc[0]
    assert g_card["gap"] < 0  # 个人卡余额富余


# ---------- 规则路由 ----------

def test_route_independent_fx_lender_usdt():
    gaps = pd.DataFrame([
        {"entity": "HK ALPHA", "currency": "HKD", "need": 10, "avail": 0, "transit": 0,
         "gap": 100000.0},
        {"entity": "MX NORTH", "currency": "MXN", "need": 0, "avail": 0, "transit": 0,
         "gap": -1.0},
        {"entity": "MX BETA", "currency": "USD", "need": 0, "avail": 0, "transit": 0,
         "gap": 45000.0},
        {"entity": "HK GAMMA", "currency": "USD", "need": 0, "avail": 0, "transit": 0,
         "gap": -500000.0},
        {"entity": "MX SOUTH", "currency": "USD", "need": 0, "avail": 0, "transit": 0,
         "gap": 60000.0},
        {"entity": "HK DELTA", "currency": "USDT", "need": 0, "avail": 0, "transit": 0,
         "gap": 150000.0},
    ])
    bal = bal_df([("NORTH SA", "pay_NORTH_CW", "MXN", 3000000.0),
                  ("NORTH SA", "opmPay-NORTH-fintek1", "MXN", 2000000.0),
                  ("X", "HUB_Ledger", "USDT", 50000.0),
                  ("Y", "P1_Ledger", "USDT", 37000.0)])
    actions, warns = advisor.route(gaps, bal, RULES, fx_usdmxn=17.5)
    text = "\n".join(actions)
    assert any("独立主体" in w for w in warns)                    # ALPHA 不许外部调入
    assert "MX BETA 缺 45,000 USD" in text and "NORTH 侧池换汇" in text
    assert "787,500 MXN" in text                                   # 45000×17.5
    assert "HK GAMMA 拆借" in text and "富余够" in text            # SOUTH 60K < 500K
    assert "[USDT]" in text and "归集" in text
    # 150K 缺口 − 37K 归集 − 100K 回流 = 13K → 已解锁理财 100K 兜住
    assert any("赎回已解锁理财" in a for a in actions)
    assert not any("USDT 缺" in w for w in warns)


def test_rules_only_approved(tmp_dir):
    p = tmp_dir / "r.yaml"
    p.write_text(yaml.safe_dump({"rules": [
        {"id": "A", "status": "approved", "type": "lender", "params": {}},
        {"id": "B", "status": "candidate", "type": "lender", "params": {}},
        {"id": "C", "status": "refuted", "type": "lender", "params": {}}]},
        allow_unicode=True), encoding="utf-8")
    rules = ai.load_rules(p)
    assert [r["id"] for r in rules] == ["A"]


# ---------- 建议单 gate ----------

def test_render_flags_missing_paid_gate():
    gaps = pd.DataFrame([{"entity": "E", "currency": "USD", "need": 1.0, "avail": 0.0,
                          "transit": 0.0, "gap": 1.0}])
    blank = plan_df([row(None, 999, "MXN", memo="主体空白", lark="123")])
    md = advisor.render("2026.08.03-2026.08.07", gaps, blank, ["a"], ["w"], [],
                        {"计划表": "p.xlsx"}, paid_provided=False)
    assert "🚩" in md and "未提供已付核销清单" in md
    assert "主体空白行" in md and "123" in md
    md2 = advisor.render("w", gaps, blank.iloc[0:0], [], [], [], {}, paid_provided=True)
    assert "🚩" not in md2
