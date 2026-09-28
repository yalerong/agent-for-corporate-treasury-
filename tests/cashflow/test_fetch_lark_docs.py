"""fetch_lark_docs 的纯逻辑测试：文件名安全化 / URL 拼接 / 搜索面扫的 offset 上限。"""
import fetch_lark_docs


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
