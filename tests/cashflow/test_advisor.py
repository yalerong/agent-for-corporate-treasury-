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
    """2026-08-02 实战教训：主体空白行（拉美出资 175万）绝不静默丢弃。"""
    grid = [["填制说明", *[""] * 15]]
    grid.append(["2026.08.03-2026.08.07", "*付款主体", "*预算金额", "*币种", *[""] * 12])
    grid.append(["", "HK GAMMA", 1000, "USD", "对外支付", "公对公", "公户", "a",
                 "", "d", "p", "s", "否", "", "", ""])
    grid.append(["", None, 1747550, "MXN", "对外支付", "公对公", "公户", "拉美出资",
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


# ---------- v0.1：在途单吃 Lark 导出 + 流水判执行 ----------

def _lark_manual_xlsx(path):
    """模拟 Lark 审批后台导出：首行筛选条件，第二行表头，同一申请多明细行。"""
    cols = ["申请编号", "申请状态", "发起时间", "调拨原因", "金额", "金额币种", "调拨性质", "主体", "账号"]
    rows = [
        ["202609990001", "已同意", "2026-09-01 20:38", "同户名划款：PayB-NORTH-fintek1转款MXN 390万 至pay_NORTH_CW",
         3900000, "墨西哥比索", "同名账户调拨", "NORTH SA", "pay_NORTH_CW"],
        ["202609990001", "已同意", "2026-09-01 20:38", "同上（明细第二行，应去重）",
         3900000, "墨西哥比索", "同名账户调拨", "NORTH SA", "pay_NORTH_CW"],
        ["202609990004", "审批中", "2026-09-02 03:46", "关联方划转：从pay_NORTH_CW 换汇调拨MXN 450万至GAMMA_BANKA_USD_5501",
         4500000, "墨西哥比索", "异名调拨-借款往来", "GAMMA LTD", "GAMMA_BANKA_USD_5501"],
        ["202609990003", "审批中", "2026-09-03 20:53", "同户名划转：从HUB_Ledger 调拨 USDT 22万 到 GAMMA_WALLETY_USDT",
         220000, "美元", "同名账户调拨", "GAMMA LTD", "GAMMA_WALLETY_USDT"],
        ["202609990007", "已同意", "2026-09-01 21:08", "GAMMA DBS 购买定存USD 700,000.00",
         700000, "美元", "资金理财", "GAMMA LTD", "GAMMA_DBS_USD_2112"],
    ]
    with pd.ExcelWriter(path) as w:
        pd.DataFrame([["筛选条件： 发起时间：2026-08"] + [None] * (len(cols) - 1)]).to_excel(
            w, index=False, header=False, startrow=0)
        pd.DataFrame(rows, columns=cols).to_excel(w, index=False, startrow=1)


def test_load_transfers_any_manual_export(tmp_dir):
    p = tmp_dir / "调拨申请(全部).xlsx"
    _lark_manual_xlsx(p)
    ts = ai.load_transfers_any(p, EMAP)
    by = {t["lark_no"]: t for t in ts}
    assert len(ts) == 4, "同一申请多明细行按编号去重"
    assert by["202609990001"]["currency"] == "MXN" and by["202609990001"]["to_entity"] == "MX NORTH"
    assert by["202609990001"]["from_account"] == "PayB-NORTH-fintek1"
    fx = by["202609990004"]
    assert fx["currency"] == "MXN" and fx["to_currency"] == "USD", "换汇单：出账 MXN、到账 USD"
    assert by["202609990003"]["currency"] == "USDT", "Lark 把 Ledger 划转写成「美元」，按账户名纠成 USDT"
    assert by["202609990007"]["kind"] == "资金理财"


def test_mark_executed_and_transit(tmp_dir):
    p = tmp_dir / "调拨申请(全部).xlsx"
    _lark_manual_xlsx(p)
    ts = ai.load_transfers_any(p, EMAP)
    flows = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-02", "2026-09-04"]),
        "currency": ["MXN", "USDT"], "amount": [3900000.0, 220000.0],
        "payee": ["pay_NORTH_CW", "GAMMA_WALLETY_USDT"], "memo": ["", ""],
        "account": ["PayB-NORTH-fintek1", "HUB_Ledger"],
        "approval_no": ["202609990001", "202609990003"], "classification": ["同户名划转出款"] * 2})
    notes = advisor.mark_executed(ts, flows)
    by = {t["lark_no"]: t for t in ts}
    assert by["202609990001"]["executed"] and by["202609990003"]["executed"]
    assert not by["202609990004"].get("executed")
    assert any("状态仍是审批中" in n for n in notes), "审批中但流水已执行要点名"
    needs = pd.DataFrame({"entity": ["HK GAMMA", "HK GAMMA", "MX NORTH"],
                          "currency": ["USD", "USDT", "MXN"], "need": [1e6, 1e5, 1e6], "count": [1, 1, 1]})
    avail = pd.DataFrame({"entity": ["HK GAMMA"], "currency": ["USD"], "avail": [0.0]})
    gaps = advisor.compute_gaps(needs, avail, ts)
    g = gaps.set_index(["entity", "currency"])["transit"]
    assert g[("HK GAMMA", "USD")] == 0, "换汇在途（MXN→USD）不折算不计入；理财申购不是到账"
    assert g[("HK GAMMA", "USDT")] == 0, "已执行的不再算在途"
    assert g[("MX NORTH", "MXN")] == 0, "已执行的不再算在途"


