"""账户分层与归一：同一主体的账户不是同一个钱包。

三件事（全部规则驱动，规则见 advisor_rules.yaml）：
  normalize  通道户名尾号是渠道分配的虚拟账户号(VA)、会轮换 → 按「主体_通道_币种」归一。
             禁止硬编码别名对：下次轮换就会再失配。
  classify   账户分层 scope：
               group      集团账户，可自由动用（唯一计入"可动用余额"的一类）
               project:X  项目账户，只能付同项目（LBANK 后缀 / 资金池前缀编码项目）
               earmarked  专款专用（如 XBANK 仅供指定地区代付，KYC 摩擦高）
               channel    过路通道户，余额常态≈0，不是弹药（弹药在其上游项目户）
               business   业务控制（fx_pool 声明 exclusive 时，同主体同币种的其他户）
  check      流水 vs 余额的账户一致性质检：VA 轮换静默归一，真新账户才报警。

分层优先级：earmarked > channel > project > business > group（越具体越优先）。
未命中任何规则的账户默认 group——多数主体（HK/SG）确实自由，但质检会列出未分类账户。
"""
import re

import pandas as pd

# 通道户归一：去掉尾部 VA 段（_1789 / _6464 ...）
_TAIL_VA = re.compile(r"_[0-9]+$")


def rules_of(rules: list[dict], typ: str) -> list[dict]:
    return [r for r in rules if r.get("type") == typ]


def _cfg(rules: list[dict]) -> dict:
    """取 account_classification 规则的参数（缺省给空配置，函数仍可跑）。"""
    rs = rules_of(rules, "account_classification")
    return rs[0]["params"] if rs else {}


def is_channel(account: str, rules: list[dict]) -> bool:
    a = str(account).lower()
    return any(p.lower() in a for p in _cfg(rules).get("channel_patterns", []))


def normalize(account: str, rules: list[dict]) -> str:
    """通道户 → 「主体_通道_币种」规范键（去 VA 尾号）；非通道户原样返回。保留原大小写。"""
    a = str(account)
    return _TAIL_VA.sub("", a) if is_channel(a, rules) else a


def canon_key(account: str, rules: list[dict]) -> str:
    """比较用规范键：去 VA + 大小写归一。

    上游流水模块与余额模块对同一账户大小写不一致（payx_NORTH_CW vs
    PAYX_NORTH_CW、DELTA_CUSTODY_USD vs DELTA_Custody_USD），
    与 VA 轮换同属"同一账户两个标签"，都该静默归一而非报警。
    """
    return normalize(account, rules).casefold()


def classify(account: str, rules: list[dict], company: str = "",
             currency: str = "", business_keys: set | None = None) -> dict:
    """返回 {scope, detail, rule_id}。business_keys 由 annotate 预计算（见 exclusive 口径）。"""
    a = str(account)
    for r in rules_of(rules, "earmarked_account"):
        if a == r["params"].get("account"):
            return {"scope": "earmarked", "detail": r["params"].get("purpose", ""),
                    "rule_id": r["id"]}
    if is_channel(a, rules):
        return {"scope": "channel", "detail": "过路户，余额≈0 不作弹药", "rule_id": "R-015"}
    cfg = _cfg(rules)
    for suffix, proj in (cfg.get("project_suffix_patterns") or {}).items():
        if a.upper().endswith(suffix.upper()):
            return {"scope": "project", "detail": proj, "rule_id": "R-015"}
    for prefix, proj in (cfg.get("project_prefix_patterns") or {}).items():
        if a.upper().startswith(prefix.upper()):
            return {"scope": "project", "detail": proj, "rule_id": "R-015"}
    for r in rules_of(rules, "account_scope"):
        for proj, accts in (r["params"].get("project_accounts") or {}).items():
            if a in accts:
                return {"scope": "project", "detail": proj, "rule_id": r["id"]}
    if business_keys and (company, currency, a) in business_keys:
        return {"scope": "business", "detail": "业务控制（限额/放款周转）", "rule_id": "R-002/003"}
    return {"scope": "group", "detail": "", "rule_id": ""}


