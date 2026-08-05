"""多智能体 demo 的两条底线测试：审批 Gate 超阈值必拒、无 key 全流程可跑。"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "demo" / "multi_agent.py"


@pytest.fixture(scope="module")
def demo():
    """按文件路径加载 demo（demo/ 是平铺脚本目录，不是包）。"""
    pytest.importorskip("langgraph", reason="demo 依赖 langgraph（见 requirements.txt）")
    sys.path.insert(0, str(REPO / "cashflow"))  # policy/constants 等兄弟模块
    spec = importlib.util.spec_from_file_location("multi_agent_demo", DEMO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _proposal(kind: str, amount: float, currency: str = "CNY", text: str = "HQ|运营|Vendor-1"):
    return {"kind": kind, "amount": amount, "currency": currency, "text": text,
            "title": "t", "why": "w"}


def test_gate_rejects_above_hard_limit(demo):
    """超硬限必须直接拒绝，且说明触发的红线——人工通道都不给开。"""
    import policy
    pol = policy.load_policy()
    limit = 120_000.0

    # 关联方关键词命中 → require_human 档；金额又超硬限 → 拒绝
    over = demo.judge(_proposal("prefund", 151_879.68, text="HQ|拆借|深圳子公司(关联方)"),
                      pol, limit)
    assert over["verdict"] == "rejected"
    assert any("硬限" in r for r in over["redlines"])
    assert any("关联方" in r for r in over["redlines"])

    # 同样超硬限的跨主体调拨（靠动作红线进 require_human 档）同样拒绝
    big_transfer = demo.judge(_proposal("transfer", 500_000.0, text="HK Co|SG Co|主体间调拨"),
                              pol, limit)
    assert big_transfer["verdict"] == "rejected"

    # 未超硬限的跨主体调拨：停下来等人工，不是自动放行
    small_transfer = demo.judge(_proposal("transfer", 80_000.0, text="HK Co|SG Co|主体间调拨"),
                                pol, limit)
    assert small_transfer["verdict"] == "needs_human"
    assert any("跨主体调拨" in r for r in small_transfer["redlines"])

    # 小额非调拨：放行（demo 不会把所有事都拦成噪音）
    small = demo.judge(_proposal("fx_purchase", 11_879.68, text="购汇|CNY"), pol, limit)
    assert small["verdict"] == "passed"

    # 硬限可配置：调低后原本只需人批的调拨也被拒
    assert demo.judge(_proposal("transfer", 80_000.0), pol, 50_000.0)["verdict"] == "rejected"


def test_runs_without_any_llm_key():
    """无任何 key 时一条命令跑通：三个角色都出场，且至少拦下一条。"""
    pytest.importorskip("langgraph", reason="demo 依赖 langgraph（见 requirements.txt）")
    # 置空而非删除：llm_client 会 load_dotenv，已存在的键（含空串）不会被 .env 覆盖
    env = {**os.environ, "LLM_PROVIDER": "", "LLM_API_KEY": "", "ANTHROPIC_API_KEY": "",
           "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run([sys.executable, str(DEMO), "--auto"], cwd=REPO, env=env,
                       capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    out = r.stdout
    assert "未配置" in out, "应显式说明走了确定性降级"
    for role in ("现金流分析 Agent", "调拨建议 Agent", "人工审批 Gate"):
        assert f"[{role}]" in out, f"轨迹缺少角色 {role}"
    assert "❌ 拒绝" in out, "演示必须至少展示一次 gate 拦截"
    assert "触发红线" in out
    assert "红线拒绝 1" in out
