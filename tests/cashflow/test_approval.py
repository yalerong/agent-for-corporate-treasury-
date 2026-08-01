"""PR2 三态规律库：pattern_id 稳定性、v1→v2 迁移幂等、审批 CLI、engine strict 门控。

会改写规律库/报告的用例一律在 pipeline_root 的隔离副本(iso_root)上跑，
不污染共享 session fixture。
"""
import shutil
import tempfile
from pathlib import Path

import pattern_store as ps
import pytest
import yaml
from pipeline_utils import run_script

V2_ONLY = {"id", "status", "source", "valid_from", "approved_by", "approved_at",
           "refuted_reason", "superseded_by", "evidence"}


@pytest.fixture()
def iso_root(pipeline_root) -> Path:
    dst = Path(tempfile.mkdtemp(prefix="treasury_ap_"))
    shutil.copytree(pipeline_root, dst, dirs_exist_ok=True)
    yield dst
    shutil.rmtree(dst, ignore_errors=True)


def load_doc(root: Path) -> dict:
    return yaml.safe_load((root / "patterns" / "patterns.yaml").read_text(encoding="utf-8"))


def find(doc: dict, type_: str, **key) -> dict:
    hits = [p for p in doc["patterns"] if p["type"] == type_ and p["key"] == key]
    assert len(hits) == 1
    return hits[0]


def report(root: Path) -> str:
    return (root / "runs" / "2026-07-30" / "report.md").read_text(encoding="utf-8")


# ---------- pattern_id ----------

def test_pattern_id_order_insensitive():
    a = ps.pattern_id("recurring", {"payee": "X", "currency": "USD"})
    b = ps.pattern_id("recurring", {"currency": "USD", "payee": "X"})
    assert a == b
    assert len(a) == 12
    assert ps.pattern_id("recurring", {"payee": "Y", "currency": "USD"}) != a
    assert ps.pattern_id("weekly_level", {"payee": "X", "currency": "USD"}) != a


# ---------- v1 → v2 迁移 ----------

