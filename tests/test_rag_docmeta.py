"""文档元数据：默认从严、三层合并、拼写错误拒收、取代关系与生效期。"""
from __future__ import annotations

from datetime import date

import pytest
from qdrant_client import QdrantClient

from app.rag import get_store
from app.rag.docmeta import (
    NEVER,
    DocMeta,
    MetaError,
    compute_superseded_on,
    resolve_meta,
    split_front_matter,
)
from app.rag.ingest import Manifest, sync_directory
from tests.test_rag import DeterministicFakeEmbeddings


@pytest.fixture
def store():
    return get_store(
        "enterprise",
        DeterministicFakeEmbeddings(),
        client=QdrantClient(location=":memory:"),
        collection_name="t_meta",
    )


@pytest.fixture
def root(tmp_dir):
    r = tmp_dir / "kb"
    r.mkdir()
    return r


def _sync(root, store, tmp_dir, **kw):
    m = Manifest(tmp_dir / "m.json")
    return sync_directory(root, "enterprise", store, m, **kw), m


def _payloads(store) -> dict[str, dict]:
    pts, _ = store.client.scroll(store.collection_name, limit=100, with_payload=True)
    out: dict[str, dict] = {}
    for p in pts:
        md = p.payload["metadata"]
        out[md["source"]] = md
    return out


FM = "---\nstatus: approved\nclassification: internal\n---\n正文。"


class TestFrontMatter:
    def test_split(self):
        fm, body = split_front_matter(FM)
        assert fm == {"status": "approved", "classification": "internal"}
        assert body == "正文。"

    def test_no_front_matter_and_unclosed(self):
        assert split_front_matter("# 标题\n---\n内容") == (None, "# 标题\n---\n内容")
        assert split_front_matter("---\nstatus: approved\n无结束线")[0] is None

    def test_bad_yaml(self):
        with pytest.raises(MetaError):
            split_front_matter("---\na: [unclosed\n---\nx")


class TestResolve:
    def test_defaults_are_strict(self, root):
        f = root / "a.md"
        f.write_text("x", "utf-8")
        m = resolve_meta(root, f, "a.md", None)
        assert (m.status, m.classification, m.doc_id) == ("draft", "restricted", "a.md")

    def test_three_layers_deeper_wins(self, root):
        (root / "p").mkdir()
        (root / "_meta.yaml").write_text("classification: internal\nowner: 财务部\n", "utf-8")
        (root / "p" / "_meta.yaml").write_text("status: approved\nclassification: public\n", "utf-8")
        f = root / "p" / "a.md"
        f.write_text("x", "utf-8")
        m = resolve_meta(root, f, "p/a.md", {"classification": "restricted", "title": "T"})
        assert (m.status, m.classification, m.owner, m.title) == (
            "approved", "restricted", "财务部", "T"
        )

    def test_sidecar_for_binary_formats(self, root):
        f = root / "a.pdf"
        f.write_bytes(b"x")
        (root / "a.pdf.meta.yaml").write_text("status: approved\nentities: A公司, B公司\n", "utf-8")
        m = resolve_meta(root, f, "a.pdf", None)
        assert m.status == "approved" and m.entities == ("A公司", "B公司")

    @pytest.mark.parametrize(
        "yaml_text, hint",
        [
            ("staus: approved\n", "未知"),
            ("status: aproved\n", "取值非法"),
            ("classification: secret\n", "取值非法"),
            ("effective_from: 2026-13-40\n", "日期"),
            ("effective_from: 2026-02-01\neffective_to: 2026-01-01\n", "早于"),
            ("supersedes: [a.md]\n", "自己"),
            ("entities: 5\n", "列表"),
        ],
    )
    def test_invalid_rejected(self, root, yaml_text, hint):
        f = root / "a.md"
        f.write_text("x", "utf-8")
        (root / "a.md.meta.yaml").write_text(yaml_text, "utf-8")
        with pytest.raises(MetaError, match=hint):
            resolve_meta(root, f, "a.md", None)

    def test_dir_level_rejects_doc_only_fields(self, root):
        (root / "_meta.yaml").write_text("doc_id: x\n", "utf-8")
        f = root / "a.md"
        f.write_text("x", "utf-8")
        with pytest.raises(MetaError, match="目录级"):
            resolve_meta(root, f, "a.md", None)

    def test_front_matter_and_sidecar_conflict(self, root):
        f = root / "a.md"
        f.write_text("x", "utf-8")
        (root / "a.md.meta.yaml").write_text("status: approved\n", "utf-8")
        with pytest.raises(MetaError, match="同时存在"):
            resolve_meta(root, f, "a.md", {"status": "approved"})


def _d(doc_id, *, status="approved", eff_from=None, supersedes=()):
    return DocMeta(
        doc_id=doc_id, status=status,
        effective_from=date.fromisoformat(eff_from) if eff_from else None,
        supersedes=tuple(supersedes),
    ).to_dict()


