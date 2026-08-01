"""导出合成示例数据 → docs/data.json（对外静态展示页的数据源，绝不触真实数据）。

在临时目录跑一遍完整合成流水线（make_sample → ingest → ingest_balances → patterns →
approve 全部 high → validate → engine），收集 KPI / 规律库 / 报告（预渲染 HTML）/
预测明细 / 核验结果，写入 docs/data.json。报告渲染复用审批台同一实现（ui.md_to_html）。

用法: python export_demo.py [--out ../docs/data.json]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd
import yaml
from ui import md_to_html

CODE = Path(__file__).parent


def run(script: str, root: Path, *args: str) -> None:
    r = subprocess.run(
        [sys.executable, script, *args], cwd=CODE,
        env={**os.environ, "CASHFLOW_ROOT": str(root), "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise SystemExit(f"{script} 失败:\n{r.stdout}\n{r.stderr}")


def build_data() -> dict:
    root = Path(tempfile.mkdtemp(prefix="treasury_demo_"))
    try:
        for s in ("make_sample.py", "ingest.py", "ingest_balances.py", "patterns.py"):
            run(s, root)
        run("approve.py", root, "approve", "--all", "--confidence", "high", "--by", "demo")
        run("validate.py", root)
        run("engine.py", root)

        doc = yaml.safe_load((root / "patterns" / "patterns.yaml").read_text(encoding="utf-8"))
        run_dir = sorted((root / "runs").iterdir())[-1]
        report = (run_dir / "report.md").read_text(encoding="utf-8")
        fc = pd.read_csv(run_dir / "forecast.csv", encoding="utf-8-sig")
        lineage = json.loads((run_dir / "lineage.json").read_text(encoding="utf-8"))

        pats = doc["patterns"]
        st = {}
        for p in pats:
            st[p["status"]] = st.get(p["status"], 0) + 1
        checked = [p for p in pats if (p.get("evidence") or {}).get("last_validated")]
        return {
            "note": "本页全部数字来自固定种子的合成示例数据（make_sample.py），与任何真实业务无关。",
            "meta": doc["meta"],
            "kpis": {
                "payments": doc["meta"]["rows"],
                "patterns": len(pats),
                "high": sum(1 for p in pats if p["confidence"] == "high"),
                "approved": st.get("approved", 0),
                "candidate": st.get("candidate", 0),
                "refuted": st.get("refuted", 0),
                "forecast_rows": len(fc),
                "require_human": int((fc.get("review") == "require_human").sum()),
                "checked": len(checked),
                "violated": sum(1 for p in checked if p["evidence"]["misses"] > 0),
            },
            "patterns": [
                {"id": p["id"], "type": p["type"], "key": p["key"], "claim": p["claim"],
                 "status": p["status"], "confidence": p["confidence"],
                 "evidence": p.get("evidence") or {}} for p in pats],
            "report_html": md_to_html(report),
            "forecast": fc.to_dict("records"),
            "lineage_sample": {k: v for k, v in list(lineage.items())[:3]},
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(CODE.parent / "docs" / "data.json"))
    args = ap.parse_args()
    data = build_data()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"合成 demo 数据 → {out}（patterns {data['kpis']['patterns']} 条，"
          f"approved {data['kpis']['approved']}，violated {data['kpis']['violated']}）")


if __name__ == "__main__":
    main()