def test_payment_flows_whitelist():
    flows = pd.DataFrame({"classification": ["账单出款", "同户名划转出款", "本金出款", "提现出款", ""],
                          "amount": [1, 2, 3, 4, 5]})
    kept = advisor.payment_flows(flows)
    assert kept["amount"].tolist() == [1, 3, 5], "对外出款类 + 基金本金进核销；内部划转/提现不进；无分类列的老导出不过滤"


def test_cfg_path_prefers_root_rules(tmp_dir, monkeypatch):
    (tmp_dir / "rules").mkdir()
    (tmp_dir / "rules" / "advisor_rules.yaml").write_text("rules: []", encoding="utf-8")
    monkeypatch.setenv("CASHFLOW_ROOT", str(tmp_dir))
    assert ai.cfg_path("advisor_rules.yaml") == tmp_dir / "rules" / "advisor_rules.yaml"
    assert ai.cfg_path("advisor_rules.yaml", "x.yaml").name == "x.yaml", "显式路径优先"
    assert ai.cfg_path("nope.yaml").name == "nope.yaml", "root 下没有就退回相对路径"


def test_plan_parser_keeps_blank_amount_when_asked(tmp_dir):
    """2026-09-06 挑错：金额空白但有主体/说明的行（拉美出资 85 万、google play×5）不许静默消失。"""
    grid = [["填制说明", *[""] * 15]]
    grid.append(["2026.09.07-2026.09.11", "*付款主体", "*预算金额", "*币种", *[""] * 12])
    grid.append(["", "HK GAMMA", 1000, "USD", "对外支付", "公对公", "公户", "a", "", "d", "p", "s", "否", "", "", ""])
    grid.append(["", None, None, "MXN", "对外支付", "公对公", "控台", "拉美出资 出资款 5万美金*17", "严格9/8",
                 "d", "p", "s", "是", "202609990006", "", ""])
    grid.append(["", "HK GAMMA", None, "USDT", "对外支付", "私对私", "虚拟账户", "google play咨询费", "9/7",
                 "d", "p", "", "", "", "", ""])
    grid.append(["", None, None, None, *[""] * 12])  # 纯空行不算
    xlsx = tmp_dir / "plan.xlsx"
    with pd.ExcelWriter(xlsx) as w:
        pd.DataFrame(grid).to_excel(w, sheet_name="资金预算-周预估", header=False, index=False)
    _, blk = ai.load_plan_week(xlsx)
    assert len(blk) == 1, "缺省行为不变：只留金额非空"
    _, blk2 = ai.load_plan_week(xlsx, keep_blank_amount=True)
    assert len(blk2) == 3 and blk2["amount"].isna().sum() == 2
    assert "202609990006" in blk2.loc[blk2["amount"].isna(), "lark_no"].tolist()


