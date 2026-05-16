"""敏感数据脱敏映射表。

按 ADR-005 设计：LLM / Agent 上下文中只出现脱敏代号（`ACC-0001`、`CUST-0023`），
真实账号/客户名仅在 Tool 层与下游真实接口对接前 unmask。

Phase 2 提供框架：默认空映射，由调用方按需 mask()。
真实业务数据导入是 Phase 3 任务（接 ERP / 主数据 / 银行账户系统）。
"""
from __future__ import annotations

from functools import lru_cache
from threading import Lock
from typing import Literal

Category = Literal["ACC", "CUST", "CP", "ENTITY"]


class MaskingMap:
    """线程安全的双向脱敏映射。

    - mask(real, category) → code：真实值进入即返回稳定代号，重复调用返回同一代号
    - unmask(code) → real | None：代号 → 真实值，仅 Tool 层在调外部接口前调用
    """

    def __init__(self) -> None:
        self._real_to_code: dict[tuple[str, str], str] = {}
        self._code_to_real: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        self._lock = Lock()

    def mask(self, real: str, category: Category = "ACC") -> str:
        if not real:
            raise ValueError("real value must be non-empty")
        key = (category, real)
        with self._lock:
            if key in self._real_to_code:
                return self._real_to_code[key]
            n = self._counters.get(category, 0) + 1
            self._counters[category] = n
            code = f"{category}-{n:04d}"
            self._real_to_code[key] = code
            self._code_to_real[code] = real
            return code

    def unmask(self, code: str) -> str | None:
        return self._code_to_real.get(code)

    def clear(self) -> None:
        """仅用于测试。生产环境清空脱敏表 = 丢失业务连续性。"""
        with self._lock:
            self._real_to_code.clear()
            self._code_to_real.clear()
            self._counters.clear()


@lru_cache(maxsize=1)
def get_masking_map() -> MaskingMap:
    """运行时单例。测试中通过 `get_masking_map.cache_clear()` 重置。"""
    return MaskingMap()
