"""静态展示页数据导出（export_demo.py）：合成流水线全链路 → data.json 结构断言。"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from pipeline_utils import CASHFLOW_DIR


def test_export_demo_produces_data_json():
    out = Path(tempfile.mkdtemp(prefix="treasury_demo_out_")) / "data.json"
    r = subprocess.run(
        [sys.executable, "export_demo.py", "--out", str(out)], cwd=CASHFLOW_DIR,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr
    d = json.loads(out.read_text(encoding="utf-8"))
    assert set(d) >= {"note", "meta", "kpis", "patterns", "report_html", "forecast"}
    assert d["kpis"]["patterns"] == 12
    assert d["kpis"]["approved"] == 4          # demo 里 high 全批,strict 门控生效
    assert d["kpis"]["violated"] >= 1          # Vendor-4 断缴样例进核验清单
    assert "strict-approval 开启" in d["report_html"]
    assert "合成" in d["note"]                  # 对外声明必须在
