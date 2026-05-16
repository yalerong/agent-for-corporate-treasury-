"""audit 装饰器测试：函数元数据保留、成功/异常记录、JSONL 格式。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.tools.audit import audit


def _read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def audit_log_path(monkeypatch, tmp_dir):
    """指向临时审计日志路径，并清空 get_settings 缓存。"""
    log = tmp_dir / "audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(log))
    from app.config import get_settings
    get_settings.cache_clear()
    return log


class TestAuditDecorator:
    def test_preserves_function_metadata(self):
        @audit()
        def my_tool(x: int) -> int:
            """do something"""
            return x * 2

        assert my_tool.__name__ == "my_tool"
        assert my_tool.__doc__ == "do something"

    def test_success_logs_record(self, audit_log_path):
        @audit()
        def my_tool(x: int) -> int:
            return x * 2

        result = my_tool(3)
        assert result == 6

        records = _read_log(audit_log_path)
        assert len(records) == 1
        r = records[0]
        assert r["tool"] == "my_tool"
        assert r["args"] == [3]
        assert r["kwargs"] == {}
        assert r["error"] is None
        assert "6" in r["result_preview"]
        assert r["duration_ms"] >= 0
        assert "ts" in r

    def test_exception_logs_with_error_and_reraises(self, audit_log_path):
        @audit()
        def failing_tool() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            failing_tool()

        records = _read_log(audit_log_path)
        assert len(records) == 1
        assert records[0]["error"] is not None
        assert "boom" in records[0]["error"]

    def test_custom_tool_name(self, audit_log_path):
        @audit(tool_name="explicit_name")
        def my_tool() -> int:
            return 1

        my_tool()
        assert _read_log(audit_log_path)[0]["tool"] == "explicit_name"

    def test_kwargs_recorded(self, audit_log_path):
        @audit()
        def my_tool(a: int, b: int = 10) -> int:
            return a + b

        my_tool(1, b=5)
        r = _read_log(audit_log_path)[0]
        assert r["args"] == [1]
        assert r["kwargs"] == {"b": 5}

    def test_multiple_calls_appended(self, audit_log_path):
        @audit()
        def my_tool(x: int) -> int:
            return x

        my_tool(1)
        my_tool(2)
        my_tool(3)
        assert len(_read_log(audit_log_path)) == 3

    def test_result_preview_truncated(self, audit_log_path):
        @audit()
        def big_result() -> str:
            return "x" * 1000

        big_result()
        r = _read_log(audit_log_path)[0]
        assert len(r["result_preview"]) <= 501  # 500 chars + 省略号

    def test_non_serializable_args_degrade_to_repr(self, audit_log_path):
        class Weird:
            def __repr__(self):
                return "<Weird>"

        @audit()
        def my_tool(obj) -> int:
            return 1

        my_tool(Weird())
        r = _read_log(audit_log_path)[0]
        assert "<Weird>" in str(r["args"])
