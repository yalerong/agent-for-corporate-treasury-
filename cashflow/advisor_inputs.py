"""调拨建议引擎 v0 · 输入层：全部靠人工导出的文件投喂（finweb 不直连）。

四类输入：
  资金计划表.xlsx   「资金预算-周预估」sheet，按周分块，列0 有 YYYY.M.D-YYYY.M.D 周标记
  余额总览_*.xlsx   finweb 导出，取「公司/账户/币种/账户余额」四列
  paid.yaml         已付核销清单（对日记账人工确认后填写；lark 编号或 主体+币种+金额）
  transfers.yaml    在途调拨（arrived: false 才计入待到账）

解析纪律（2026-08-02 实战教训）：
  周块行只按「金额非空」保留——付款主体可能空白（空白≠无效单），
  空白主体行保留并标记 entity=None，由引擎单列人工确认，绝不静默丢弃。
"""
import re
from pathlib import Path

import pandas as pd
import yaml
from constants import get_root

WEEK_RE = re.compile(r"^20\d{2}\.\d{1,2}\.\d{1,2}-")

# Lark 调拨申请导出里的中文币种 → ISO（API 版没有币种列，见 load_transfers_any 的回退推断）
CCY_WORDS = {"美元": "USD", "印尼盾": "IDR", "墨西哥比索": "MXN", "人民币元": "CNY", "人民币": "CNY",
             "欧元": "EUR", "港币": "HKD", "港元": "HKD", "新加坡元": "SGD", "新币": "SGD",
             "印度卢比": "INR", "菲律宾比索": "PHP", "智利比索": "CLP", "秘鲁索尔": "PEN"}
# 不用 \b：Python 的 \w 含中文与下划线，"调拨MXN" / "GAMMA_BANKA_USD_5501" 都会让 \b 失效
_CCY_TOKEN = re.compile(r"(?<![A-Za-z])(USDT|USDC|USD|IDR|MXN|CNY|CNH|EUR|HKD|SGD|INR|PHP|CLP|PEN)(?![A-Za-z])", re.I)
# 调拨原因里的账户名：EPS_BANKB_IDR_GIRO_8806_PJ1 / PayB-NORTH-fintek1 / pay_NORTH_CW / HUB_Ledger
# 先抓 ASCII 开头的（"从P1_Ledger_…" 里的"从"不能粘进来），抓不到再允许中文开头（ONSHORE_BANKA_CNY_0521）
_ACCT_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*[_-][A-Za-z0-9_\-（）()]{3,}")
_ACCT_TOKEN_CN = re.compile(r"[一-鿿]+[_-][A-Za-z0-9一-鿿_\-（）()]{3,}")


def cfg_path(name: str, explicit: str | None = None) -> Path:
    """配置文件解析：显式路径 > CASHFLOW_ROOT/rules/<name> > 代码目录相对路径（旧行为）。

    规则库/映射表是真值，2026-09-06 起单源在私有知识仓 `rules/`；代码目录那份只是历史副本。
    """
    if explicit:
        return Path(explicit)
    cand = get_root() / "rules" / name
    return cand if cand.exists() else Path(name)

# 周预估块的列位（0 起算；列 0 是周标记列，明细从列 1 开始）
PLAN_COLS = {1: "entity", 2: "amount", 3: "currency", 4: "kind", 5: "pub_priv",
             6: "channel", 7: "memo", 8: "deadline", 9: "dept", 10: "project",
             11: "submitter", 12: "lark_submitted", 13: "lark_no"}


def load_plan_week(path: str | Path, week: str | None = None,
                   sheet: str = "资金预算-周预估",
                   keep_blank_amount: bool = False) -> tuple[str, pd.DataFrame]:
    """取指定周（缺省=最新一周）的明细块。返回 (周标记, DataFrame)。

    保留所有金额非空的行；entity 可能为 NaN（主体空白，需人工确认）。
    keep_blank_amount=True 时连"金额空白但有主体或款项说明"的行也保留（amount=NaN），
    由引擎单列——2026-09-06 挑错：拉美出资 85 万 MXN 等 8 行因金额空白静默消失，比主体空白更贵。
    """
    df = pd.read_excel(path, sheet_name=sheet, header=None)
    col0 = df[0].astype(str)
    marks = df[col0.str.match(WEEK_RE)].index.tolist()
    if not marks:
        raise SystemExit(f"{path} 的「{sheet}」里找不到周标记（YYYY.M.D-YYYY.M.D）")
    if week is None:
        i0 = marks[0]
    else:
        hit = [i for i in marks if str(df.iloc[i, 0]).strip() == week]
        if not hit:
            raise SystemExit(f"找不到周块 {week}；表内有：{[str(df.iloc[i, 0]) for i in marks[:5]]}")
        i0 = hit[0]
    nxt = [i for i in marks if i > i0]
    i1 = nxt[0] if nxt else len(df)
    blk = df.iloc[i0 + 1:i1, list(PLAN_COLS)].copy()
    blk.columns = list(PLAN_COLS.values())
    blk["amount"] = pd.to_numeric(blk["amount"], errors="coerce")
    if keep_blank_amount:
        has_text = blk["entity"].notna() | blk["memo"].fillna("").astype(str).str.strip().ne("")
        blk = blk[blk["amount"].notna() | has_text].reset_index(drop=True)
    else:
        blk = blk[blk["amount"].notna()].reset_index(drop=True)
    blk["currency"] = blk["currency"].astype(str).str.strip()
    # lark 编号统一成纯数字字符串，便于与 paid.yaml 匹配（Excel 里常是数值型）
    blk["lark_no"] = blk["lark_no"].map(
        lambda v: re.sub(r"\.0$", "", str(v).strip()) if pd.notna(v) else "")
    return str(df.iloc[i0, 0]).strip(), blk