class TestSupersession:
    def test_basic_and_effective_date(self):
        docs = {"old.md": _d("old"), "new.md": _d("new", eff_from="2026-10-01", supersedes=["old"])}
        res, warn = compute_superseded_on(docs)
        assert res == {"old.md": 20261001, "new.md": NEVER} and not warn

    def test_draft_superseder_does_not_count(self):
        docs = {"old.md": _d("old"), "new.md": _d("new", status="draft", supersedes=["old"])}
        assert compute_superseded_on(docs)[0]["old.md"] == NEVER

    def test_earliest_of_multiple(self):
        docs = {
            "old.md": _d("old"),
            "n1.md": _d("n1", eff_from="2026-12-01", supersedes=["old"]),
            "n2.md": _d("n2", eff_from="2026-11-01", supersedes=["old"]),
        }
        assert compute_superseded_on(docs)[0]["old.md"] == 20261101

    def test_unknown_ref_and_mutual_cycle_warn(self):
        res, warn = compute_superseded_on({"a.md": _d("a", supersedes=["ghost"])})
        assert any("ghost" in w for w in warn) and res["a.md"] == NEVER
        res, warn = compute_superseded_on(
            {"a.md": _d("a", supersedes=["b"]), "b.md": _d("b", supersedes=["a"])}
        )
        assert res == {"a.md": NEVER, "b.md": NEVER} and any("互相" in w for w in warn)


class TestSyncWithMeta:
    def test_front_matter_stripped_and_payload_written(self, root, store, tmp_dir):
        (root / "a.md").write_text(FM, "utf-8")
        r, m = _sync(root, store, tmp_dir)
        assert not r.errors and r.drafts == []
        md = _payloads(store)["a.md"]
        assert md["status"] == "approved" and md["classification"] == "internal"
        assert md["superseded_on"] == NEVER and md["eff_from"] == 0 and md["eff_to"] == NEVER
        texts = [h.page_content for h in store.similarity_search("正文", k=5)]
        assert texts == ["正文。"]  # front matter 没进索引

    def test_unlabelled_doc_indexed_as_draft_restricted(self, root, store, tmp_dir):
        (root / "a.md").write_text("无元数据。", "utf-8")
        r, _ = _sync(root, store, tmp_dir)
        md = _payloads(store)["a.md"]
        assert (md["status"], md["classification"]) == ("draft", "restricted")
        assert r.drafts == ["a.md"] and "尚未批准" in r.summary()

    def test_bad_meta_rejected_and_old_chunks_kept(self, root, store, tmp_dir):
        (root / "a.md").write_text(FM, "utf-8")
        _sync(root, store, tmp_dir)
        (root / "a.md").write_text("---\nstaus: approved\n---\n新正文。", "utf-8")
        r, m = _sync(root, store, tmp_dir)
        assert "a.md" in r.errors and "未知" in r.errors["a.md"]
        assert _payloads(store)["a.md"]["status"] == "approved"  # 旧版本仍在
        assert [h.page_content for h in store.similarity_search("正文", k=5)] == ["正文。"]

    def test_sidecar_change_alone_triggers_reindex(self, root, store, tmp_dir):
        (root / "a.md").write_text("内容。", "utf-8")
        _sync(root, store, tmp_dir)
        assert _payloads(store)["a.md"]["status"] == "draft"
        (root / "_meta.yaml").write_text("status: approved\n", "utf-8")
        r, _ = _sync(root, store, tmp_dir)
        assert r.changed == ["a.md"] and _payloads(store)["a.md"]["status"] == "approved"

    def test_supersession_updates_old_doc_payload_and_reverts(self, root, store, tmp_dir):
        (root / "old.md").write_text("---\nstatus: approved\ndoc_id: r1\n---\n旧裁决。", "utf-8")
        _sync(root, store, tmp_dir)
        assert _payloads(store)["old.md"]["superseded_on"] == NEVER
        new = root / "new.md"
        new.write_text(
            "---\nstatus: approved\ndoc_id: r2\nsupersedes: [r1]\neffective_from: 2026-10-01\n"
            "---\n新裁决。",
            "utf-8",
        )
        _sync(root, store, tmp_dir)
        assert _payloads(store)["old.md"]["superseded_on"] == 20261001  # 旧文档本身没重写
        new.unlink()
        _sync(root, store, tmp_dir, force=True)
        assert _payloads(store)["old.md"]["superseded_on"] == NEVER  # 取代者没了，恢复现行

    def test_duplicate_doc_id_second_rejected(self, root, store, tmp_dir):
        (root / "a.md").write_text("---\ndoc_id: x\n---\n甲。", "utf-8")
        (root / "b.md").write_text("---\ndoc_id: x\n---\n乙。", "utf-8")
        r, m = _sync(root, store, tmp_dir)
        assert "a.md" in m.docs and "b.md" not in m.docs and "已被" in r.errors["b.md"]

    def test_doc_id_change_releases_old_reservation(self, root, store, tmp_dir):
        (root / "a.md").write_text("---\nstatus: approved\ndoc_id: x\n---\n甲。", "utf-8")
        _sync(root, store, tmp_dir)
        # a.md 改用新 id，空出来的 x 同批次就该能给 b.md 用
        (root / "a.md").write_text("---\nstatus: approved\ndoc_id: y\n---\n甲改。", "utf-8")
        (root / "b.md").write_text("---\nstatus: approved\ndoc_id: x\n---\n乙。", "utf-8")
        r, m = _sync(root, store, tmp_dir)
        assert not r.errors and m.docs["b.md"]["meta"]["doc_id"] == "x"
        assert m.docs["a.md"]["meta"]["doc_id"] == "y"

    def test_non_utf8_sidecar_is_per_document_error(self, root, store, tmp_dir):
        (root / "a.md").write_text("内容。", "utf-8")
        (root / "a.md.meta.yaml").write_bytes("status: 已批准".encode("gb18030"))
        (root / "b.md").write_text("内容。", "utf-8")
        r, m = _sync(root, store, tmp_dir)
        assert "读取失败" in r.errors["a.md"]
        assert "b.md" in m.docs  # 一份坏掉不中断整批
