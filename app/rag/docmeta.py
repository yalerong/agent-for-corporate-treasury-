"""文档元数据：状态、密级、生效期、取代关系。

目的：让"哪份文档现在算数、谁能看"成为数据而不是人脑里的约定。默认全部**从严**：
- status 缺省 draft（待审，检索不到）——必须有人明确写 approved 才生效
- classification 缺省 restricted（最高密级）——必须明确降级才对低权限角色开放
- 元数据写错（拼错字段、日期不合法）→ 该文档整份不入库并报错，绝不猜一个值放行

元数据来源，后者覆盖前者（逐字段合并）：
  1. 目录默认：root 到文档所在目录沿途的 `_meta.yaml`（只许写目录级字段，不许写 doc_id/title/supersedes）
  2. 文档自带：.md/.txt 开头的 `---` YAML front matter；其他格式用同目录 `<文件名>.meta.yaml`
     （同一份文档两处都有 → 报错，避免两处说法不一致时悄悄取其一）

日期在 payload 里存成 YYYYMMDD 整数，便于检索侧用范围过滤。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import yaml

DOC_TYPES = ("regulation", "policy", "ruling")
STATUSES = ("draft", "approved", "deprecated")
CLASSIFICATIONS = ("public", "internal", "restricted")  # 顺序即密级高低
NEVER = 99999999

_DIR_FIELDS = {"doc_type", "status", "classification", "effective_from", "effective_to",
               "entities", "owner"}
_DOC_FIELDS = _DIR_FIELDS | {"doc_id", "title", "supersedes"}


class MetaError(Exception):
    """元数据非法。该文档不入库（旧版本的块保留）。"""


@dataclass(frozen=True)
class DocMeta:
    doc_id: str
    title: str | None = None
    doc_type: str = "policy"
    status: str = "draft"
    classification: str = "restricted"
    effective_from: date | None = None
    effective_to: date | None = None
    supersedes: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    owner: str | None = None

    @property
    def eff_from(self) -> int:
        return to_int(self.effective_from) if self.effective_from else 0

    @property
    def eff_to(self) -> int:
        return to_int(self.effective_to) if self.effective_to else NEVER

    def to_dict(self) -> dict:
        """可 JSON 化（进 manifest）。"""
        return {
            "doc_id": self.doc_id, "title": self.title, "doc_type": self.doc_type,
            "status": self.status, "classification": self.classification,
            "effective_from": self.effective_from.isoformat() if self.effective_from else None,
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "supersedes": list(self.supersedes), "entities": list(self.entities),
            "owner": self.owner,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DocMeta:
        return cls(
            doc_id=d["doc_id"], title=d.get("title"), doc_type=d["doc_type"],
            status=d["status"], classification=d["classification"],
            effective_from=_to_date(d.get("effective_from"), "effective_from"),
            effective_to=_to_date(d.get("effective_to"), "effective_to"),
            supersedes=tuple(d.get("supersedes") or ()), entities=tuple(d.get("entities") or ()),
            owner=d.get("owner"),
        )

    def payload(self) -> dict:
        """写进每个块 metadata 的检索字段。superseded_on 由同步的最后一步另行维护。"""
        return {
            "doc_id": self.doc_id, "title": self.title, "doc_type": self.doc_type,
            "status": self.status, "classification": self.classification,
            "eff_from": self.eff_from, "eff_to": self.eff_to,
            "entities": list(self.entities), "owner": self.owner,
        }


def to_int(d: date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def _to_date(v, field: str) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v.strip())
        except ValueError:
            pass
    raise MetaError(f"{field} 不是合法日期（要 YYYY-MM-DD）: {v!r}")


def _to_str_tuple(v, field: str) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        v = [x for x in (s.strip() for s in v.replace("，", ",").split(",")) if x]
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise MetaError(f"{field} 应为字符串列表: {v!r}")
    return tuple(x.strip() for x in v)


def _load_yaml_mapping(text: str, where: str) -> dict:
    try:
        data = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError) as e:  # 2026-13-40 这类非法日期 PyYAML 抛 ValueError
        raise MetaError(f"{where} 解析失败（YAML 语法或日期不合法，日期要 YYYY-MM-DD）: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise MetaError(f"{where} 应为键值对")
    return data


def split_front_matter(text: str) -> tuple[dict | None, str]:
    """开头是 `---` 围起来的 YAML 则拆出来；否则 (None, 原文)。front matter 不进索引正文。"""
    t = text.lstrip("﻿")
    lines = t.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return _load_yaml_mapping("".join(lines[1:i]), "front matter"), "".join(lines[i + 1:])
    return None, text  # 没有结束线：当普通正文，不当元数据


def _dir_meta_files(root: Path, path: Path) -> list[Path]:
    """root 到文档所在目录沿途的 _meta.yaml，浅到深。"""
    dirs = [root]
    for part in path.parent.relative_to(root).parts:
        dirs.append(dirs[-1] / part)
    return [d / "_meta.yaml" for d in dirs if (d / "_meta.yaml").is_file()]


def sidecar_path(path: Path) -> Path:
    return path.with_name(path.name + ".meta.yaml")


def inputs_digest(root: Path, path: Path, file_sha: str) -> str:
    """文档指纹 = 文件内容 + 影响它元数据的外部文件。任一变化都应触发重新入库。"""
    h = hashlib.sha256(file_sha.encode())
    for extra in (*_dir_meta_files(root, path), sidecar_path(path)):
        if extra.is_file():
            h.update(extra.name.encode())
            h.update(extra.read_bytes())
    return h.hexdigest()


def resolve_meta(root: Path, path: Path, source: str, front_matter: dict | None) -> DocMeta:
    """合并三层元数据并校验。front_matter 由调用方在解析正文时一并取出。"""
    raw: dict = {}
    for f in _dir_meta_files(root, path):
        layer = _load_yaml_mapping(f.read_text(encoding="utf-8-sig"), str(f.relative_to(root)))
        bad = set(layer) - _DIR_FIELDS
        if bad:
            raise MetaError(
                f"{f.relative_to(root).as_posix()} 含不允许的目录级字段或拼写错误: {sorted(bad)}"
            )
        raw.update(layer)

    side = sidecar_path(path)
    own: dict = {}
    if front_matter is not None and side.is_file():
        raise MetaError("front matter 与 .meta.yaml 同时存在，请只保留一处")
    if front_matter is not None:
        own = front_matter
    elif side.is_file():
        own = _load_yaml_mapping(side.read_text(encoding="utf-8-sig"), side.name)
    bad = set(own) - _DOC_FIELDS
    if bad:
        raise MetaError(f"未知元数据字段（拼写错误？）: {sorted(bad)}")
    raw.update(own)

    doc_type = raw.get("doc_type", "policy")
    status = raw.get("status", "draft")
    classification = raw.get("classification", "restricted")
    for name, val, allowed in (("doc_type", doc_type, DOC_TYPES), ("status", status, STATUSES),
                               ("classification", classification, CLASSIFICATIONS)):
        if val not in allowed:
            raise MetaError(f"{name} 取值非法 {val!r}，可选 {list(allowed)}")

    eff_from = _to_date(raw.get("effective_from"), "effective_from")
    eff_to = _to_date(raw.get("effective_to"), "effective_to")
    if eff_from and eff_to and eff_to < eff_from:
        raise MetaError("effective_to 早于 effective_from")

    doc_id = raw.get("doc_id") or source
    if not isinstance(doc_id, str) or not doc_id.strip():
        raise MetaError("doc_id 应为非空字符串")
    owner = raw.get("owner")
    title = raw.get("title")
    for name, val in (("owner", owner), ("title", title)):
        if val is not None and not isinstance(val, str):
            raise MetaError(f"{name} 应为字符串")
    supersedes = _to_str_tuple(raw.get("supersedes"), "supersedes")
    if doc_id in supersedes:
        raise MetaError("supersedes 不能包含自己")
    return DocMeta(
        doc_id=doc_id.strip(), title=title, doc_type=doc_type, status=status,
        classification=classification, effective_from=eff_from, effective_to=eff_to,
        supersedes=supersedes, entities=_to_str_tuple(raw.get("entities"), "entities"),
        owner=owner,
    )


def compute_superseded_on(docs: dict[str, dict]) -> tuple[dict[str, int], list[str]]:
    """docs: source → DocMeta.to_dict()。返回 (source → superseded_on, 警告)。

    superseded_on = 取代它的已批准文档中最早的生效日（YYYYMMDD）；没人取代 = NEVER。
    检索侧要求 superseded_on > 今天，所以"新裁决尚未生效"时旧文档仍然算数。
    只有 approved 的取代者才算（草稿不能把现行文档顶掉）；互相取代的对子视为矛盾，
    两边都不生效取代关系并告警，交给人处理。
    """
    by_id: dict[str, str] = {}
    for src, d in docs.items():
        by_id[d["doc_id"]] = src
    warnings: list[str] = []
    edges: dict[str, set[str]] = {}  # 被取代 doc_id → {取代者 doc_id}
    for d in docs.values():
        for old in d["supersedes"]:
            if old not in by_id:
                warnings.append(f"{d['doc_id']} 声明取代 {old}，但知识库里没有这份文档")
                continue
            edges.setdefault(old, set()).add(d["doc_id"])
    result = {src: NEVER for src in docs}
    for old, news in edges.items():
        old_src = by_id[old]
        for new in sorted(news):
            new_d = docs[by_id[new]]
            if new in docs[old_src]["supersedes"]:
                warnings.append(f"{old} 与 {new} 互相声明取代，取代关系均不生效，请人工裁定")
                continue
            if new_d["status"] != "approved":
                continue
            eff = to_int(date.fromisoformat(new_d["effective_from"])) if new_d[
                "effective_from"] else 0
            result[old_src] = min(result[old_src], eff)
    return result, warnings