def load_balances(path: str | Path) -> pd.DataFrame:
    """finweb 余额总览 → DataFrame[company, account, currency, balance]。"""
    df = pd.read_excel(path)
    df.columns = [str(c).strip() for c in df.columns]
    need = {"公司", "账户", "币种", "账户余额"}
    missing = need - set(df.columns)
    if missing:
        raise SystemExit(f"{path} 缺列：{missing}（要求 finweb 余额总览导出原样）")
    out = df[["公司", "账户", "币种", "账户余额"]].copy()
    out.columns = ["company", "account", "currency", "balance"]
    out["balance"] = pd.to_numeric(out["balance"], errors="coerce").fillna(0.0)
    out["currency"] = out["currency"].astype(str).str.strip()
    return out


def load_liushui(path: str | Path, since: str | None = None) -> pd.DataFrame:
    """finweb「流水查询_原始流水」导出 → 出账流水（列口径与 ingest_liushui 一致）。

    注意：流水的「项目归属」是业务线，≠计划表付款主体——自动核销只按
    币种+金额+时间窗匹配，主体不参与（这是已知映射坑，见 TASK_BRIEF）。
    """
    raw = pd.read_excel(path)
    need = ["日期", "币种", "支出原币"]
    missing = [c for c in need if c not in raw.columns]
    if missing:
        raise SystemExit(f"{path} 缺列 {missing}，这不是流水查询原始流水导出？")
    df = raw[pd.to_numeric(raw["支出原币"], errors="coerce") > 0].copy()

    def col(name: str) -> pd.Series:
        return df.get(name, pd.Series(dtype=object)).reindex(df.index).fillna("").astype(str)

    out = pd.DataFrame({
        "date": pd.to_datetime(df["日期"]),
        "currency": df["币种"].astype(str).str.strip(),
        "amount": pd.to_numeric(df["支出原币"], errors="coerce"),
        "payee": col("交易对手"),
        "memo": col("摘要"),
        "account": col("我方账户").str.strip(),          # 付款账户 → 主体守门用
        "approval_no": col("流水审批号").str.replace(r"\.0$", "", regex=True),  # lark 编号，可含多个以 / 分隔
        "classification": col("实质分类"),
    }).dropna(subset=["amount"])
    if since:
        out = out[out["date"] >= pd.Timestamp(since)]
    return out.reset_index(drop=True)


def load_yaml(path: str | Path, key: str) -> list[dict]:
    """读 paid.yaml / transfers.yaml 之类的 {key: [...]} 文件；文件不存在返回 []。"""
    p = Path(path)
    if not p.exists():
        return []
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return doc.get(key) or []


def _guess_currency(account: str, reason: str) -> str | None:
    """API 版导出没有币种列：出账币种先看调拨原因里第一个币种词（换汇单写的是出账币种），
    再退到收款账号名里的币种段。"""
    m = _CCY_TOKEN.search(str(reason))
    if m:
        return m.group(1).upper()
    for w, iso in CCY_WORDS.items():
        if w in str(reason):
            return iso
    m = _CCY_TOKEN.search(str(account))
    return m.group(1).upper() if m else None


def _account_currency(account: str) -> str | None:
    """收款账户自身币种（账户名里的币种段）：换汇类在途单出账币种≠到账币种，靠这个分辨。"""
    m = _CCY_TOKEN.search(str(account))
    return m.group(1).upper() if m else None


def _norm_name(s: str) -> str:
    return re.sub(r"[\s,.，。、（）()\-_/&]", "", str(s)).casefold()


def _entity_of_company(company: str, emap: dict | None) -> str | None:
    """Lark 收款方主体 → 计划表简称：精确 > 归一后相等 > 归一后包含（ONSHORE ⊂ ONSHORE（上海）数字科技有限公司）。"""
    if not emap or not company:
        return None
    ents = emap.get("entities", {})
    for short, full in ents.items():
        if full == company:
            return short
    nc = _norm_name(company)
    for short, full in ents.items():
        nf = _norm_name(full)
        if nf == nc:
            return short
    for short, full in ents.items():
        nf = _norm_name(full)
        if len(nf) >= 3 and (nf in nc or nc in nf):
            return short
    return None