def test_auto_net_v01_lark_window_and_entity_gate():
    """核销三改：lark 编号跨币种命中；周窗口外的同额不销；付款主体不一致降含糊；USDT 不守门。"""
    emap = {"entities": EMAP["entities"], "channel_overrides": {},
            "account_prefixes": {"GAMMA_": "HK GAMMA", "ALPHA_": "HK ALPHA", "HUB_": "HUB"}}
    plan = plan_df([
        row("HK GAMMA", 301003.28, "USD", memo="CC 广告费", lark="202608990002"),   # CHANNELX 以 IDR 直付
        row("HK ALPHA", 253062, "USD", memo="CC 广告费", lark="202608990002"),      # 同一 lark 挂两行
        row("HK GAMMA", 50000, "USDT", memo="拉美出资 尾款"),                        # 上周同额，本周窗口外
        row("HK ALPHA", 5000, "USD", memo="风控充值"),                             # GAMMA 付了同额 → 主体不一致
        row("HK ALPHA", 900, "USDT", memo="坐席费"),                               # hub 代付 USDT 不守门
    ])
    flows = pd.DataFrame({
        "date": pd.to_datetime(["2026-08-26", "2026-08-26", "2026-08-26", "2026-09-01", "2026-09-03"]),
        "currency": ["IDR", "IDR", "USDT", "USD", "USDT"],
        "amount": [4505124305.0, 5361000000.0, 50000.0, 5000.0, 900.0],
        "payee": ["ADVENDOR", "ADVENDOR", "NETCO", "CLOUDVENDOR", "x"], "memo": [""] * 5,
        "account": ["ALPHA_CHANNELX_IDR", "GAMMA_CHANNELX_IDR", "HUB_Ledger", "GAMMA_BANKA_USD", "HUB_Ledger"],
        "approval_no": ["202608990002", "202608990002", "202608990010", "202608990009", ""],
        "classification": ["账单出款"] * 5})
    out, notes, ambig = advisor.auto_net_from_liushui(
        plan, flows, week_start=pd.Timestamp("2026-08-31"), emap=emap)
    assert sum("lark 202608990002" in n and "跨币种" in n for n in notes) == 2, "同 lark 两行各销各的"
    assert any("GAMMA" in n and "5,361,000,000" in n for n in notes), "按付款主体挑对那一笔"
    assert 50000 in out["amount"].values, "窗口外同额不销（拉美出资 四连击）"
    assert 5000 in out["amount"].values and any("主体不一致" in s for s in ambig)
    assert 900 not in out["amount"].values, "USDT 由 hub 代付，不守门"
    assert len(out) == 2


def test_route_payment_path_beats_fx_pool():
    """R-020 型：主体有明确兜底路径时，不再路由到侧池直付。"""
    rules = RULES + [{"id": "R-020", "type": "payment_path", "status": "approved",
                      "params": {"entity": "MX BETA", "fallback_mxn": "控台代付",
                                 "localbank_status": "受限"}}]
    gaps = pd.DataFrame({"entity": ["MX BETA"], "currency": ["MXN"], "need": [85349.0],
                         "avail": [0.0], "transit": [0.0], "gap": [85349.0]})
    bal = bal_df([("NORTH SA", "pay_NORTH_CW", "MXN", 2e6)])
    actions, warns = advisor.route(gaps, bal, rules, 17.0)
    assert len(actions) == 1 and "控台代付" in actions[0] and "R-020" in actions[0]
    assert "侧财务池" not in actions[0]


