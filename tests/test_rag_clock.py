"""“今天”按配置时区算：UTC 的前一天，在北京已经是新的一天。"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.config import get_settings
from app.rag import clock
from app.rag.access import active_filter


class _FrozenDatetime(datetime):
    """把 datetime.now(tz) 钉在 UTC 2026-10-14 20:30（= 北京 2026-10-15 04:30）。"""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 14, 20, 30, tzinfo=UTC).astimezone(tz)


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(clock, "datetime", _FrozenDatetime)
    yield
    get_settings.cache_clear()


def test_default_timezone_is_business_day_not_utc(frozen, monkeypatch):
    monkeypatch.delenv("KB_TIMEZONE", raising=False)
    get_settings.cache_clear()
    assert clock.today() == date(2026, 10, 15)  # UTC 还是 14 号，北京已经 15 号


def test_timezone_is_configurable(frozen, monkeypatch):
    monkeypatch.setenv("KB_TIMEZONE", "UTC")
    get_settings.cache_clear()
    assert clock.today() == date(2026, 10, 14)


def test_active_filter_uses_configured_business_day(frozen, monkeypatch):
    monkeypatch.delenv("KB_TIMEZONE", raising=False)
    get_settings.cache_clear()
    f = active_filter()
    eff_from = next(c for c in f.must if c.key.endswith("eff_from"))
    assert eff_from.range.lte == 20261015  # 生效日是 10-15 的新裁决，北京凌晨 4:30 就该生效