def _from_account(reason: str, to_account: str) -> str | None:
    """从「调拨原因」文本里抓付款账户：第一个不等于收款账户的账户名 token。
    Lark 导出的「资金支付信息」列 100% 为空，付款方只在这段文本里（2026-09-02 实测）。"""
    to_key = str(to_account).casefold()
    for pat in (_ACCT_TOKEN, _ACCT_TOKEN_CN):
        for m in pat.finditer(str(reason)):
            tok = m.group(0).strip("（）()")
            if tok.casefold() != to_key and any(ch in tok for ch in "_-"):
                return tok
    return None


def load_transfers_any(path: str | Path, emap: dict | None = None) -> list[dict]:
    """在途调拨：yaml（旧格式）或 Lark「调拨申请」导出 xlsx（手动后台导出 / fetch_lark.py API 版）。

    统一成 [{lark_no, status, date, amount, currency, to_account, to_company, to_entity,
    from_account, reason, arrived}]。同一申请多明细行按 lark_no 去重（取首行）。
    状态语义：审批中/已同意 都可能"未到账"，是否执行由引擎对流水判定（mark_executed）。
    """
    p = Path(path)
    if p.suffix.lower() in (".yaml", ".yml"):
        return load_yaml(p, "transfers")
    head = pd.read_excel(p, header=None, nrows=2)
    manual = str(head.iloc[0, 0]).startswith("筛选条件")
    df = pd.read_excel(p, header=1 if manual else 0)
    df.columns = [str(c).strip() for c in df.columns]
    if manual:
        c_reason, c_amt, c_ccy, c_kind = "调拨原因", "金额", "金额币种", "调拨性质"
        c_company, c_acct = "主体", "账号"
    else:  # fetch_lark.py 摊平版
        c_reason, c_amt, c_ccy, c_kind = "调拨明细-调拨原因", "调拨明细-金额", None, "调拨明细-调拨性质"
        c_company, c_acct = "收款方信息-主体", "收款方信息-账号"
    need = [c for c in ("申请编号", "申请状态", "发起时间", c_reason, c_amt, c_company, c_acct) if c not in df.columns]
    if need:
        raise SystemExit(f"{p} 不像 Lark 调拨申请导出，缺列 {need}")
    out, seen = [], set()
    for _, r in df.iterrows():
        lark = re.sub(r"\.0$", "", str(r["申请编号"]).strip())
        if not lark or lark == "nan" or lark in seen:
            continue
        seen.add(lark)
        amt = pd.to_numeric(r[c_amt], errors="coerce")
        if pd.isna(amt):
            continue
        acct = str(r[c_acct]).strip() if pd.notna(r[c_acct]) else ""
        reason = str(r[c_reason]) if pd.notna(r[c_reason]) else ""
        ccy = CCY_WORDS.get(str(r[c_ccy]).strip()) if c_ccy and pd.notna(r.get(c_ccy)) else None
        ccy = ccy or _guess_currency(acct, reason)
        to_ccy = _account_currency(acct)
        if ccy == "USD" and (to_ccy == "USDT" or "USDT" in reason.upper()):
            ccy = "USDT"  # Lark 里 Ledger/Wallety 划转币种填「美元」
        company = str(r[c_company]).strip() if pd.notna(r[c_company]) else ""
        kind = str(r[c_kind]).strip() if c_kind in df.columns and pd.notna(r.get(c_kind)) else ""
        out.append({"lark_no": lark, "status": str(r["申请状态"]).strip(), "kind": kind,
                    "date": pd.to_datetime(r["发起时间"], errors="coerce"),
                    "amount": float(amt), "currency": ccy or "UNK",
                    "to_currency": to_ccy or ccy,      # 到账币种；≠currency 即换汇单
                    "to_account": acct, "to_company": company,
                    "to_entity": _entity_of_company(company, emap),
                    "from_account": _from_account(reason, acct),
                    "reason": reason.replace("\n", " ").strip(), "arrived": False})
    return out


def load_rules(path: str | Path) -> list[dict]:
    """规则库：只返回 status==approved 的规则（与 patterns 三态纪律同构）。"""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    rules = doc.get("rules") or []
    return [r for r in rules if r.get("status") == "approved"]


def load_alias(path: str | Path) -> dict:
    """entity_alias.yaml：项目短码 → 长名（PJ1: ProjectOne）。文件不存在返回 {}。"""
    p = Path(path)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def load_entity_map(path: str | Path) -> dict:
    """主体映射：计划表简称 → finweb 公司全称；channel_overrides：交易账户 → 公司。"""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {"entities": doc.get("entities") or {},
            "channel_overrides": doc.get("channel_overrides") or {},
            "account_prefixes": doc.get("account_prefixes") or {}}


def entity_of_account(account: str, emap: dict) -> str | None:
    """流水「我方账户」→ 计划表主体简称（account_prefixes 前缀匹配，长前缀优先，大小写不敏感）。"""
    a = str(account).casefold()
    best = None
    for pre, ent in (emap.get("account_prefixes") or {}).items():
        if a.startswith(pre.casefold()) and (best is None or len(pre) > len(best[0])):
            best = (pre, ent)
    return best[1] if best else None
