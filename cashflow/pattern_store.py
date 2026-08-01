"""规律库存取层：pattern_id、schema v2 三态语义、加载/保存、v1 状态继承。

schema v2（meta.schema_version: 2）:
  status: candidate（重算自动产生）| approved（人工批准，strict 门控下才进计算）
        | refuted（人工否决，重算保持不复活，防重提）
  approved 为过渡期派生字段（= status=="approved"），PR3 删除。

自动重算永不改人工状态：按 id 从老库继承 STATE_FIELDS；未被重算命中的
refuted / llm 来源条目留存（防重提、防外源丢失）。
id 对 key 的 dict 顺序不敏感；v1 老文件（str(key)+type 脆弱键）读入时一次性兜底匹配。
"""
import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

import yaml

SCHEMA_VERSION = 2
STATUSES = ("candidate", "approved", "refuted")
SOURCES = ("stats", "llm")
CONFIDENCES = ("high", "provisional")
# 人工/生命周期状态：重算时按 id 从老库继承；统计数值字段一律用新算的
STATE_FIELDS = ("status", "source", "valid_from", "approved_by", "approved_at",
                "refuted_reason", "superseded_by")
_HEAD = ("id", "type", "key", "claim")
_TAIL = ("confidence", "status", "source", "valid_from", "approved_by", "approved_at",
         "refuted_reason", "superseded_by", "evidence", "approved")


def pattern_id(type_: str, key: dict) -> str:
    """身份 = 类型 + key 内容（与 dict 顺序无关），12 位 sha1 前缀。"""
    canon = f"{type_}|" + "|".join(f"{k}={key[k]}" for k in sorted(key))
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:12]


def legacy_key(p: dict) -> str:
    """schema v1 的脆弱身份键（str(key)+type），只用于一次性迁移兜底。"""
    return str(p["key"]) + p["type"]


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def upgrade_pattern(p: dict) -> dict:
    """单条补齐 v2 字段（v1 的 approved:true → status=approved）。幂等，不丢已有字段。"""
    q = dict(p)
    q["id"] = pattern_id(q["type"], q["key"])
    if not q.get("status"):
        q["status"] = "approved" if q.get("approved") else "candidate"
    q.setdefault("source", "stats")
    for f in ("valid_from", "approved_by", "approved_at", "refuted_reason", "superseded_by"):
        q.setdefault(f, None)
    q.setdefault("evidence", {})
    q["approved"] = q["status"] == "approved"  # 过渡派生字段，PR3 删
    return q


def _ordered(p: dict) -> dict:
    """固定字段顺序：身份/主张在前，数值字段居中，状态/审计在尾，yaml 可读。"""
    mid = {k: v for k, v in p.items() if k not in _HEAD + _TAIL}
    return {**{k: p[k] for k in _HEAD}, **mid, **{k: p.get(k) for k in _TAIL}}


def validate(doc: dict) -> None:
    if doc.get("meta", {}).get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"meta.schema_version 必须为 {SCHEMA_VERSION}")
    seen = set()
    for p in doc.get("patterns", []):
        for f in ("id", "type", "key", "claim", "confidence", "status"):
            if f not in p:
                raise ValueError(f"规律缺字段 {f}: {p.get('id', p)}")
        if p["id"] != pattern_id(p["type"], p["key"]):
            raise ValueError(f"id 与内容不符: {p['id']}（key/type 变了必须换 id）")
        if p["status"] not in STATUSES:
            raise ValueError(f"非法 status {p['status']}: {p['id']}")
        if p["confidence"] not in CONFIDENCES:
            raise ValueError(f"非法 confidence {p['confidence']}: {p['id']}")
        if p.get("source", "stats") not in SOURCES:
            raise ValueError(f"非法 source {p.get('source')}: {p['id']}")
        if p["id"] in seen:
            raise ValueError(f"重复 id: {p['id']}")
        seen.add(p["id"])


def load(path: Path | str) -> dict:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    doc.setdefault("meta", {})
    doc.setdefault("patterns", [])
    return doc


def save(path: Path | str, doc: dict, backup: bool = False) -> dict:
    """规范化+校验后写盘；backup=True 时先备份到 <path>.bak。"""
    path = Path(path)
    out = {"meta": {"schema_version": SCHEMA_VERSION,
                    **{k: v for k, v in doc["meta"].items() if k != "schema_version"}},
           "patterns": [_ordered(upgrade_pattern(p)) for p in doc["patterns"]]}
    validate(out)
    if backup and path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(out, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return out


def merge_states(new_pats: list[dict], old_doc: dict | None,
                 default_valid_from: str | None = None) -> list[dict]:
    """重算产物继承老库人工状态（按 id；v1 老库退回 legacy 键兜底）。

    返回完整规律列表：重算命中的条目用新统计值+老人工状态；未命中但
    status=refuted（防重提）或 source=llm（非统计重算产物）的老条目原样留存。
    """
    old_pats = [upgrade_pattern(p) for p in (old_doc or {}).get("patterns", [])]
    by_id = {p["id"]: p for p in old_pats}
    by_legacy = {legacy_key(p): p for p in old_pats}
    out = []
    for p in new_pats:
        q = upgrade_pattern(p)
        old = by_id.get(q["id"]) or by_legacy.get(legacy_key(q))
        if old is not None:
            for f in STATE_FIELDS:
                q[f] = old.get(f)
            q["evidence"] = old.get("evidence") or {}
            q["approved"] = q["status"] == "approved"
        if not q.get("valid_from"):
            q["valid_from"] = default_valid_from
        out.append(q)
    new_ids = {q["id"] for q in out}
    out += [p for p in old_pats if p["id"] not in new_ids
            and (p["status"] == "refuted" or p.get("source") == "llm")]
    return out
