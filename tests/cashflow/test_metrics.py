"""PR3 指标层：REGISTRY 与 metrics.yaml 登记同步、lineage.json 落盘与血缘内容。"""
import json
import sqlite3

import metrics
import pandas as pd
import pattern_store as ps
import pytest
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
    # fixture 已显式批准全部 high；但仍有候选行覆盖的币种不能进入头寸/FX 血缘
    high_ids = {p["id"] for p in pats["patterns"] if p["confidence"] == "high"}
    unknown_currencies = {
        p["key"].get("currency")
        for p in pats["patterns"]
        if p["type"] in {"weekly_level", "recurring"} and ps.status_of(p) != "approved"
    }
    known_high_ids = {
        p["id"] for p in pats["patterns"]
        if p["type"] in {"weekly_level", "recurring"}
        and p["confidence"] == "high"
        and p["key"].get("currency") not in unknown_currencies
    }
    assert set(lineage["fx_advice"]["pattern_ids"]) == known_high_ids
    assert set(lineage["position"]["pattern_ids"]) == known_high_ids
    # 无规律参与的指标血缘为空
    assert lineage["budget_variance"]["pattern_ids"] == []
    assert lineage["approvals_profile"]["pattern_ids"] == []
    # forecast_4w 正式预测同样只记录参与计算的 approved 行
    assert set(lineage["forecast_4w"]["pattern_ids"]) == high_ids


def _write_metric_fixture(tmp_dir, meta_end="2026-09-11", meta_rows=2,
                          balance_asof="2026-09-11", include_fingerprint=True):
    (tmp_dir / "patterns").mkdir()
    (tmp_dir / "data" / "db").mkdir(parents=True)
    pat = tmp_dir / "patterns" / "patterns.yaml"
    db = tmp_dir / "data" / "db" / "treasury.db"
    pay_rows = pd.DataFrame({"row_hash": ["h1", "h2"]})
    meta = {"schema_version": 2, "generated_at": "t",
            "data_range": ["2026-09-01", meta_end], "rows": meta_rows}
    if include_fingerprint:
        meta["payments_fingerprint"] = ps.payments_fingerprint(pay_rows)
    pat.write_text(yaml.safe_dump({
        "meta": meta,
        "patterns": [],
    }, allow_unicode=True), encoding="utf-8")
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE payments(date TEXT, entity TEXT, project TEXT, currency TEXT, payee TEXT, amount REAL, row_hash TEXT)"
    )
    con.executemany(
        "INSERT INTO payments VALUES(?,?,?,?,?,?,?)",
        [
            ("2026-09-10", "A", "P", "USD", "X", 100.0, "h1"),
            ("2026-09-11", "A", "P", "USD", "Y", 100.0, "h2"),
        ],
    )
    con.execute(
        "CREATE TABLE balances(as_of TEXT, entity TEXT, bank TEXT, account TEXT, currency TEXT, balance REAL, source_file TEXT)"
    )
    con.execute(
        "INSERT INTO balances VALUES(?,?,?,?,?,?,?)",
        (balance_asof, "A", "B", "acct", "USD", 1000.0, "fixture"),
    )
    con.commit()
    con.close()
    return pat, db


def _patch_metric_paths(monkeypatch, tmp_dir, pat, db):
    monkeypatch.setattr(metrics, "ROOT", tmp_dir)
    monkeypatch.setattr(metrics, "PAT", pat)
    monkeypatch.setattr(metrics, "DB", db)


def test_require_fresh_rejects_stale_pattern_metadata(tmp_dir, monkeypatch):
    pat, db = _write_metric_fixture(tmp_dir, meta_end="2026-09-08")
    _patch_metric_paths(monkeypatch, tmp_dir, pat, db)

    with pytest.raises(SystemExit, match="patterns.yaml data_range"):
        metrics.build_context("2026-09-11", True, require_fresh=True, max_payment_age_days=1)


def test_require_fresh_rejects_unparseable_pattern_metadata(tmp_dir, monkeypatch):
    pat, db = _write_metric_fixture(tmp_dir, meta_end="NaT")
    _patch_metric_paths(monkeypatch, tmp_dir, pat, db)

    with pytest.raises(SystemExit, match="patterns.yaml data_range 无法解析"):
        metrics.build_context("2026-09-11", True, require_fresh=True, max_payment_age_days=1)


def test_require_fresh_rejects_pattern_row_count_drift(tmp_dir, monkeypatch):
    pat, db = _write_metric_fixture(tmp_dir, meta_rows=1)
    _patch_metric_paths(monkeypatch, tmp_dir, pat, db)

    with pytest.raises(SystemExit, match="patterns.yaml rows"):
        metrics.build_context("2026-09-11", True, require_fresh=True, max_payment_age_days=1)


def test_require_fresh_rejects_missing_pattern_fingerprint(tmp_dir, monkeypatch):
    pat, db = _write_metric_fixture(tmp_dir, include_fingerprint=False)
    _patch_metric_paths(monkeypatch, tmp_dir, pat, db)

    with pytest.raises(SystemExit, match="payments_fingerprint"):
        metrics.build_context("2026-09-11", True, require_fresh=True, max_payment_age_days=1)


def test_require_fresh_rejects_pattern_fingerprint_drift(tmp_dir, monkeypatch):
    pat, db = _write_metric_fixture(tmp_dir)
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE payments SET amount=?, row_hash=? WHERE payee=?",
        (125.0, "changed", "Y"),
    )
    con.commit()
    con.close()
    _patch_metric_paths(monkeypatch, tmp_dir, pat, db)

    with pytest.raises(SystemExit, match="payments_fingerprint"):
        metrics.build_context("2026-09-11", True, require_fresh=True, max_payment_age_days=1)