def make_v1_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="treasury_v1_"))
    (root / "patterns").mkdir(parents=True)
    doc = {"meta": {"generated_at": "2026-06-30T00:00:00+00:00",
                    "data_range": ["2026-01-01", "2026-06-30"], "rows": 10},
           "patterns": [
               {"type": "recurring", "key": {"payee": "X", "currency": "USD"},
                "claim": "月度固定", "day_of_month": 15, "avg_amount": 100.0,
                "sample_n": 5, "confidence": "high", "approved": True},
               {"type": "weekly_level",
                "key": {"entity": "A", "project": "P", "currency": "USD"},
                "claim": "周度", "base_weekly": 2.0, "cv": 0.1, "trend": 1.0,
                "sample_weeks": 9, "confidence": "high", "approved": False}]}
    (root / "patterns" / "patterns.yaml").write_text(
        yaml.dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return root


def test_migrate_v1_maps_status_and_is_idempotent():
    root = make_v1_root()
    try:
        run_script("migrate_patterns.py", root)
        pat_file = root / "patterns" / "patterns.yaml"
        assert (root / "patterns" / "patterns.yaml.v1.bak").exists()
        doc = yaml.safe_load(pat_file.read_text(encoding="utf-8"))
        assert doc["meta"]["schema_version"] == 2
        x = find(doc, "recurring", payee="X", currency="USD")
        assert x["status"] == "approved"
        assert x["approved_by"] == "v1-migration"
        assert "approved" not in x  # v1 布尔只映射 status，不再写盘
        a = find(doc, "weekly_level", entity="A", project="P", currency="USD")
        assert a["status"] == "candidate"
        assert all(p["valid_from"] == "2026-06-30" for p in doc["patterns"])
        # 幂等：二跑 no-op，文件逐字节不变
        before = pat_file.read_text(encoding="utf-8")
        r = run_script("migrate_patterns.py", root)
        assert "无需迁移" in r.stdout
        assert pat_file.read_text(encoding="utf-8") == before
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_v1_approved_survives_patterns_rerun_without_migrate(iso_root):
    """服务器忘了跑 migrate 直接 git pull 重算：legacy 键兜底，approved 不丢。"""
    pat_file = iso_root / "patterns" / "patterns.yaml"
    doc = load_doc(iso_root)
    v1 = []
    for p in doc["patterns"]:
        q = {k: v for k, v in p.items() if k not in V2_ONLY}
        q["approved"] = p["type"] == "recurring" and p["key"].get("payee") == "ADP Payroll"
        v1.append(q)
    pat_file.write_text(yaml.dump({"meta": {k: v for k, v in doc["meta"].items()
                                            if k != "schema_version"}, "patterns": v1},
                                  allow_unicode=True, sort_keys=False), encoding="utf-8")
    run_script("patterns.py", iso_root)
    doc2 = load_doc(iso_root)
    assert doc2["meta"]["schema_version"] == 2
    adp = find(doc2, "recurring", payee="ADP Payroll", currency="USD")
    assert adp["status"] == "approved"


# ---------- 审批 CLI ----------

def test_approve_all_high_then_refute_sticks(iso_root):
    run_script("approve.py", iso_root, "stats")
    run_script("approve.py", iso_root, "approve", "--all", "--confidence", "high", "--by", "yale")
    doc = load_doc(iso_root)
    highs = [p for p in doc["patterns"] if p["confidence"] == "high"]
    assert len(highs) == 4
    assert all(p["status"] == "approved" and p["approved_by"] == "yale" for p in highs)
    assert all(p["status"] == "candidate" for p in doc["patterns"]
               if p["confidence"] == "provisional")
    assert (iso_root / "patterns" / "patterns.yaml.bak").exists()

    adp_id = find(doc, "recurring", payee="ADP Payroll", currency="USD")["id"]
    run_script("approve.py", iso_root, "refute", "--ids", adp_id,
               "--reason", "测试否决", "--by", "yale")
    run_script("patterns.py", iso_root)  # 重算不得复活 refuted
    adp = find(load_doc(iso_root), "recurring", payee="ADP Payroll", currency="USD")
    assert adp["status"] == "refuted"
    assert "测试否决" in adp["refuted_reason"]


def test_refuted_excluded_from_forecast(iso_root):
    doc = load_doc(iso_root)
    sz_id = find(doc, "recurring", payee="深圳子公司(关联方)", currency="CNY")["id"]
    run_script("approve.py", iso_root, "refute", "--ids", sz_id,
               "--reason", "口径测试", "--by", "yale")
    run_script("engine.py", iso_root)
    fc = (iso_root / "runs" / "2026-07-30" / "forecast.csv").read_text(encoding="utf-8-sig")
    assert "深圳子公司" not in fc  # refuted 连预测行都不出


# ---------- engine strict 门控 ----------

def test_strict_gating_transition_and_effective(iso_root):
    # ① 0 approved：strict 自动回退过渡模式，数字仍按置信度口径出（防空报告）
    run_script("engine.py", iso_root, "--strict-approval")
    rep = report(iso_root)
    assert "过渡模式" in rep
    assert "**CNY**: 未来4周购汇需求 11,880" in rep

    # ② 批掉全部 high 后 strict 生效：approved 集合==high 集合，数字应与置信度口径一致
    run_script("approve.py", iso_root, "approve", "--all", "--confidence", "high", "--by", "yale")
    run_script("engine.py", iso_root, "--strict-approval")
    rep = report(iso_root)
    assert "strict-approval 开启" in rep
    assert "过渡模式" not in rep
    assert "**CNY**: 未来4周购汇需求 11,880" in rep

    # ③ 否决 CNY 大头后：CNY 无计算内流出 → 头寸转富余，购汇需求消失
    sz_id = find(load_doc(iso_root), "recurring", payee="深圳子公司(关联方)", currency="CNY")["id"]
    run_script("approve.py", iso_root, "refute", "--ids", sz_id,
               "--reason", "口径测试", "--by", "yale")
    run_script("engine.py", iso_root, "--strict-approval")
    rep = report(iso_root)
    assert "| CNY | 140,000 | 0 | 140,000 | 富余 |" in rep
    assert "未来4周购汇需求" not in rep


def test_no_strict_escape_hatch(iso_root):
    """--no-strict-approval 逃生口：不进 strict 也不打过渡标注，纯置信度口径。"""
    run_script("engine.py", iso_root, "--no-strict-approval")
    rep = report(iso_root)
    assert "门控" not in rep
    assert "过渡模式" not in rep
    assert "**CNY**: 未来4周购汇需求 11,880" in rep
