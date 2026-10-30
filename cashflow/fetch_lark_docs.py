"""云文档盘点（只出清单，不拉内容）· 用户身份 = 当前授权用户可见范围。

三条路合并去重：
  1) 知识库(wiki)：/wiki/v2/spaces → 逐空间递归 /wiki/v2/spaces/{id}/nodes
  2) 云空间(drive)：/drive/explorer/v2/root_folder/meta → 递归 /drive/v1/files?folder_token=
  3) 搜索面扫（默认开）：/suite/docs-api/search/object 按业务关键词翻页，捞「我的空间」之外的共享文档
     —— 文件夹接口覆盖不到共享空间，Lark 也没有「列共享文件夹」的接口，搜索是唯一能扫到共享件的路。

2026-09-27 实测坑：
  * 搜索接口 offset 上限 ≈150（offset=180 起报 10001 invalid param）→ 每个关键词最多取 180 条；
  * 云空间文件列表 / 搜索 / 知识库都必须在授权链接上显式申请 scope（见 fetch_lark.py 的
    LARK_OAUTH_SCOPE），否则即使应用后台显示"已开通"，接口也报 99991679 Unauthorized；
  * 输出较多时 Windows 控制台会被块缓冲，本脚本对每条进度 print(flush=True)。

产物：<out>/cloud-docs-inventory-<YYYYMMDD>.csv（来源,路径,名称,类型,token,URL,最后修改）
有未完成部分时另存为 -incomplete.csv 并以状态码 1 退出，保留当天已有的完整清单。
只读：不下载正文、不改权限、不分享、不发版。

环境变量（.env）：
    LARK_WEB_BASE   可选：文档 URL 前缀（如 https://<租户>.larksuite.com）；不填则 URL 列留空（脱敏）
    LARK_OAUTH_SCOPE  见 fetch_lark.py；本脚本建议至少
                      "drive:drive:readonly search:docs:read wiki:wiki:readonly"

用法：
    cd cashflow && python fetch_lark_docs.py                  # 三路全扫，落 CASHFLOW_ROOT/data/cloud-docs/
    cd cashflow && python fetch_lark_docs.py --out D:/dump    # 指定输出目录
    cd cashflow && python fetch_lark_docs.py --no-search      # 只走 wiki + 云空间文件夹树
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import time
from pathlib import Path

import fetch_lark as fl  # 平铺脚本目录：cd cashflow 直跑时同目录可导入
import requests

BASE = fl.BASE
WEB = os.environ.get("LARK_WEB_BASE", "").rstrip("/")
MAX_DEPTH = 12
SEARCH_OFFSET_CAP = 150  # 实测上限：offset=180 起 10001 invalid param

TYPE_ZH = {"docx": "文档", "doc": "旧文档", "sheet": "表格", "bitable": "多维表格",
           "mindnote": "思维笔记", "slides": "幻灯片", "file": "文件", "folder": "文件夹",
           "wiki": "知识库节点", "shortcut": "快捷方式"}
URL_SEG = {"docx": "docx", "doc": "docs", "sheet": "sheets", "bitable": "base",
           "mindnote": "mindnotes", "slides": "slides", "file": "file",
           "folder": "drive/folder", "wiki": "wiki"}

SEARCH_KEYS = [
    "资金", "账户", "合同", "账单", "日记账", "审批", "预算", "税", "银行", "美元",
    "比索", "印尼", "墨西哥", "付款", "调拨", "理财", "余额", "流水", "对账", "报销",
    "工资", "租金", "借款", "投资", "基金", "保险", "人民币", "港币", "新加坡", "秘鲁",
    "智利", "阿根廷", "印度", "菲律宾", "客户", "供应商", "invoice", "2026", "2025",
]


# ---------- 纯函数（可测） ----------
def safe_name(name: str, token: str, limit: int = 80) -> str:
    """文件名安全化：非法字符换下划线、空名兜底、截断、附 token 前 8 位防重名。"""
    base = re.sub(r'[\\/:*?"<>|]', "_", str(name or "").strip())
    return f"{(base or '未命名')[:limit]}__{str(token or '')[:8]}"


def doc_url(obj_type: str, token: str) -> str:
    """按对象类型拼文档 URL；未配置 LARK_WEB_BASE 时返回空串（脱敏：URL 里含租户域名）。"""
    if not WEB or not token:
        return ""
    return f"{WEB}/{URL_SEG.get(obj_type, 'file')}/{token}"


# ---------- HTTP ----------
def get(path: str, token: str, **kw) -> dict:
    r = requests.get(f"{BASE}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=60, **kw)
    try:
        return r.json()
    except ValueError:
        return {"code": -1, "msg": f"非 JSON 响应 {r.status_code}"}


def post(path: str, token: str, payload: dict) -> dict:
    r = requests.post(f"{BASE}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=60, json=payload)
    try:
        return r.json()
    except ValueError:
        return {"code": -1, "msg": f"非 JSON 响应 {r.status_code}"}


# ---------- 1) 知识库 ----------
def list_wiki(tok: str, rows: list[dict], problems: list[str]) -> None:
    page = None
    count = 0
    while True:
        params: dict = {"page_size": 50}
        if page:
            params["page_token"] = page
        d = get("/wiki/v2/spaces", tok, params=params)
        if d.get("code") != 0:
            problems.append(f"wiki 空间列表失败 code={d.get('code')} {str(d.get('msg'))[:40]}")
            return
        data = d.get("data") or {}
        spaces = data.get("items") or []
        count += len(spaces)
        for sp in spaces:
            sid, sname = sp.get("space_id"), sp.get("name")
            rows.append({"来源": "wiki", "路径": sname, "名称": sname, "类型": "知识空间",
                         "token": sid, "URL": doc_url("wiki", sid), "最后修改": ""})
            _wiki_nodes(tok, sid, None, sname, rows, problems, depth=0)
        if not data.get("has_more"):
            print(f"知识空间 {count} 个", flush=True)
            return
        page = data.get("page_token")
        time.sleep(0.15)


def _wiki_nodes(tok: str, space_id: str, parent: str | None, path: str,
                rows: list[dict], problems: list[str], depth: int) -> None:
    if depth > MAX_DEPTH:
        return
    page = None
    while True:
        params: dict = {"page_size": 50}
        if parent:
            params["parent_node_token"] = parent
        if page:
            params["page_token"] = page
        d = get(f"/wiki/v2/spaces/{space_id}/nodes", tok, params=params)
        if d.get("code") != 0:
            problems.append(f"wiki 节点失败 {path} code={d.get('code')} {str(d.get('msg'))[:36]}")
            return
        data = d.get("data") or {}
        for n in data.get("items") or []:
            rows.append({"来源": "wiki", "路径": path, "名称": n.get("title"), "类型": "知识库节点",
                         "token": n.get("node_token"), "URL": doc_url("wiki", n.get("node_token")),
                         "最后修改": ""})
            if n.get("has_child"):
                _wiki_nodes(tok, space_id, n.get("node_token"), f"{path}/{n.get('title')}",
                            rows, problems, depth + 1)
        if not data.get("has_more"):
            return
        page = data.get("page_token")
        time.sleep(0.15)


# ---------- 2) 云空间 ----------
def walk_folder(tok: str, folder_token: str, path: str, rows: list[dict],
                problems: list[str], depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        return
    page = None
    while True:
        params = {"folder_token": folder_token, "page_size": 200,
                  "order_by": "EditedTime", "direction": "DESC"}
        if page:
            params["page_token"] = page
        d = get("/drive/v1/files", tok, params=params)
        if d.get("code") != 0:
            problems.append(f"列目录失败 {path} code={d.get('code')} {str(d.get('msg'))[:36]}")
            return
        data = d.get("data") or {}
        for f in data.get("files") or []:
            ftype, ftoken = f.get("type"), f.get("token")
            rows.append({"来源": "drive", "路径": path, "名称": f.get("name"),
                         "类型": TYPE_ZH.get(ftype, ftype), "token": ftoken,
                         "URL": doc_url(ftype, ftoken), "最后修改": f.get("modified_time") or ""})
            if ftype == "folder":
                walk_folder(tok, ftoken, f"{path}/{f.get('name')}", rows, problems, depth + 1)
        if not data.get("has_more"):
            return
        page = data.get("page_token")
        time.sleep(0.15)


# ---------- 3) 搜索面扫（覆盖共享空间） ----------
def sweep_search(tok: str, rows: list[dict], problems: list[str],
                 keys: list[str] | None = None) -> None:
    seen = {r["token"] for r in rows if r.get("token")}
    added = 0
    for kw in keys or SEARCH_KEYS:
        offset = 0
        while offset <= SEARCH_OFFSET_CAP:
            d = post("/suite/docs-api/search/object", tok,
                     {"search_key": kw, "count": 30, "offset": offset})
            if d.get("code") != 0:
                problems.append(f"搜索「{kw}」失败 code={d.get('code')} {str(d.get('msg'))[:30]}")
                break
            ents = (d.get("data") or {}).get("docs_entities") or []
            for e in ents:
                t = e.get("docs_token")
                if not t or t in seen:
                    continue
                seen.add(t)
                added += 1
                rows.append({"来源": "search", "路径": f"(搜索命中：{kw})", "名称": e.get("title"),
                             "类型": TYPE_ZH.get(e.get("docs_type"), e.get("docs_type")),
                             "token": t, "URL": e.get("url") or doc_url(e.get("docs_type"), t),
                             "最后修改": ""})
            if len(ents) < 30:
                break
            offset += 30
            time.sleep(0.2)
    print(f"搜索面扫新增 {added} 项（已去重）", flush=True)


# ---------- 主流程 ----------
def collect(tok: str, with_search: bool = True) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    problems: list[str] = []
    list_wiki(tok, rows, problems)
    meta = get("/drive/explorer/v2/root_folder/meta", tok)
    if meta.get("code") == 0:
        root = (meta.get("data") or {}).get("token", "")
        walk_folder(tok, root, "云空间/我的空间", rows, problems)
    else:
        problems.append(f"根目录元数据失败 code={meta.get('code')} {str(meta.get('msg'))[:40]}")
    if with_search:
        sweep_search(tok, rows, problems)
    return rows, problems


def main() -> None:
    ap = argparse.ArgumentParser(description="云文档盘点（只出清单，不拉内容）")
    ap.add_argument("--out", default=str(Path(fl.DATA_ROOT) / "cloud-docs"))
    ap.add_argument("--no-search", action="store_true", help="跳过搜索面扫（只走 wiki + 云空间文件夹树）")
    a = ap.parse_args()

    rows, problems = collect(fl.load_token(), with_search=not a.no_search)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    suffix = "-incomplete" if problems else ""
    csv_path = out / f"cloud-docs-inventory-{time.strftime('%Y%m%d')}{suffix}.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["来源", "路径", "名称", "类型", "token", "URL", "最后修改"])
        w.writeheader()
        w.writerows(rows)

    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r["类型"]] = by_type.get(r["类型"], 0) + 1
    print(f"\n清单 → {csv_path}（{len(rows)} 项）")
    print("按类型:", by_type)
    if problems:
        print("\n⚠️ 未完成的部分（多半是权限没开，见 LARK_OAUTH_SCOPE）:")
        for p in sorted(set(problems))[:10]:
            print("  -", p)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