def _business_keys(bal: pd.DataFrame, rules: list[dict]) -> set:
    """fx_pool 声明 exclusive 时：持有池内账户的公司，其同币种其他账户 = 业务控制。"""
    keys = set()
    for r in rules_of(rules, "fx_pool"):
        p = r["params"]
        if not p.get("exclusive", True):
            continue
        listed, ccy = set(p.get("accounts", [])), p.get("currency")
        owners = set(bal.loc[bal["account"].isin(listed), "company"])
        m = (bal["company"].isin(owners)) & (bal["currency"] == ccy) & (~bal["account"].isin(listed))
        for _, row in bal[m].iterrows():
            keys.add((row["company"], row["currency"], row["account"]))
    return keys


def annotate(bal: pd.DataFrame, rules: list[dict]) -> pd.DataFrame:
    """给余额表加 canonical/scope/scope_detail/rule_id 四列。"""
    out = bal.copy()
    bkeys = _business_keys(out, rules)
    out["canonical"] = [normalize(a, rules) for a in out["account"]]
    cls = [classify(r.account, rules, r.company, r.currency, bkeys)
           for r in out.itertuples(index=False)]
    out["scope"] = [c["scope"] for c in cls]
    out["scope_detail"] = [c["detail"] for c in cls]
    out["rule_id"] = [c["rule_id"] for c in cls]
    return out


def usable(bal_annotated: pd.DataFrame) -> pd.DataFrame:
    """可动用余额 = 仅 group 层（剔除项目户/专用户/通道户/业务控制户）。"""
    return bal_annotated[bal_annotated["scope"] == "group"]


def check(bal: pd.DataFrame, flows: pd.DataFrame, rules: list[dict]) -> list[dict]:
    """账户质检。返回 [{level, msg}]：warn 需人工确认，info 已自动处理。"""
    ann = annotate(bal, rules)
    known = set(ann["account"])
    key_of = {a: canon_key(a, rules) for a in known}
    by_key: dict[str, list[str]] = {}
    for a, k in key_of.items():
        by_key.setdefault(k, []).append(a)
    findings: list[dict] = []
    if flows is None or flows.empty:
        return findings
    seen: set[str] = set()
    for _, f in flows.iterrows():
        acct = str(f.get("payee", "") or "")
        if not acct or acct in known or acct in seen:
            continue
        seen.add(acct)
        k = canon_key(acct, rules)
        if k in by_key:
            same = sorted(by_key[k])
            # 尾号被 normalize 削掉 = VA 轮换；否则纯大小写差异
            kind = ("VA 轮换" if normalize(acct, rules) != acct
                    else "大小写差异")
            findings.append({"level": "info",
                             "msg": f"{kind}已归一：流水用 {acct}，余额表为 {'/'.join(same)}"})
        elif "_" in acct:
            findings.append({"level": "warn",
                             "msg": f"流水里的账户 {acct} 在余额表中查无（规范键 {k} 也无）"
                                    f"——疑似真新账户或台账遗漏，需确认"})
    # 同一行内 payee 与摘要提到的账户是否指向同一规范键
    for _, f in flows.iterrows():
        acct, memo = str(f.get("payee", "") or ""), str(f.get("memo", "") or "")
        if not acct or not memo:
            continue
        hits = {a for a in known if a.casefold() in memo.casefold()}
        if not hits:
            continue
        if canon_key(acct, rules) not in {key_of[h] for h in hits}:
            findings.append({"level": "warn",
                             "msg": f"行内矛盾：收款方 {acct} 与摘要提及 {'/'.join(sorted(hits))} 不同源"})
    # 去重并保持顺序
    uniq, out = set(), []
    for f in findings:
        if f["msg"] not in uniq:
            uniq.add(f["msg"])
            out.append(f)
    return out


def pool_summary(bal_annotated: pd.DataFrame, emap: dict) -> pd.DataFrame:
    """按主体×币种×分层汇总，供建议单展示"可动用 vs 受限"。"""
    name_of = {c: s for s, c in (emap.get("entities") or {}).items()}
    name_of.update({c: ch for ch, c in (emap.get("channel_overrides") or {}).items()})
    df = bal_annotated.copy()
    df["entity"] = df["company"].map(name_of)
    df = df[df["entity"].notna() & (df["balance"].abs() > 0.5)]
    piv = (df.pivot_table(index=["entity", "currency"], columns="scope", values="balance",
                          aggfunc="sum", fill_value=0.0).reset_index())
    return piv
