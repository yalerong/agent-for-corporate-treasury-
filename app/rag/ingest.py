"""企业知识库摄取管线：多格式解析 + 内容哈希增量同步。

和 `index_track` 的区别：后者是"全量重建、只认 .md/.txt"，这里是生产路径——
- 解析 .md/.txt/.docx/.pdf/.xlsx/.csv（重依赖延迟导入，纯文本场景不装也能跑）
- 每份文档记哈希，下次只处理 新增 / 改动 / 删除 三类；源头删了，索引里的块也跟着删
- 块 ID 由 (source, 内容哈希, 序号) 确定性生成，重跑不产生重复块
- 单份文档解析失败：保留它旧的块不动（宁可旧，不可空），错误写进报告
- 一次要删掉超过一半的已入库文档 → 默认拒绝（挂载点掉了/路径写错的典型症状），--force 才放行

source 是相对 root 的路径（不含 root 目录名），换机器、换挂载点不变。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.documents import Document
from qdrant_client.http import models as qm

from app.rag.docmeta import (
    NEVER,
    DocMeta,
    MetaError,
    compute_superseded_on,
    inputs_digest,
    resolve_meta,
    split_front_matter,
)
from app.rag.store import Track, chunk_documents

SUPPORTED_SUFFIXES = (".md", ".txt", ".docx", ".pdf", ".xlsx", ".csv")
MASS_DELETE_RATIO = 0.5
_NS = uuid.UUID("6f1c2f0e-3b8e-4c43-9d57-5a0c1b7d9e11")


class ParseError(Exception):
    """文件读不出可用文本（损坏、加密、扫描件无文字层等）。"""


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ParseError("无法按 utf-8 / gb18030 解码")


def _parse_docx(path: Path) -> str:
    from docx import Document as DocxDocument
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = DocxDocument(str(path))
    parts: list[str] = []
    # 段落和表格必须按在文档里的先后顺序走，否则表格会被甩到所有章节之后，挂错标题
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name or "") if p.style is not None else ""
            if style.startswith("Heading"):
                level = "".join(c for c in style if c.isdigit()) or "1"
                parts.append("#" * min(int(level), 6) + " " + text)
            else:
                parts.append(text)
        elif tag == "tbl":
            t = Table(child, doc)
            parts.append("\n".join(
                " | ".join(c.text.strip().replace("\n", " ") for c in r.cells) for r in t.rows
            ))
    return "\n\n".join(parts)


def _parse_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        raise ParseError("PDF 已加密")
    parts = []
    for i, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        if text:
            parts.append(f"[第{i}页]\n{text}")
    return "\n\n".join(parts)


def _parse_xlsx(path: Path) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    parts = []
    try:
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v).strip() for v in row]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                parts.append(f"## {ws.title}\n" + "\n".join(rows))
    finally:
        wb.close()
    return "\n\n".join(parts)


_PARSERS: dict[str, Callable[[Path], str]] = {
    ".md": _read_text,
    ".txt": _read_text,
    ".csv": _read_text,
    ".docx": _parse_docx,
    ".pdf": _parse_pdf,
    ".xlsx": _parse_xlsx,
}


def parse_file(path: Path) -> str:
    """按后缀解析成纯文本。读不出任何文字 → ParseError（扫描件需 OCR，本管线不做）。"""
    parser = _PARSERS.get(path.suffix.lower())
    if parser is None:
        raise ParseError(f"不支持的格式: {path.suffix}")
    try:
        text = parser(path)
    except ParseError:
        raise
    except Exception as e:  # 第三方库对损坏文件抛的异常类型各异
        raise ParseError(f"{type(e).__name__}: {e}") from e
    if not text.strip():
        raise ParseError("解析后无文字（扫描件或空文件）")
    return text


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class Manifest:
    """source → {sha256, chunk_ids, indexed_at}。每处理完一份文档就落盘（原子替换）。"""

    def __init__(self, path: Path):
        self.path = path
        self.docs: dict[str, dict] = {}
        self.collection: str | None = None  # 这份 manifest 描述的是哪个向量集合
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.docs = data.get("docs", {})
            self.collection = data.get("collection")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {"version": 1, "collection": self.collection, "docs": self.docs},
                ensure_ascii=False, indent=1,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)


@dataclass
class SyncReport:
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    scan_errors: list[str] = field(default_factory=list)
    drafts: list[str] = field(default_factory=list)  # 已入库但非 approved：检索不到
    warnings: list[str] = field(default_factory=list)
    blocked: str | None = None
    dry_run: bool = False

    def summary(self) -> str:
        head = "[dry-run] " if self.dry_run else ""
        s = (
            f"{head}新增 {len(self.added)}｜改动 {len(self.changed)}｜"
            f"删除 {len(self.deleted)}｜未变 {len(self.unchanged)}｜失败 {len(self.errors)}"
        )
        if self.drafts:
            s += (
                f"\n其中 {len(self.drafts)} 份尚未批准（status 不是 approved），检索不到；"
                "在文档 front matter / .meta.yaml / _meta.yaml 里写 status: approved 后重跑"
            )
        for w in self.warnings:
            s += f"\n提示: {w}"
        if self.scan_errors:
            s += (
                f"\n扫描不完整（{len(self.scan_errors)} 个目录读不了），"
                "已跳过删除对账: " + "；".join(self.scan_errors)
            )
        if self.blocked:
            s += f"\n已拒绝执行: {self.blocked}"
        return s


def scan_root(root: Path, errors: list[str] | None = None) -> dict[str, Path]:
    """root 下所有受支持文件 → {相对路径(posix): 绝对路径}。跳过 ~$ 临时文件与隐藏条目。

    传了 errors 时，读不了的目录记进 errors 并继续（调用方据此拒绝做删除对账）；
    不传则沿用旧行为：目录读不了直接抛 OSError。
    """
    found: dict[str, Path] = {}

    def walk(directory: Path) -> None:
        try:
            entries = sorted(directory.iterdir())
        except OSError as e:
            if errors is None:
                raise
            errors.append(f"{directory}: {e.strerror or e}")
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                # 符号链接目录不跟，避免环
                if not entry.is_symlink():
                    walk(entry)
            elif not entry.name.startswith("~$") and entry.suffix.lower() in SUPPORTED_SUFFIXES:
                found[entry.relative_to(root).as_posix()] = entry

    walk(root)
    return found


def _chunk_ids(source: str, sha: str, n: int) -> list[str]:
    return [str(uuid.uuid5(_NS, f"{source}#{sha}#{i}")) for i in range(n)]


def _delete_source(store, source: str, keep_sha: str | None = None) -> None:
    """按 source 删块（可保留指定内容哈希的那一版）。

    不依赖 manifest 里记的块 ID：更新中途失败时，新块已写入但还没记进 manifest，
    之后这份文档被改或被删，只按旧记录删就会让那批块永远留在索引里、继续被检索到。
    """
    key = f"{store.METADATA_KEY}."
    must = [qm.FieldCondition(key=key + "source", match=qm.MatchValue(value=source))]
    must_not = (
        [qm.FieldCondition(key=key + "doc_sha", match=qm.MatchValue(value=keep_sha))]
        if keep_sha else []
    )
    store.client.delete(
        store.collection_name,
        points_selector=qm.FilterSelector(filter=qm.Filter(must=must, must_not=must_not)),
    )


def _drop_all(store) -> int:
    """清空集合里所有块（显式重建用：没有 manifest 时无法逐块对账）。"""
    deleted = 0
    while True:
        points, _ = store.client.scroll(
            store.collection_name, limit=256, with_payload=False, with_vectors=False
        )
        if not points:
            return deleted
        store.delete(ids=[str(p.id) for p in points])
        deleted += len(points)


def _upgrade_legacy_entries(
    manifest: Manifest, report: SyncReport, dry_run: bool
) -> None:
    """把上一版 manifest（只有 sha256/chunk_ids/indexed_at）补成带 meta 的格式。

    补的 meta 只是占位：sha256 清空 → 本轮回读元数据并按新格式重建块，
    旧块 ID 保留在 entry 里，重建成功后按 stale 删除。
    """
    legacy = [src for src, d in manifest.docs.items() if "meta" not in d]
    for src in legacy:
        entry = manifest.docs[src]
        entry["meta"] = DocMeta(doc_id=src).to_dict()
        entry["sha256"] = ""
        entry.setdefault("superseded_on", NEVER)
    if legacy:
        report.warnings.append(
            f"manifest 里 {len(legacy)} 份旧格式条目缺元数据，已标记重新入库"
        )
        if not dry_run:
            manifest.save()


def _load(root: Path, path: Path, source: str) -> tuple[DocMeta, str]:
    """解析正文 + 元数据。文本类格式的 front matter 在这里拆掉，不进索引。"""
    text = parse_file(path)
    front = None
    if path.suffix.lower() in (".md", ".txt"):
        front, text = split_front_matter(text)
        if not text.strip():
            raise ParseError("去掉 front matter 后正文为空")
    return resolve_meta(root, path, source, front), text


def _refresh_superseded(store, manifest: Manifest, report: SyncReport, dry_run: bool) -> None:
    """按全库已批准文档重算 superseded_on，只对变化的文档改块 payload。"""
    computed, warnings = compute_superseded_on(
        {src: d["meta"] for src, d in manifest.docs.items()}
    )
    report.warnings.extend(warnings)
    for src, value in computed.items():
        d = manifest.docs[src]
        if d.get("superseded_on", NEVER) == value or dry_run:
            continue
        if d["chunk_ids"]:
            store.client.set_payload(
                store.collection_name,
                payload={"superseded_on": value},
                points=d["chunk_ids"],
                key=store.METADATA_KEY,
            )
        d["superseded_on"] = value
        manifest.save()


def sync_directory(
    root: Path,
    category: str,
    store,
    manifest: Manifest,
    *,
    dry_run: bool = False,
    force: bool = False,
    chunk_size: int = 500,
    chunk_overlap: int = 50,
) -> SyncReport:
    """把 root 与 manifest 对账，增量写入 store。返回逐文档的处理报告。

    dry_run=True 时只读文件系统与 manifest，不碰 store（可以传 store=None）。
    """
    report = SyncReport(dry_run=dry_run)
    if not root.is_dir():
        report.blocked = f"root 不存在或不是目录: {root}"
        return report
    if store is None and not dry_run:
        raise ValueError("store 不能为空（只有 dry_run 可以不带）")
    if dry_run:
        # 预演在 manifest 副本上做：既要按"拟入库"的元数据算草稿/取代关系，
        # 又不能污染调用方手里的 manifest
        manifest = copy.deepcopy(manifest)

    if not dry_run:
        # manifest 只对它建账时的那个集合有效：换了集合名或集合被重建，"哈希没变就跳过"
        # 会让新集合一直是空的却报告成功
        if manifest.docs and manifest.collection not in (None, store.collection_name):
            report.blocked = (
                f"manifest 属于集合 {manifest.collection!r}，当前集合是 {store.collection_name!r}；"
                "换了集合请换一份 manifest（或删掉旧 manifest）后重新同步"
            )
            return report
        if manifest.docs and store.client.count(store.collection_name, exact=True).count == 0:
            report.blocked = (
                f"manifest 记录了 {len(manifest.docs)} 份文档，但集合是空的"
                "（集合被删除或重建过？）；删掉 manifest 后重新同步"
            )
            return report
        manifest.collection = store.collection_name

    on_disk = scan_root(root, errors=report.scan_errors)
    # 目录读不全时，"文件没了" 可能只是看不见：宁可不对账，也绝不删错
    vanished = [] if report.scan_errors else sorted(set(manifest.docs) - set(on_disk))
    if (
        not report.scan_errors
        and manifest.docs
        and len(vanished) > len(manifest.docs) * MASS_DELETE_RATIO
        and not force
    ):
        report.blocked = (
            f"将删除 {len(vanished)}/{len(manifest.docs)} 份已入库文档，"
            "疑似挂载点丢失或路径错误；确认无误请加 --force"
        )
        return report

    if not dry_run and not manifest.docs:
        # manifest 为空但集合非空：多半是旧索引（scripts.build_kb 的随机块 ID）。
        # 没法逐块对账，继续同步只会叠加重复块，所以要求显式重建。
        existing = store.client.count(store.collection_name, exact=True).count
        if existing and not force:
            report.blocked = (
                f"集合里有 {existing} 个块，但没有 manifest（旧索引或 manifest 丢失）；"
                "继续会叠加重复块。确认重建请加 --force（先清空该集合再全量入库）"
            )
            return report
        if existing:
            removed = _drop_all(store)
            report.warnings.append(f"已清空 {removed} 个无 manifest 可对账的旧块，按源文件重建")

    _upgrade_legacy_entries(manifest, report, dry_run)

    # 扫描不完整时 vanished 里的条目会原样保留，它们的 doc_id 也必须留着占坑，
    # 否则可读目录里的新文档能把同一个 id 抢走，manifest 里出现两个同 id 文档
    retained = manifest.docs.keys() if report.scan_errors else on_disk.keys()
    claimed = {d["meta"]["doc_id"]: src for src, d in manifest.docs.items() if src in retained}
    for source, path in on_disk.items():
        old = manifest.docs.get(source)
        try:
            digest = inputs_digest(root, path, file_sha256(path))
        except OSError as e:
            # 单份读不了不影响其余文档；旧块与 manifest 原样保留，下次再试
            report.errors[source] = f"读取失败: {e.strerror or e}"
            continue
        if old and old["sha256"] == digest:
            report.unchanged.append(source)
            continue
        try:
            meta, text = _load(root, path, source)
            owner = claimed.get(meta.doc_id)
            if owner is not None and owner != source:
                raise MetaError(f"doc_id {meta.doc_id!r} 已被 {owner} 使用")
        except (ParseError, MetaError) as e:
            # 读不出或元数据非法：旧块原样保留（宁可旧，不可空/不可错放行），新文件不入库
            report.errors[source] = str(e)
            continue
        if old and old["meta"]["doc_id"] != meta.doc_id:
            # 换了 doc_id：旧 id 的占坑要释放，否则同批次里复用该 id 的文档会被误拒
            claimed.pop(old["meta"]["doc_id"], None)
        claimed[meta.doc_id] = source
        (report.changed if old else report.added).append(source)
        if dry_run:
            # 预演也要用"拟入库"的元数据算草稿清单与取代关系，否则预览报不出元数据问题
            manifest.docs[source] = {
                "sha256": digest,
                "chunk_ids": old["chunk_ids"] if old else [],
                "indexed_at": "",
                "meta": meta.to_dict(),
                "superseded_on": NEVER,
            }
            continue
        base = Document(
            page_content=text,
            metadata={
                "source": source,
                "category": category,
                "subcategory": path.parent.name,
                "doc_sha": digest,
                "superseded_on": NEVER,
                **meta.payload(),
            },
        )
        chunks = chunk_documents([base], chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        ids = _chunk_ids(source, digest, len(chunks))
        now = datetime.now(UTC).isoformat(timespec="seconds")
        for i, c in enumerate(chunks):
            c.metadata["chunk_index"] = i
            c.metadata["indexed_at"] = now
        store.add_documents(chunks, ids=ids)  # 先写新块，再删旧块：中途失败只会多不会缺
        _delete_source(store, source, keep_sha=digest)  # 按 source 清旧版，中断残留下次也能清
        manifest.docs[source] = {
            "sha256": digest, "chunk_ids": ids, "indexed_at": now,
            "meta": meta.to_dict(), "superseded_on": NEVER,
        }
        manifest.save()

    for source in vanished:
        report.deleted.append(source)
        if dry_run:
            # 在副本上去掉：后面的取代关系告警、待批准清单要按"同步后的语料"算，
            # 否则已删文档还会占着 doc_id、挂在待批准清单里
            del manifest.docs[source]
            continue
        _delete_source(store, source)
        del manifest.docs[source]
        manifest.save()

    _refresh_superseded(store, manifest, report, dry_run)
    report.drafts = sorted(
        src for src, d in manifest.docs.items() if d["meta"]["status"] != "approved"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    from app.config import get_settings
    from app.rag.store import get_embeddings, get_store

    s = get_settings()
    p = argparse.ArgumentParser(description="增量同步一个目录到知识库")
    p.add_argument("--track", choices=["industry", "enterprise"], required=True)
    p.add_argument("--root", type=Path, help="源目录；缺省用该 track 的配置路径")
    p.add_argument("--dry-run", action="store_true", help="只对账不写入（不加载模型、不碰向量库）")
    p.add_argument("--force", action="store_true", help="放行大批量删除，或重建无 manifest 的旧索引")
    args = p.parse_args(argv)

    track: Track = args.track
    root = args.root or (s.industry_kb_path if track == "industry" else s.enterprise_kb_path)
    manifest = Manifest(s.qdrant_path / f"manifest-{track}.json")
    store = None if args.dry_run else get_store(track, get_embeddings())
    report = sync_directory(root, track, store, manifest, dry_run=args.dry_run, force=args.force)
    print(report.summary())
    for src, err in report.errors.items():
        print(f"  ✗ {src}: {err}", file=sys.stderr)
    for src in report.drafts:
        print(f"  · 待批准: {src}")
    return 1 if report.blocked or report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
