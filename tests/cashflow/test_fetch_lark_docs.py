"""fetch_lark_docs 的纯逻辑测试：文件名安全化 / URL 拼接 / 搜索面扫的 offset 上限。"""
import csv
import sys

import fetch_lark_docs
import pytest


def test_safe_name_sanitizes_illegal_chars_and_appends_token():
    assert fetch_lark_docs.safe_name("a/b:c", "TOKEN1234567") == "a_b_c__TOKEN123"


def test_safe_name_blank_falls_back_and_truncates():
    assert fetch_lark_docs.safe_name("   ", "tok").startswith("未命名__")
    assert fetch_lark_docs.safe_name("x" * 200, "tok", limit=10) == "xxxxxxxxxx__tok"


def test_doc_url_respects_web_base_for_desensitization(monkeypatch):
    monkeypatch.setattr(fetch_lark_docs, "WEB", "")
    assert fetch_lark_docs.doc_url("sheet", "T1") == ""
    assert fetch_lark_docs.doc_url("sheet", "") == ""

    monkeypatch.setattr(fetch_lark_docs, "WEB", "https://example.larksuite.com")
    assert fetch_lark_docs.doc_url("sheet", "T1") == "https://example.larksuite.com/sheets/T1"
    assert fetch_lark_docs.doc_url("wiki", "N1") == "https://example.larksuite.com/wiki/N1"


def test_sweep_search_stops_at_offset_cap(monkeypatch):
    """2026-09-27 实测：搜索接口 offset=180 起报 10001，故每词最多取到 offset=150（180 条）。"""
    calls: list[int] = []

    def fake_post(path: str, token: str, payload: dict) -> dict:
        calls.append(payload["offset"])
        ents = [{"docs_token": f"t{payload['offset']}_{i}", "docs_type": "docx", "title": "x"}
                for i in range(30)]
        return {"code": 0, "data": {"docs_entities": ents}}

    monkeypatch.setattr(fetch_lark_docs, "post", fake_post)
    monkeypatch.setattr(fetch_lark_docs.time, "sleep", lambda *_: None)

    rows: list[dict] = []
    problems: list[str] = []
    fetch_lark_docs.sweep_search("tok", rows, problems, keys=["k"])

    assert calls == [0, 30, 60, 90, 120, 150]
    assert len(rows) == 180
    assert problems == []


def test_sweep_search_dedupes_against_existing_rows(monkeypatch):
    def fake_post(path: str, token: str, payload: dict) -> dict:
        return {"code": 0, "data": {"docs_entities": [
            {"docs_token": "dup", "docs_type": "docx", "title": "x"},
            {"docs_token": "new", "docs_type": "sheet", "title": "y"},
        ]}}

    monkeypatch.setattr(fetch_lark_docs, "post", fake_post)
    monkeypatch.setattr(fetch_lark_docs.time, "sleep", lambda *_: None)

    rows = [{"token": "dup"}]
    problems: list[str] = []
    fetch_lark_docs.sweep_search("tok", rows, problems, keys=["k"])

    assert [r["token"] for r in rows] == ["dup", "new"]
    assert problems == []


def test_list_wiki_follows_pages_after_empty_page(monkeypatch):
    pages = {
        None: {"items": [], "has_more": True, "page_token": "page2"},
        "page2": {"items": [{"space_id": "s1", "name": "First"}],
                  "has_more": True, "page_token": "page3"},
        "page3": {"items": [{"space_id": "s2", "name": "Second"}], "has_more": False},
    }
    calls = []
    walked = []

    def fake_get(path, token, **kwargs):
        assert path == "/wiki/v2/spaces"
        params = kwargs["params"]
        calls.append(params.copy())
        return {"code": 0, "data": pages[params.get("page_token")]}

    def fake_nodes(token, space_id, parent, path, rows, problems, depth):
        walked.append(space_id)

    monkeypatch.setattr(fetch_lark_docs, "get", fake_get)
    monkeypatch.setattr(fetch_lark_docs, "_wiki_nodes", fake_nodes)
    monkeypatch.setattr(fetch_lark_docs.time, "sleep", lambda *_: None)
    rows, problems = [], []

    fetch_lark_docs.list_wiki("tok", rows, problems)

    assert [params.get("page_token") for params in calls] == [None, "page2", "page3"]
    assert [row["token"] for row in rows] == ["s1", "s2"]
    assert walked == ["s1", "s2"]
    assert problems == []


@pytest.mark.parametrize("partial", [False, True], ids=["all_sources_failed", "partial_results"])
def test_main_failed_rerun_preserves_complete_inventory(monkeypatch, tmp_dir, partial):
    complete = tmp_dir / "cloud-docs-inventory-20261029.csv"
    previous = b"previous successful inventory\noriginal row\n"
    complete.write_bytes(previous)
    rows = [{"类型": "文档", "token": "doc1", "名称": "partial"}] if partial else []
    monkeypatch.setattr(sys, "argv", ["fetch_lark_docs.py", "--out", str(tmp_dir)])
    monkeypatch.setattr(fetch_lark_docs.fl, "load_token", lambda: "tok")
    monkeypatch.setattr(fetch_lark_docs, "collect", lambda *a, **kw: (rows, ["API unauthorized"]))
    monkeypatch.setattr(fetch_lark_docs.time, "strftime", lambda _: "20261029")

    with pytest.raises(SystemExit) as exc:
        fetch_lark_docs.main()

    assert exc.value.code == 1
    assert complete.read_bytes() == previous
    incomplete = tmp_dir / "cloud-docs-inventory-20261029-incomplete.csv"
    with incomplete.open(encoding="utf-8-sig", newline="") as fh:
        saved = list(csv.DictReader(fh))
    assert [row["token"] for row in saved] == (["doc1"] if partial else [])


def test_main_success_writes_complete_inventory(monkeypatch, tmp_dir):
    rows = [{"类型": "文档", "token": "doc1", "名称": "complete"}]
    monkeypatch.setattr(sys, "argv", ["fetch_lark_docs.py", "--out", str(tmp_dir)])
    monkeypatch.setattr(fetch_lark_docs.fl, "load_token", lambda: "tok")
    monkeypatch.setattr(fetch_lark_docs, "collect", lambda *a, **kw: (rows, []))
    monkeypatch.setattr(fetch_lark_docs.time, "strftime", lambda _: "20261029")

    assert fetch_lark_docs.main() is None

    complete = tmp_dir / "cloud-docs-inventory-20261029.csv"
    with complete.open(encoding="utf-8-sig", newline="") as fh:
        saved = list(csv.DictReader(fh))
    assert [row["token"] for row in saved] == ["doc1"]
    assert not (tmp_dir / "cloud-docs-inventory-20261029-incomplete.csv").exists()
