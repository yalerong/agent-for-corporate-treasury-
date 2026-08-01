"""PR3 指标层：REGISTRY 与 metrics.yaml 登记同步、lineage.json 落盘与血缘内容。"""
import json

import metrics
import yaml


def test_registry_synced_with_yaml():
    ids = [m["id"] for m in metrics.load_registry()]
    assert ids == ["budget_variance", "mom_attribution", "forecast_4w", "position",
                   "fx_advice", "related_party", "approvals_profile", "pattern_validation"]
    assert set(ids) == set(metrics.REGISTRY)
    for m in metrics.load_registry():
        assert m["name"] and m["desc"]  # 登记处必须有中文名与口径说明


def test_lineage_json(pipeline_root):
    lineage = json.loads((pipeline_root / "runs" / "2026-07-30" / "lineage.json")
                         .read_text(encoding="utf-8"))
    assert set(lineage) == set(metrics.REGISTRY)
    pats = yaml.safe_load(
        (pipeline_root / "patterns" / "patterns.yaml").read_text(encoding="utf-8"))
    all_ids = {p["id"] for p in pats["patterns"]}
    for ln in lineage.values():
        assert ln["data_range"] == ["2026-01-02", "2026-07-30"]
        assert ln["db_row_count"] == 245
        assert ln["patterns_generated_at"]
        assert ln["computed_at"]
        assert set(ln["pattern_ids"]) <= all_ids
    # 计算口径（0 approved 过渡=high）的血缘应恰为 4 条 high 规律
    high_ids = {p["id"] for p in pats["patterns"] if p["confidence"] == "high"}
    assert set(lineage["fx_advice"]["pattern_ids"]) == high_ids
    assert set(lineage["position"]["pattern_ids"]) == high_ids
    # 无规律参与的指标血缘为空
    assert lineage["budget_variance"]["pattern_ids"] == []
    assert lineage["approvals_profile"]["pattern_ids"] == []
    # 预测血缘覆盖全部 high（provisional 行仅提示也在预测里，此处只卡下界）
    assert high_ids <= set(lineage["forecast_4w"]["pattern_ids"])
