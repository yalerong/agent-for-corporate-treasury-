"""调拨建议引擎 v0：解析纪律（主体空白保留）、已付核销门、缺口、规则路由。"""
import advisor
import advisor_accounts as acct
import advisor_inputs as ai
import pandas as pd
import pytest
import yaml

RULES = [
    {"id": "R-001", "type": "independent_entity", "status": "approved",
     "params": {"entity": "HK ALPHA"}},
    {"id": "R-002", "type": "fx_pool", "status": "approved",
     "params": {"side": "NORTH", "currency": "MXN",
                "accounts": ["pay_NORTH_CW", "PayB-NORTH-fintek1"]}},
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
                  ("NORTH SA", "PayB-NORTH-fintek1", "MXN", 2000000.0),
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

# ---------- v0.1 账户分层 / 归一 / 质检 ----------

ACCT_RULES = RULES + [
    {"id": "R-017", "type": "account_classification", "status": "approved", "params": {
        "channel_patterns": ["_LINKPAY_", "_PayOne_", "_RELAY_"],
        "project_suffix_patterns": {"_PJA": "PJA", "_PJB": "PJB"},
        "project_prefix_patterns": {"PJA-": "PJA", "QuickCash-": "PJB"}}},
    {"id": "R-012", "type": "earmarked_account", "status": "approved",
     "params": {"account": "X_XBANK_USD_4821", "purpose": "菲律宾代付专用"}},
]
ACCT_BAL = bal_df([
    ("GAMMA LTD", "X_CBANK_USD_7501", "USD", 400000.0),      # group 可动用
    ("GAMMA LTD", "X_XBANK_USD_4821", "USD", 84000.0),         # earmarked 剔除
    ("GAMMA LTD", "X_LBANK_IDR_GIRO_8806_PJA", "IDR", 1.2e9),  # project 剔除
    ("GAMMA LTD", "PJA-RDL-X", "IDR", 1.5e9),               # project(前缀) 剔除
    ("GAMMA LTD", "X_LINKPAY_IDR_1789", "IDR", 0.0),        # channel 剔除
    ("NORTH SA", "pay_NORTH_CW", "MXN", 3000000.0),          # fx_pool 内 → group
    ("NORTH SA", "PayB-NORTH-fintek1", "MXN", 2000000.0),  # fx_pool 内 → group
    ("NORTH SA", "PayB-NORTH-CORE3", "MXN", 2500000.0),     # exclusive → business 剔除
])


def test_normalize_strips_va_only_for_channel():
    assert acct.normalize("X_LINKPAY_IDR_6464", ACCT_RULES) == "X_LINKPAY_IDR"
    assert acct.normalize("X_LINKPAY_IDR_1789", ACCT_RULES) == "X_LINKPAY_IDR"
    # 非通道户不动尾号（LBANK 尾号是账号不是 VA）
    assert acct.normalize("X_LBANK_IDR_GIRO_8806_PJA", ACCT_RULES) == "X_LBANK_IDR_GIRO_8806_PJA"
    assert acct.normalize("X_CBANK_USD_7501", ACCT_RULES) == "X_CBANK_USD_7501"


def test_classify_scopes():
    ann = acct.annotate(ACCT_BAL, ACCT_RULES)
    got = dict(zip(ann["account"], ann["scope"], strict=False))
    assert got["X_CBANK_USD_7501"] == "group"
    assert got["X_XBANK_USD_4821"] == "earmarked"
    assert got["X_LBANK_IDR_GIRO_8806_PJA"] == "project"
    assert got["PJA-RDL-X"] == "project"
    assert got["X_LINKPAY_IDR_1789"] == "channel"
    assert got["pay_NORTH_CW"] == "group"          # 财务控制池算可动用
    assert got["PayB-NORTH-CORE3"] == "business"  # exclusive → 业务控制
    assert dict(zip(ann["account"], ann["scope_detail"], strict=False))[
        "X_LBANK_IDR_GIRO_8806_PJA"] == "PJA"


def test_entity_avail_excludes_restricted():
    """多身份主体陷阱：整体加总会虚增（XBANK 专用+项目户+通道户都不是可动用）。"""
    emap = {"entities": {"HK GAMMA": "GAMMA LTD", "MX NORTH": "NORTH SA"},
            "channel_overrides": {}}
    naive = advisor.entity_avail(ACCT_BAL, emap)                 # 旧行为：整体加总
    strict = advisor.entity_avail(ACCT_BAL, emap, ACCT_RULES)    # v0.1：只算 group
    g = lambda df, e, c: float(df[(df["entity"] == e) & (df["currency"] == c)]["avail"].sum())  # noqa: E731
    assert g(naive, "HK GAMMA", "USD") == 484000.0
    assert g(strict, "HK GAMMA", "USD") == 400000.0   # 剔掉 XBANK 8.4 万
    assert g(strict, "HK GAMMA", "IDR") == 0.0        # 项目户+通道户全剔
    assert g(strict, "MX NORTH", "MXN") == 5000000.0  # 只认财务控制两户


def test_check_accounts_va_vs_new():
    flows = pd.DataFrame({
        "date": pd.to_datetime(["2026-07-28", "2026-07-29", "2026-07-30"]),
        "currency": ["IDR"] * 3, "amount": [1.0, 2.0, 3.0],
        "payee": ["X_LINKPAY_IDR_6464",        # VA 轮换 → info
                  "Y_LINKPAY_IDR_9999",        # 规范键也没有 → warn
                  "X_CBANK_USD_7501"],          # 已知 → 无
        "memo": ["同名划转：X_LBANK_IDR_GIRO_8806_PJA 调拨至 X_LINKPAY_IDR_1789", "", ""]})
    fs = acct.check(ACCT_BAL, flows, ACCT_RULES)
    infos = [f for f in fs if f["level"] == "info"]
    warns = [f for f in fs if f["level"] == "warn"]
    assert any("VA 轮换已归一" in f["msg"] and "6464" in f["msg"] for f in infos)
    assert any("9999" in f["msg"] and "查无" in f["msg"] for f in warns)
    # payee 6464 与摘要 1789 归一后同源 → 不报行内矛盾
    assert not any("行内矛盾" in f["msg"] for f in warns)


def test_check_case_difference_is_not_new_account():
    """真数据回归：上游流水写 payx_NORTH_CW、余额写 PAYX_NORTH_CW —
    同一账户两个标签，应静默归一为 info，不得报"疑似新账户"。"""
    bal = bal_df([("NORTH SA", "PAYX_NORTH_CW", "MXN", 3000000.0),
                  ("D LTD", "DELTA_Custody_USD", "USD", 21367.0)])
    flows = pd.DataFrame({
        "date": pd.to_datetime(["2026-07-28", "2026-07-29"]),
        "currency": ["MXN", "USD"], "amount": [1.0, 2.0],
        "payee": ["payx_NORTH_CW", "DELTA_CUSTODY_USD"],
        "memo": ["划转至 PAYX_NORTH_CW", "转入 DELTA_Custody_USD"]})
    fs = acct.check(bal, flows, ACCT_RULES)
    assert not [f for f in fs if f["level"] == "warn"], f"不该有 warn: {fs}"
    assert sum("大小写差异已归一" in f["msg"] for f in fs) == 2


def test_check_flags_cross_account_mismatch():
    flows = pd.DataFrame({
        "date": pd.to_datetime(["2026-07-28"]), "currency": ["USD"], "amount": [1.0],
        "payee": ["X_LINKPAY_IDR_6464"],
        "memo": ["实际转入 PJA-RDL-X"]})   # 与收款方不同源 → warn
    fs = acct.check(ACCT_BAL, flows, ACCT_RULES)
    assert any("行内矛盾" in f["msg"] for f in fs if f["level"] == "warn")


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
