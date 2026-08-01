"""cashflow 测试公用：以子进程在指定数据根目录跑流水线脚本。"""
import os
import subprocess
import sys
from pathlib import Path

CASHFLOW_DIR = Path(__file__).resolve().parents[2] / "cashflow"


def run_script(script: str, root: Path, *args: str) -> subprocess.CompletedProcess:
    # PYTHONIOENCODING: Windows 下子进程被管道接管时默认走 ANSI 代码页，中文输出会
    # 让 utf-8 读取端解码失败（stdout 变 None），统一强制 utf-8
    env = {**os.environ, "CASHFLOW_ROOT": str(root), "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(
        [sys.executable, script, *args],
        cwd=CASHFLOW_DIR, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    assert r.returncode == 0, f"{script} 退出码 {r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    return r
