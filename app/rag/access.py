"""检索侧过滤：只有"现在算数"的文档块才能进上下文。

条件（全部满足，且字段缺失一律不满足——没有元数据的块查不到，而不是默认放行）：
- status == approved                   待审 / 已废止的不参与回答
- eff_from <= 今天 <= eff_to           未到生效日、已过失效日的不参与
- superseded_on > 今天                 被已批准且已生效的新文档取代的不参与

**角色 / 密级过滤目前有意不做**（2026-10 决定：权限先全部放开）。元数据里的 classification
已随每个块写入，以后要加只需在这里多拼一个 `metadata.classification` 的 MatchAny 条件，
调用方不用改。
"""
from __future__ import annotations

from datetime import date

from qdrant_client.http import models as qm

from app.rag.docmeta import to_int

_KEY = "metadata."


def active_filter(today: date | None = None) -> qm.Filter:
    t = to_int(today or date.today())
    return qm.Filter(
        must=[
            qm.FieldCondition(key=_KEY + "status", match=qm.MatchValue(value="approved")),
            qm.FieldCondition(key=_KEY + "eff_from", range=qm.Range(lte=t)),
            qm.FieldCondition(key=_KEY + "eff_to", range=qm.Range(gte=t)),
            qm.FieldCondition(key=_KEY + "superseded_on", range=qm.Range(gt=t)),
        ]
    )
