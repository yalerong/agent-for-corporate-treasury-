"""Tool 审计日志装饰器。

按 CLAUDE.md §4.1"资金类 Tool 必须带审计日志"要求实现：
记录调用时间、Tool 名、入参、返回值预览、耗时、异常。
JSONL 格式，每行一条；路径由 settings.audit_log_path 控制。
"""
from __future__ import annotations

import functools
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from app.config import get_settings

F = TypeVar("F", bound=Callable[..., Any])


def _safe_json(obj: Any) -> Any:
    """尽力转为 JSON 可序列化对象；失败则降级为 repr 字符串。"""
    try:
        json.dumps(obj, default=str, ensure_ascii=False)
        return obj
    except (TypeError, ValueError):
        return repr(obj)


def _emit(record: dict[str, Any]) -> None:
    path = Path(get_settings().audit_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def audit(tool_name: str | None = None) -> Callable[[F], F]:
    """装饰资金类 Tool。每次调用追加一条 JSONL 审计记录，调用前后均执行。

    使用方式（@tool 必须在外层，以保留 langchain 元数据）：

        @tool
        @audit()
        def my_tool(...) -> ...: ...
    """

    def decorator(fn: F) -> F:
        name = tool_name or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            result: Any = None
            error: str | None = None
            try:
                result = fn(*args, **kwargs)
                return result
            except Exception as e:
                error = repr(e)
                raise
            finally:
                preview = None
                if result is not None:
                    s = str(result)
                    preview = s[:500] + ("…" if len(s) > 500 else "")
                _emit(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "tool": name,
                        "args": [_safe_json(a) for a in args],
                        "kwargs": {k: _safe_json(v) for k, v in kwargs.items()},
                        "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                        "error": error,
                        "result_preview": preview,
                    }
                )

        return wrapper  # type: ignore[return-value]

    return decorator
