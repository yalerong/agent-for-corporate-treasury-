"""PR5 LLM 摘要层（全离线，fake 数据，不触网）：
profile 只含聚合、三重闸（schema/数值 verifier/去重）、LLM 永不置 approved、
无 key 跳过、自主权滑块分档。"""
import os
import subprocess
import sys

import llm_patterns
import pandas as pd
import pattern_store as ps
import policy
import yaml
from pipeline_utils import CASHFLOW_DIR
from profiles import build_profiles, resolve_field

COLS = ["date", "entity", "project", "currency", "payee", "amount"]


def pay_df(rows):
    df = pd.DataFrame(rows, columns=COLS)
    df["date"] = pd.to_datetime(df["date"])
    return df


def sample_pay():
    rows = []
    for m in range(1, 7):
        rows.append((f"2026-{m:02d}-15", "A Co", "运营", "USD", "工资商", 100000.0))
        rows.append((f"2026-{m:02d}-03", "A Co", "运营", "USD", "小额商", 1000.0))
    return pay_df(rows)


# ---------- profile：只含聚合 ----------

def test_profile_structure_and_no_raw_rows():
    profs = build_profiles(sample_pay())
    assert len(profs) == 1
    p = profs[0]
    assert p["entity"] == "A Co" and p["months"] == 6 and p["n_payments"] == 12
    assert set(p["weekly"]) == {"weeks", "mean", "std", "cv", "p50", "p90",
                                "last4_mean", "prev4_mean"}
    assert p["dom_share"]["15"] > 0.9  # 金额占比直方图，非单笔
    assert p["top_payees"][0]["payee"] == "工资商"
    assert p["top_payees"][0]["total"] == 600000.0  # 聚合额，不是单笔明细
    # 结构里不存在任何"日期×金额"的单笔记录字段
    assert "payments" not in p and "rows" not in p


def test_profile_anonymize():
    p = build_profiles(sample_pay(), anonymize=True)[0]
    names = [t["payee"] for t in p["top_payees"]]
    assert names == ["P1", "P2"]
    assert "工资商" not in str(p)


def test_resolve_field():
    p = build_profiles(sample_pay())[0]
    assert resolve_field(p, "weekly.mean") == p["weekly"]["mean"]
    assert resolve_field(p, "top_payees.0.share") == p["top_payees"][0]["share"]
    assert resolve_field(p, "dom_share.15") == p["dom_share"]["15"]
    assert resolve_field(p, "no.such.path") is None
    assert resolve_field(p, "top_payees.9.share") is None


# ---------- 三重闸 ----------

def empty_doc():
    return {"meta": {"schema_version": 2, "generated_at": "t",
                     "data_range": ["2026-01-01", "2026-06-30"], "rows": 12},
            "patterns": []}


def cand(slug="salary_d15", claim="工资商月中集中付款", field="dom_share.15", value=None,
         profile=None, **extra):
    v = value if value is not None else profile["dom_share"]["15"]
    return {"slug": slug, "claim": claim, "checks": [{"field": field, "value": v}], **extra}


def test_harvest_schema_gate():
    prof = build_profiles(sample_pay())[0]
    bad = [{"slug": "Bad Slug!", "claim": "x", "checks": [{"field": "total", "value": 1}]},
           {"slug": "no_checks", "claim": "x", "checks": []},
           {"slug": "ok_pattern", "claim": "x"}]  # checks 缺失
    out = llm_patterns.harvest([(prof, bad)], empty_doc())
    assert out["added"] == []
    assert len(out["rejected"]) == 3
    assert all(r[0].startswith("schema") for r in out["rejected"])


def test_harvest_verifier_gate():
    prof = build_profiles(sample_pay())[0]
    good = cand(profile=prof)                                    # 精确值 → 过
    off6 = cand(slug="off_six", value=prof["dom_share"]["15"] * 1.06)  # 偏 6% → 拒
    ghost = cand(slug="ghost_field", field="weekly.magic", value=1.0)  # 路径不存在 → 拒
    doc = empty_doc()
    out = llm_patterns.harvest([(prof, [good, off6, ghost])], doc)
    assert [p["key"]["slug"] for p in out["added"]] == ["salary_d15"]
    assert {r[1] for r in out["rejected"]} == {"off_six", "ghost_field"}
    assert all("verifier" in r[0] for r in out["rejected"])


