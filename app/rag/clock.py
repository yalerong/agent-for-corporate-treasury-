"""“今天”按知识库配置的时区算，而不是机器本地时区。

容器默认是 UTC：北京时间 00:00–08:00，UTC 还是前一天，生效日当天的新裁决会查不到、
旧裁决还在。生效期、取代日都是业务日期，必须用业务所在时区。
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config import get_settings


def today() -> date:
    return datetime.now(ZoneInfo(get_settings().kb_timezone)).date()
