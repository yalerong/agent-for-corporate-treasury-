"""CLI 入口测试。直接调用 main.run() 而非 subprocess，加快测试。"""
from __future__ import annotations

import pytest

from main import run


class TestArgParsing:
    def test_missing_role_exits(self, capsys):
        with pytest.raises(SystemExit) as exc:
            run(["--task", "inquiry"])
        assert exc.value.code == 2

    def test_missing_task_exits(self, capsys):
        with pytest.raises(SystemExit) as exc:
            run(["--role", "cashier"])
        assert exc.value.code == 2

    def test_invalid_role_exits(self, capsys):
        with pytest.raises(SystemExit) as exc:
            run(["--role", "bad_role", "--task", "inquiry"])
        assert exc.value.code == 2

    def test_invalid_task_exits(self, capsys):
        with pytest.raises(SystemExit) as exc:
            run(["--role", "cashier", "--task", "bad_task"])
        assert exc.value.code == 2


class TestEndToEnd:
    def test_inquiry_success(self, capsys):
        rc = run(["--role", "cashier", "--task", "inquiry", "查一下余额"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "[出纳]" in out

    def test_unauthorized_fx_returns_3(self, capsys):
        rc = run(["--role", "cashier", "--task", "fx"])
        out = capsys.readouterr().out
        assert rc == 3
        assert "无权" in out

    def test_large_transfer_triggers_hitl(self, capsys):
        rc = run(
            ["--role", "treasury_supervisor", "--task", "transfer", "--amount", "10000000"]
        )
        out = capsys.readouterr().out
        assert rc == 4
        assert "等待人工审批" in out
        assert "Thread ID" in out

    def test_small_transfer_routes_to_supervisor(self, capsys):
        rc = run(
            ["--role", "treasury_supervisor", "--task", "transfer", "--amount", "3000000"]
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "[资金主管]" in out

    def test_cashier_transfer_without_approval_rejected(self, capsys):
        rc = run(["--role", "cashier", "--task", "transfer"])
        out = capsys.readouterr().out
        assert rc == 3
        assert "无权" in out

    def test_cashier_transfer_with_approval_succeeds(self, capsys):
        rc = run(
            [
                "--role",
                "cashier",
                "--task",
                "transfer",
                "--approved-id",
                "APR-2026-0001",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "[出纳]" in out

    def test_knowledge_task(self, capsys, fake_llm, populated_stores):
        # knowledge 任务会真实走 search_*_knowledge + LLM 合成；
        # 用 fake_llm + populated_stores 让流程不依赖 HF / 真实 LLM。
        fake_llm(["综合答案 [来源: aml_law.md]"])
        rc = run(["--role", "cashier", "--task", "knowledge", "反洗钱报告标准是多少？"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "[知识库]" in out
        assert "综合答案" in out