def test_harvest_dedup_and_never_approved():
    prof = build_profiles(sample_pay())[0]
    doc = empty_doc()
    sneaky = cand(profile=prof, status="approved", approved_by="llm")  # 越权字段应被忽略
    out = llm_patterns.harvest([(prof, [sneaky, cand(profile=prof)])], doc)  # 同 slug 批内重复
    assert len(out["added"]) == 1
    p = out["added"][0]
    assert p["status"] == "candidate"       # LLM 永不置 approved
    assert p["approved_by"] is None
    assert p["source"] == "llm"
    assert p["confidence"] == "provisional"
    # 二次投喂同一候选 → 库内去重
    out2 = llm_patterns.harvest([(prof, [cand(profile=prof)])], doc)
    assert out2["added"] == [] and out2["rejected"][0][0].startswith("dedup")


def test_llm_patterns_survive_stats_rerun():
    """merge_states 对 source=llm 条目的留存（PR2 埋的钩子在这里闭环）。"""
    prof = build_profiles(sample_pay())[0]
    doc = empty_doc()
    llm_patterns.harvest([(prof, [cand(profile=prof)])], doc)
    stats_pats = [{"type": "recurring", "key": {"payee": "工资商", "currency": "USD"},
                   "claim": "c", "day_of_month": 15, "avg_amount": 100000.0,
                   "sample_n": 6, "confidence": "high"}]
    merged = ps.merge_states(stats_pats, doc, default_valid_from="2026-06-30")
    kinds = {p.get("source", "stats") for p in merged}
    assert kinds == {"stats", "llm"}
    assert sum(1 for p in merged if p.get("source") == "llm") == 1


# ---------- 无 key 跳过 ----------

def test_no_key_skips_and_leaves_patterns_untouched(pipeline_root):
    pat = pipeline_root / "patterns" / "patterns.yaml"
    before = pat.read_bytes()
    env = {**os.environ, "CASHFLOW_ROOT": str(pipeline_root), "PYTHONIOENCODING": "utf-8"}
    env.pop("LLM_API_KEY", None)
    env.pop("LLM_PROVIDER", None)
    r = subprocess.run([sys.executable, "llm_patterns.py"], cwd=CASHFLOW_DIR, env=env,
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0
    assert "跳过" in r.stdout
    assert pat.read_bytes() == before


# ---------- 自主权滑块 ----------

def test_review_tier_units():
    pol = yaml.safe_load((CASHFLOW_DIR / "policy.example.yaml").read_text(encoding="utf-8"))
    assert policy.review_tier(1000, "USD", "HK Co运营", pol) == "auto_report"
    assert policy.review_tier(60000, "USD", "HK Co运营", pol) == "flag_review"
    assert policy.review_tier(600000, "USD", "HK Co运营", pol) == "require_human"
    assert policy.review_tier(1e9, "IDR", "x", pol) == "flag_review"  # 币种覆盖
    assert policy.review_tier(100, "CNY", "母公司(关联方)", pol) == "require_human"  # 关键词


def test_forecast_review_column(pipeline_root):
    fc = pd.read_csv(pipeline_root / "runs" / "2026-07-30" / "forecast.csv",
                     encoding="utf-8-sig")
    assert "review" in fc.columns
    assert set(fc["review"]) <= set(policy.TIERS)
    sz = fc[fc["payee"] == "深圳子公司(关联方)"]
    assert len(sz) and (sz["review"] == "require_human").all()   # 关键词直升
    adp = fc[fc["payee"] == "ADP Payroll"]
    assert len(adp) and (adp["review"] == "flag_review").all()   # 11.8万 USD 落中档