def test_project_self_funded_uses_giro_only():
    """R-033：项目行由项目户自付；IDR 只算 GIRO（RDL 是过账通道）；项目从「所属项目」或 memo 推。"""
    rules = [{"id": "R-014", "type": "account_scope", "status": "approved",
              "params": {"project_suffix_patterns": ["_PJ1", "-PJ1"]}}]
    # 用 annotate 的真实分类逻辑太重，这里直接 monkeypatch 成最小实现
    ann = pd.DataFrame({
        "company": ["GAMMA LTD", "GAMMA LTD", "GAMMA LTD"],
        "account": ["GAMMA_BANKB_IDR_GIRO_8806_PJ1", "PJ1-RDL-gamma", "GAMMA_BANKA_USD"],
        "currency": ["IDR", "IDR", "USD"], "balance": [2.0e9, 5.0e9, 1e5],
        "canonical": [""] * 3, "scope": ["project", "project", "group"],
        "scope_detail": ["PJ1", "PJ1", ""], "rule_id": [""] * 3})
    orig = advisor.acct.annotate
    advisor.acct.annotate = lambda bal, rules: ann
    try:
        plan = plan_df([
            row("HK GAMMA", 15_000_000, "IDR", memo="董事顾问费"),                 # 项目列空、memo 无关键词 → 不判
            row("HK GAMMA", 110_910_000, "IDR", memo="projectone 顾问费"),                # memo 命中长名
            row("HK GAMMA", 3_000_000_000, "IDR", memo="平台费"),                         # 项目列命中，但 GIRO 20 亿不够
        ])
        plan.loc[2, "project"] = "印尼PJ1"
        out, lines = advisor.project_self_funded(plan, bal_df([]), rules, EMAP, {"PJ1": "ProjectOne"})
    finally:
        advisor.acct.annotate = orig
    assert len(out) == 2 and 110_910_000 not in out["amount"].values
    assert any("PJ1 项目户自付" in ln and "2,000,000,000" in ln for ln in lines), "只算 GIRO 20 亿，不算 RDL 50 亿"
    assert any(ln.startswith("⚠️") and "3,000,000,000" in ln for ln in lines)


def test_flag_stale_transfers():
    """在途单付款账户现余额 < 单上金额 → 疑似过时，不计在途。"""
    ts = [{"lark_no": "202609990005", "amount": 162801.0, "currency": "USDT", "from_account": "P1_Ledger",
           "to_entity": "HK GAMMA", "status": "审批中"},
          {"lark_no": "202609990008", "amount": 1000.0, "currency": "USD", "from_account": "GAMMA_X",
           "to_entity": "HK GAMMA", "status": "审批中"}]
    bal = bal_df([("P", "P1_Ledger", "USDT", 47169.0), ("G", "GAMMA_X", "USD", 5000.0)])
    out = advisor.flag_stale_transfers(ts, bal)
    assert len(out) == 1 and "202609990005" in out[0] and ts[0].get("suspect") and not ts[1].get("suspect")
    needs = pd.DataFrame({"entity": ["HK GAMMA", "HK GAMMA"], "currency": ["USDT", "USD"],
                          "need": [1e5, 1e4], "count": [1, 1]})
    gaps = advisor.compute_gaps(needs, pd.DataFrame(columns=["entity", "currency", "avail"]), ts)
    g = gaps.set_index("currency")["transit"]
    assert g["USDT"] == 0 and g["USD"] == 1000


def test_append_carryover_and_netting_hint():
    """滚存台账并入计划；本周流水付掉的滚存项被核销。"""
    plan = plan_df([row("HK GAMMA", 1000, "USD")])
    carry = [{"entity": "HK ALPHA", "currency": "USD", "amount": 406744, "memo": "NORTH Advendor2 4+7 月",
              "since": "2026-08-24"}]
    out, notes = advisor.append_carryover(plan, carry)
    assert len(out) == 2 and out.iloc[1]["kind"] == "滚存" and "[滚存自 2026-08-24]" in out.iloc[1]["memo"]
    assert notes and "406,744" in notes[0]
    flows = pd.DataFrame({"date": pd.to_datetime(["2026-09-08"]), "currency": ["USD"], "amount": [406744.0],
                          "payee": ["ADVENDOR2"], "memo": [""], "account": ["ALPHA_BANKA_USD"],
                          "approval_no": [""], "classification": ["账单出款"]})
    emap = {"entities": EMAP["entities"], "channel_overrides": {}, "account_prefixes": {"ALPHA_": "HK ALPHA"}}
    out2, n2, _ = advisor.auto_net_from_liushui(out, flows, week_start=pd.Timestamp("2026-09-07"), emap=emap)
    assert len(out2) == 1 and any("滚存项" in n for n in n2)


