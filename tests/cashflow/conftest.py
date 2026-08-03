"""cashflow 流水线测试基座：在临时目录里以子进程跑完整链路（合成数据，离线、确定性）。"""
import shutil
import tempfile
from pathlib import Path

import pytest
from pipeline_utils import run_script


@pytest.fixture
def tmp_dir():
    """与根 conftest 同款（此处冗余定义使 cashflow 测试可脱离 app/ 依赖单独跑）。"""
    path = Path(tempfile.mkdtemp(prefix="treasury_cf_tmp_"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="session")
def pipeline_root() -> Path:
    """跑一遍 make_sample → ingest → patterns → engine，返回数据根目录。

    与 tests/conftest.py 同理，不用 pytest 自带 tmp_path_factory——
    其 %TEMP%\\pytest-of-* 目录在某些 Windows 环境有权限问题。
    """
    root = Path(tempfile.mkdtemp(prefix="treasury_cf_"))
    try:
        for script in ("make_sample.py", "ingest.py", "ingest_balances.py",
                       "patterns.py", "validate.py", "engine.py"):
            run_script(script, root)
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