def test_require_fresh_rejects_stale_selected_balance_snapshot(tmp_dir, monkeypatch):
    pat, db = _write_metric_fixture(tmp_dir, balance_asof="2026-09-08")
    _patch_metric_paths(monkeypatch, tmp_dir, pat, db)

    with pytest.raises(SystemExit, match="balance snapshot"):
        metrics.build_context("2026-09-11", True, require_fresh=True, max_payment_age_days=1)


def test_require_fresh_rejects_stale_accounts_even_when_another_account_is_current(
    tmp_dir, monkeypatch
):
    pat, db = _write_metric_fixture(tmp_dir)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO balances VALUES(?,?,?,?,?,?,?)",
        ("2026-09-08", "A", "B", "stale-acct", "USD", 500.0, "fixture"),
    )
    con.commit()
    con.close()
    _patch_metric_paths(monkeypatch, tmp_dir, pat, db)

    with pytest.raises(SystemExit, match="1 个账户"):
        metrics.build_context("2026-09-11", True, require_fresh=True, max_payment_age_days=1)


def test_require_fresh_rejects_invalid_balance_snapshot_date(tmp_dir, monkeypatch):
    pat, db = _write_metric_fixture(tmp_dir, balance_asof="")
    _patch_metric_paths(monkeypatch, tmp_dir, pat, db)

    with pytest.raises(SystemExit, match="balance snapshot 含非法 as_of"):
        metrics.build_context("2026-09-11", True, require_fresh=True, max_payment_age_days=1)


def test_require_fresh_checks_the_balance_snapshot_the_report_will_select(tmp_dir, monkeypatch):
    pat, db = _write_metric_fixture(
        tmp_dir,
        meta_end="2026-09-12",
        balance_asof="2026-09-12",
    )
    _patch_metric_paths(monkeypatch, tmp_dir, pat, db)

    with pytest.raises(SystemExit, match="报告截止 2026-09-11 前为空"):
        metrics.build_context("2026-09-12", True, require_fresh=True, max_payment_age_days=1)


def test_require_fresh_accepts_current_decision_inputs(tmp_dir, monkeypatch):
    pat, db = _write_metric_fixture(tmp_dir)
    _patch_metric_paths(monkeypatch, tmp_dir, pat, db)

    ctx = metrics.build_context("2026-09-11", True, require_fresh=True, max_payment_age_days=1)
    assert ctx["asof"] == pd.Timestamp("2026-09-11")


def test_candidate_currency_stays_unknown_for_position_and_fx(monkeypatch):
    official = pd.DataFrame(
        [
            {
                "entity": "SG Co", "project": "Ops", "currency": "CNY", "payee": "",
                "week": "W+1", "start": "2026-08-01", "forecast": 151880.0,
                "source": "weekly_level", "confidence": "high", "status": "approved",
                "pattern_id": "approved-cny", "note": "",
            },
            {
                "entity": "US Co", "project": "Ops", "currency": "USD", "payee": "",
                "week": "W+1", "start": "2026-08-01", "forecast": 100.0,
                "source": "weekly_level", "confidence": "high", "status": "approved",
                "pattern_id": "approved-usd", "note": "",
            },
        ],
        columns=metrics.FORECAST_COLUMNS,
    )
    official.attrs["candidates"] = pd.DataFrame(
        [
            {
                "entity": "US Co", "project": "Ops", "currency": "USD", "payee": "",
                "week": "W+2", "start": "2026-08-08", "forecast": 50000.0,
                "source": "weekly_level", "confidence": "high", "status": "candidate",
                "pattern_id": "candidate-usd", "note": "",
            },
        ],
        columns=metrics.FORECAST_COLUMNS,
    )
    balances = pd.DataFrame(
        [
            {"entity": "SG Co", "bank": "B", "account": "1", "currency": "CNY",
             "as_of": "2026-07-30", "balance": 60000.0},
            {"entity": "HK Co", "bank": "B", "account": "2", "currency": "CNY",
             "as_of": "2026-07-30", "balance": 80000.0},
            {"entity": "US Co", "bank": "B", "account": "3", "currency": "USD",
             "as_of": "2026-07-30", "balance": 100000.0},
        ]
    )
    monkeypatch.setattr(metrics, "load_balances", lambda _asof: balances)
    ctx = {
        "asof": pd.Timestamp("2026-07-30"), "strict": True,
        "pay": pd.DataFrame(columns=["payee", "entity", "amount"]),
        "bud": None, "month": "2026-07",
    }
    out = {"forecast_4w": {"value": official}}

    position, position_ids = metrics.position_metric(ctx, out)
    out["position"] = {"value": position}
    fx, fx_ids = metrics.fx_advice_metric(ctx, out)

    rows = {row["currency"]: row for row in position["currency_rows"]}
    assert rows["USD"]["unknown"] is True
    assert rows["USD"]["outflow"] is None
    assert rows["CNY"]["position"] == -11880.0
    assert position["events"] == [
        {"kind": "transfer", "donor": "HK Co", "recipient": "SG Co", "currency": "CNY",
         "amount": 80000.0, "need_before": 91880.0, "donor_avail_before": 80000.0},
        {"kind": "residual", "entity": "SG Co", "currency": "CNY", "amount": 11880.0},
    ]
    assert {item["currency"] for item in fx["items"]} == {"CNY"}
    assert position["unknown_currencies"] == ["USD"]
    assert fx["unknown_currencies"] == ["USD"]
    assert position_ids == ["approved-cny"]
    assert fx_ids == ["approved-cny"]
