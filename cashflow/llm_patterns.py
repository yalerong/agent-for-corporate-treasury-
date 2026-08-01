"""LLM 归纳环：聚合 profile → 候选规律（status=candidate, source=llm）入库。

纪律（离线退化是卖点，README 明写）:
- LLM 只见 profile.py 的聚合摘要，**绝不见单笔流水**；payee 可代号化
- 输出必须严格 JSON，且每条候选必须带 checks——claim 里引用的数字要能在 profile 复算
- 代码侧三重闸: ① schema 校验 ② 数值 verifier（与 profile 复算值偏差>VERIFY_TOL 丢弃）
  ③ 按 pattern_id 去重（库内已有/本批重复都跳过）
- 通过者以 type=llm_insight, status=candidate, source=llm 入库，走同一套 approve.py 审批；
  **LLM 永不置 approved**；llm_insight 不被 forecast/头寸消费，批准与否都不影响引擎数字
- 无 LLM_API_KEY 时打印跳过，确定性流水线照常

用法: python llm_patterns.py [--max-groups 20] [--anonymize]
"""
import argparse
import json
import re
import sqlite3

import llm_client
import pandas as pd
import pattern_store as ps
from constants import get_root
from profiles import build_profiles, resolve_field

ROOT = get_root()
DB = ROOT / "data" / "db" / "treasury.db"
PAT = ROOT / "patterns" / "patterns.yaml"

VERIFY_TOL = 0.05  # claim 数字与 profile 复算值的相对偏差上限
SLUG_RE = re.compile(r"^[a-z0-9_]{3,40}$")

# Anthropic 通道用作结构化输出硬约束；openai 兼容通道退化为 json_object+三重闸
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"patterns": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "claim": {"type": "string"},
            "checks": {"type": "array", "items": {
                "type": "object",
                "properties": {"field": {"type": "string"}, "value": {"type": "number"}},
                "required": ["field", "value"], "additionalProperties": False}},
        },
        "required": ["slug", "claim", "checks"], "additionalProperties": False}}},
    "required": ["patterns"], "additionalProperties": False,
}

SYSTEM = (
    "你是资金数据分析师。你只能基于用户给出的聚合 profile 归纳规律，"
    "禁止编造 profile 里没有的数字。输出严格 JSON："
    '{"patterns": [{"slug": "小写下划线短标识", "claim": "一句话规律（中文，含关键数字）", '
    '"checks": [{"field": "profile 里的点路径，如 weekly.mean 或 dom_share.15 或 top_payees.0.share", '
    '"value": 数值}]}]}。'
    "每条规律至少 1 个 check；claim 里出现的每个关键数字都必须有对应 check。"
    "没有值得说的规律就返回 {\"patterns\": []}。"
)


def candidate_error(c: dict) -> str | None:
    """schema 闸：返回错误说明，None=通过。"""
    if not isinstance(c, dict):
        return "候选不是对象"
    if not isinstance(c.get("slug"), str) or not SLUG_RE.match(c["slug"]):
        return f"slug 非法: {c.get('slug')!r}"
    if not isinstance(c.get("claim"), str) or not c["claim"].strip():
        return "claim 缺失"
    checks = c.get("checks")
    if not isinstance(checks, list) or not checks:
        return "checks 缺失（禁止无数字断言）"
    for ch in checks:
        if not isinstance(ch, dict) or not isinstance(ch.get("field"), str) \
                or not isinstance(ch.get("value"), int | float):
            return f"check 非法: {ch!r}"
    return None


def verify_checks(c: dict, profile: dict) -> bool:
    """数值闸：每个 check 都要能在 profile 复算且偏差 ≤ VERIFY_TOL。"""
    for ch in c["checks"]:
        actual = resolve_field(profile, ch["field"])
        if actual is None:
            return False
        v = float(ch["value"])
        if actual == 0:
            if abs(v) > 1e-9:
                return False
        elif abs(v - actual) / abs(actual) > VERIFY_TOL:
            return False
    return True


def to_pattern(c: dict, profile: dict) -> dict:
    """候选 → schema v2 规律。状态永远 candidate——LLM 无权置 approved。"""
    key = {"entity": profile["entity"], "project": profile["project"],
           "currency": profile["currency"], "slug": c["slug"]}
    return ps.upgrade_pattern({
        "type": "llm_insight", "key": key, "claim": c["claim"].strip(),
        "confidence": "provisional", "status": "candidate", "source": "llm",
        "evidence": {"checks": c["checks"]},
    })


def harvest(batches: list[tuple[dict, list[dict]]], doc: dict) -> dict:
    """三重闸过滤并把通过者并入 doc（原地）。batches=[(profile, candidates)]。

    返回 {"added": [...], "rejected": [(原因, slug)]}。纯函数便于离线测试。
    """
    existing = {p["id"] for p in doc["patterns"]}
    added, rejected = [], []
    for profile, cands in batches:
        for c in cands:
            err = candidate_error(c)
            if err:
                rejected.append((f"schema: {err}", str(c.get("slug"))))
                continue
            if not verify_checks(c, profile):
                rejected.append(("verifier: 数字与 profile 复算不符", c["slug"]))
                continue
            p = to_pattern(c, profile)
            if p["id"] in existing:
                rejected.append(("dedup: pattern_id 已存在", c["slug"]))
                continue
            existing.add(p["id"])
            doc["patterns"].append(p)
            added.append(p)
    return {"added": added, "rejected": rejected}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-groups", type=int, default=20, help="最多送多少个分组 profile")
    ap.add_argument("--anonymize", action="store_true", help="payee 代号化后再送 LLM")
    args = ap.parse_args()

    client = llm_client.get_client()
    if client is None:
        print("未配置 LLM（LLM_PROVIDER=anthropic 或 LLM_API_KEY），归纳环跳过；"
              "确定性流水线不受影响。")
        return
    if not PAT.exists():
        raise SystemExit(f"{PAT} 不存在，先跑 patterns.py")
    doc = ps.load(PAT)
    if doc["meta"].get("schema_version", 1) < ps.SCHEMA_VERSION:
        raise SystemExit("patterns.yaml 还是 schema v1，先跑 python migrate_patterns.py")
    con = sqlite3.connect(DB)
    pay = pd.read_sql("SELECT * FROM payments", con, parse_dates=["date"])
    con.close()

    profiles = build_profiles(pay, anonymize=args.anonymize)
    profiles.sort(key=lambda p: -p["total"])
    batches = []
    for prof in profiles[:args.max_groups]:
        try:
            resp = llm_client.chat_json(client, SYSTEM, json.dumps(prof, ensure_ascii=False),
                                        schema=RESPONSE_SCHEMA)
        except ValueError as e:
            print(f"  [跳过] {prof['entity']}/{prof['project']}/{prof['currency']}: {e}")
            continue
        cands = resp.get("patterns", []) if isinstance(resp, dict) else []
        batches.append((prof, cands))

    result = harvest(batches, doc)
    if result["added"]:
        ps.save(PAT, doc, backup=True)
    print(f"LLM 候选入库 {len(result['added'])} 条（candidate/source=llm），"
          f"拒收 {len(result['rejected'])} 条 → {PAT}")
    for p in result["added"]:
        print(f"  [candidate] {p['id']} {p['key']} :: {p['claim']}")
    for reason, slug in result["rejected"]:
        print(f"  [拒收] {slug}: {reason}")


if __name__ == "__main__":
    main()