def test_stale_rule_data():
    rules = [{"id": "R-009", "type": "usdt_wealth_unlocked", "status": "approved", "as_of": "2026-08-12",
              "params": {"amount": 1}},
             {"id": "R-008", "type": "weekly_inflow", "status": "approved", "params": {"as_of": "2026-09-01"}},
             {"id": "R-001", "type": "x", "status": "approved", "params": {}}]
    out = advisor.stale_rule_data(rules, max_age_days=21, today=pd.Timestamp("2026-09-07"))
    assert len(out) == 1 and out[0].startswith("R-009") and "26 天前" in out[0]


def test_lark_approval_matching_is_token_exact():
    plan = plan_df([row("HK GAMMA", 1000, "USD", lark="123")])
    flows = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-08"]),
        "currency": ["USD"],
        "amount": [1000.0],
        "payee": ["vendor"],
        "memo": [""],
        "account": ["GAMMA_BANKA_USD"],
        "approval_no": ["91234/555"],
        "classification": ["账单出款"],
    })
    out, notes, _ = advisor.auto_net_from_liushui(plan, flows, emap={
        "entities": EMAP["entities"], "channel_overrides": {}, "account_prefixes": {"GAMMA_": "HK GAMMA"}})
    assert len(out) == 0 and notes, "金额 fallback 可核销，但 lark substring 不能抢先命中"

    transfers = [{"lark_no": "123", "amount": 1000, "currency": "USD", "from_account": "OTHER_BANKA_USD",
                  "to_entity": "HK GAMMA", "date": pd.Timestamp("2026-09-07"), "arrived": False}]
    advisor.mark_executed(transfers, flows)
    assert not transfers[0].get("executed"), "91234 不能按 substring 命中 123"


def test_auto_net_amount_window_has_end_and_blocks_unmapped_payers():
    emap = {"entities": EMAP["entities"], "channel_overrides": {}, "account_prefixes": {"GAMMA_": "HK GAMMA"}}
    plan = plan_df([row("HK GAMMA", 1000, "USD"), row("HK GAMMA", 2000, "USD")])
    flows = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-08", "2026-09-14"]),
        "currency": ["USD", "USD"],
        "amount": [1000.0, 2000.0],
        "payee": ["vendor", "vendor"],
        "memo": ["", ""],
        "account": ["UNKNOWN_USD", "GAMMA_BANKA_USD"],
        "approval_no": ["", ""],
        "classification": ["账单出款", "账单出款"],
    })
    out, notes, ambig = advisor.auto_net_from_liushui(
        plan, flows, week_start=pd.Timestamp("2026-09-07"), week_end=pd.Timestamp("2026-09-11"), emap=emap)
    assert len(notes) == 0
    assert sorted(out["amount"].tolist()) == [1000, 2000]
    assert any("未映射主体" in s for s in ambig)


def test_lark_shared_row_payer_mismatch_does_not_fallback_to_other_entity_flow():
    emap = {"entities": EMAP["entities"], "channel_overrides": {},
            "account_prefixes": {"ALPHA_": "HK ALPHA", "BETA_": "MX BETA"}}
    plan = plan_df([row("HK GAMMA", 1000, "USD", lark="202609070001")])
    flows = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-08", "2026-09-08"]),
        "currency": ["USD", "USD"],
        "amount": [1000.0, 1000.0],
        "payee": ["vendor", "vendor"],
        "memo": ["", ""],
        "account": ["ALPHA_BANKA_USD", "BETA_BANKA_USD"],
        "approval_no": ["202609070001", "202609070001"],
        "classification": ["账单出款", "账单出款"],
    })
    out, notes, ambig = advisor.auto_net_from_liushui(plan, flows, emap=emap)
    assert len(out) == 1 and not notes
    assert any("付款账户无法映射到该主体" in s for s in ambig)


def test_mark_executed_amount_fallback_is_one_to_one_and_date_bounded():
    flows = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-04", "2026-09-06"]),
        "currency": ["USD", "USD"],
        "amount": [1000.0, 1000.0],
        "payee": ["vendor", "vendor"],
        "memo": ["", ""],
        "account": ["GAMMA_BANKA_USD", "GAMMA_BANKA_USD"],
        "approval_no": ["", ""],
    })
    transfers = [
        {"lark_no": "A", "amount": 1000, "currency": "USD", "from_account": "GAMMA_BANKA_USD",
         "to_entity": "HK GAMMA", "date": pd.Timestamp("2026-09-05"), "arrived": False},
        {"lark_no": "B", "amount": 1000, "currency": "USD", "from_account": "GAMMA_BANKA_USD",
         "to_entity": "HK GAMMA", "date": pd.Timestamp("2026-09-05"), "arrived": False},
    ]
    advisor.mark_executed(transfers, flows)
    assert [bool(t.get("executed")) for t in transfers] == [True, False]
    assert transfers[0]["executed_by"] == "付款账户+金额"


def test_project_self_funded_partial_amount_consumes_project_pool():
    ann = pd.DataFrame({
        "company": ["GAMMA LTD"],
        "account": ["GAMMA_BANKB_IDR_GIRO_8806_PJ1"],
        "currency": ["IDR"],
        "balance": [1500.0],
        "canonical": [""],
        "scope": ["project"],
        "scope_detail": ["PJ1"],
        "rule_id": [""],
    })
    orig = advisor.acct.annotate
    advisor.acct.annotate = lambda bal, rules: ann
    try:
        plan = plan_df([row("HK GAMMA", 1000, "IDR", memo="ProjectOne fee"),
                        row("HK GAMMA", 1000, "IDR", memo="ProjectOne fee 2")])
        out, lines = advisor.project_self_funded(plan, bal_df([]), [], EMAP, {"PJ1": "ProjectOne"})
    finally:
        advisor.acct.annotate = orig
    assert out["amount"].tolist() == [500]
    assert any("差额 500" in ln for ln in lines)


def test_flag_stale_transfers_keys_balance_by_account_and_currency():
    ts = [{"lark_no": "T1", "amount": 1000.0, "currency": "USD", "from_account": "P1_Ledger",
           "to_entity": "HK GAMMA", "status": "审批中"}]
    bal = bal_df([("P", "P1_Ledger", "USDT", 5000.0), ("P", "P1_Ledger", "USD", 100.0)])
    out = advisor.flag_stale_transfers(ts, bal)
    assert out and ts[0].get("suspect")


def test_route_payment_path_uses_fallback_local_ccy():
    rules = [{"id": "R-020", "type": "payment_path", "status": "approved",
              "params": {"entity": "MX BETA", "fallback_local_ccy": "控台代付"}}]
    gaps = pd.DataFrame({"entity": ["MX BETA"], "currency": ["MXN"], "need": [100.0],
                         "avail": [0.0], "transit": [0.0], "gap": [100.0]})
    actions, _ = advisor.route(gaps, bal_df([]), rules, 17.0)
    assert actions == ["[MXN] MX BETA 缺 100 → 控台代付（R-020）"]


def test_load_transfers_seen_after_valid_amount(tmp_dir):
    xlsx = tmp_dir / "transfers.xlsx"
    df = pd.DataFrame({
        "申请编号": ["202609070001", "202609070001"],
        "申请状态": ["审批中", "审批中"],
        "发起时间": ["2026-09-07", "2026-09-07"],
        "调拨明细-调拨原因": ["bad", "GAMMA_BANKA_USD to GAMMA_BANKB_USD"],
        "调拨明细-金额": ["", 1000],
        "收款方信息-主体": ["GAMMA LTD", "GAMMA LTD"],
        "收款方信息-账号": ["GAMMA_BANKB_USD", "GAMMA_BANKB_USD"],
        "调拨明细-调拨性质": ["调拨", "调拨"],
    })
    df.to_excel(xlsx, index=False)
    transfers = ai.load_transfers_any(xlsx, EMAP)
    assert len(transfers) == 1 and transfers[0]["amount"] == 1000
