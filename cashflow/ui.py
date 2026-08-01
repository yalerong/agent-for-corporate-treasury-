"""cashflow 本地审批与报告界面（单文件 FastAPI，零新增依赖）。

启动: python ui.py [--port 8787]  →  http://127.0.0.1:8787
页面: /  最新报告   /patterns  规律审批（过滤/勾选/批准/否决）   /forecast  预测明细

只监听本机回环地址（本地工具，无鉴权设计，勿绑 0.0.0.0）。
写操作与 approve.py 共用同一套函数（apply_approve/apply_refute），写盘前自动 .bak；
approve --all 同款纪律：批量批准只动 candidate，否决必须给理由，refuted 重算不复活。
"""
import argparse
import html
import json
import re

import pandas as pd
import pattern_store as ps
from approve import apply_approve, apply_refute, pick_by_ids
from constants import get_root
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI(title="Treasury Agent 审批台")


# ---------- 路径（每次请求现算，测试可用 CASHFLOW_ROOT 注入） ----------

def pat_path():
    return get_root() / "patterns" / "patterns.yaml"


def runs_dir():
    return get_root() / "runs"


def latest_run():
    d = runs_dir()
    if not d.exists():
        return None
    runs = sorted((p for p in d.iterdir() if p.is_dir()), reverse=True)
    return runs[0] if runs else None


def load_doc() -> dict:
    p = pat_path()
    if not p.exists():
        raise HTTPException(404, "patterns.yaml 不存在，先跑 patterns.py")
    doc = ps.load(p)
    if doc["meta"].get("schema_version", 1) < ps.SCHEMA_VERSION:
        raise HTTPException(409, "patterns.yaml 还是 schema v1，先跑 migrate_patterns.py")
    return doc


# ---------- 报告 markdown → HTML（只针对本仓库报告的固定子集，确定性渲染） ----------

def _inline(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"&lt;!--.*?--&gt;", "", s)  # metric 注释不进画面
    return s


def md_to_html(md: str) -> str:
    out, i, lines = [], 0, md.splitlines()
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip("|").split("|")])
                i += 1
            body = ["<table>"]
            for r_i, cells in enumerate(rows):
                if r_i == 1 and all(re.fullmatch(r":?-+:?", c or "-") for c in cells):
                    continue
                tag = "th" if r_i == 0 else "td"
                body.append("<tr>" + "".join(f"<{tag}>{_inline(c)}</{tag}>" for c in cells) + "</tr>")
            out.append("".join(body) + "</table>")
            continue
        if ln.startswith("- ") or ln.startswith("  - "):
            out.append("<ul>")
            while i < len(lines) and (lines[i].startswith("- ") or lines[i].startswith("  - ")):
                cls = ' class="sub"' if lines[i].startswith("  ") else ""
                out.append(f"<li{cls}>{_inline(lines[i].lstrip().removeprefix('- '))}</li>")
                i += 1
            out.append("</ul>")
            continue
        if ln.startswith("## "):
            out.append(f"<h2>{_inline(ln[3:])}</h2>")
        elif ln.startswith("# "):
            out.append(f"<h1>{_inline(ln[2:])}</h1>")
        elif ln.startswith("> "):
            out.append(f"<blockquote>{_inline(ln[2:])}</blockquote>")
        elif ln.strip() == "---":
            out.append("<hr>")
        elif ln.strip():
            out.append(f"<p>{_inline(ln)}</p>")
        i += 1
    return "\n".join(out)


# ---------- 页面骨架 ----------

CSS = """
body{font-family:system-ui,'Microsoft YaHei',sans-serif;margin:0;background:#f6f7f9;color:#1c2733}
nav{background:#12314f;color:#fff;padding:10px 24px;display:flex;gap:18px;align-items:center}
nav a{color:#cfe0f2;text-decoration:none;font-size:15px}nav a.on{color:#fff;font-weight:700}
nav .brand{font-weight:700;margin-right:8px}
main{max-width:1100px;margin:18px auto;padding:0 16px 60px}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px;margin:10px 0}
th,td{border:1px solid #dde3ea;padding:5px 8px;text-align:left;vertical-align:top}
th{background:#eef2f7}tr:nth-child(even) td{background:#fafbfd}
h1{font-size:22px}h2{font-size:17px;border-left:4px solid #12314f;padding-left:8px;margin-top:26px}
blockquote{border-left:3px solid #c8d3e0;margin:8px 0;padding:4px 10px;color:#4a5a6a;background:#fff}
li.sub{margin-left:22px;list-style:circle}
code{background:#eef2f7;padding:1px 4px;border-radius:3px;font-size:12px}
.badge{padding:1px 8px;border-radius:9px;font-size:12px;white-space:nowrap}
.b-candidate{background:#e8edf3;color:#41546b}.b-approved{background:#d9f2df;color:#186b34}
.b-refuted{background:#fbdcdc;color:#a11f1f}.b-high{background:#fff3d6;color:#8a6100}
.b-provisional{background:#f0f0f0;color:#777}
.bar{background:#fff;border:1px solid #dde3ea;padding:10px 12px;margin:12px 0;border-radius:6px;
     display:flex;gap:10px;flex-wrap:wrap;align-items:center;font-size:14px}
.bar input,.bar select{padding:4px 6px;border:1px solid #c4cdd8;border-radius:4px;font-size:13px}
button{background:#12314f;color:#fff;border:0;padding:6px 14px;border-radius:4px;cursor:pointer}
button.danger{background:#a11f1f}
.msg{background:#d9f2df;border:1px solid #9fd8ae;padding:8px 12px;border-radius:6px;margin:10px 0}
.err{background:#fbdcdc;border-color:#e3a1a1}
.muted{color:#7a8794;font-size:12px}
"""


def layout(title: str, active: str, body: str, msg: str = "") -> str:
    tabs = [("/", "报告", "report"), ("/patterns", "规律审批", "patterns"),
            ("/forecast", "预测明细", "forecast")]
    nav = "".join(f'<a href="{u}" class="{"on" if k == active else ""}">{t}</a>'
                  for u, t, k in tabs)
    banner = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>"
            f"<style>{CSS}</style></head><body>"
            f"<nav><span class='brand'>💰 Treasury Agent</span>{nav}</nav>"
            f"<main>{banner}{body}</main></body></html>")


# ---------- 报告页 ----------

@app.get("/", response_class=HTMLResponse)
def report_page(run: str = ""):
    d = runs_dir() / run if run else latest_run()
    if d is None or not (d / "report.md").exists():
        return layout("报告", "report", "<p>还没有报告，先跑 engine.py。</p>")
    options = "".join(
        f'<option value="{p.name}" {"selected" if p.name == d.name else ""}>{p.name}</option>'
        for p in sorted((x for x in runs_dir().iterdir() if x.is_dir()), reverse=True))
    picker = (f'<div class="bar"><form method="get">报告日期 <select name="run" '
              f'onchange="this.form.submit()">{options}</select></form>'
              f'<span class="muted">runs/{d.name}/report.md · lineage.json 同目录</span></div>')
    return layout("报告", "report",
                  picker + md_to_html((d / "report.md").read_text(encoding="utf-8")))


# ---------- 规律审批页 ----------

def _pat_row(p: dict) -> str:
    ev = p.get("evidence") or {}
    ev_txt = (f"hit {ev['hits']}/miss {ev['misses']} (率 {ev['hit_rate']})"
              if ev.get("last_validated") else "—")
    demoted = f"<div class='muted'>降级: {html.escape(str(p.get('demoted_reason', '')))}</div>" \
        if p.get("demoted_at") else ""
    refuted = f"<div class='muted'>{html.escape(str(p.get('refuted_reason', '')))}</div>" \
        if p.get("refuted_reason") else ""
    key = ", ".join(f"{v}" for v in p["key"].values())
    return (f"<tr><td><input type='checkbox' name='ids' value='{p['id']}'></td>"
            f"<td><code>{p['id']}</code></td>"
            f"<td><span class='badge b-{p['status']}'>{p['status']}</span></td>"
            f"<td><span class='badge b-{p['confidence']}'>{p['confidence']}</span></td>"
            f"<td>{html.escape(p['type'])}</td><td>{html.escape(key)}</td>"
            f"<td>{html.escape(p['claim'])}{demoted}{refuted}</td><td>{ev_txt}</td></tr>")


@app.get("/patterns", response_class=HTMLResponse)
def patterns_page(status: str = "", confidence: str = "", type_: str = "", q: str = "",
                  msg: str = ""):
    doc = load_doc()
    pats = [p for p in doc["patterns"]
            if (not status or p["status"] == status)
            and (not confidence or p["confidence"] == confidence)
            and (not type_ or p["type"] == type_)
            and (not q or q.lower() in json.dumps(p, ensure_ascii=False).lower())]
    counts = {}
    for p in doc["patterns"]:
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    summary = " · ".join(f"{k} {v}" for k, v in sorted(counts.items()))

    def sel(name, cur, opts):
        o = "".join(f'<option value="{v}" {"selected" if v == cur else ""}>{v or "全部"}</option>'
                    for v in opts)
        return f"<select name='{name}' onchange='this.form.submit()'>{o}</select>"

    filters = (f"<div class='bar'><form method='get'>状态 {sel('status', status, ['', *ps.STATUSES])} "
               f"置信度 {sel('confidence', confidence, ['', *ps.CONFIDENCES])} "
               f"类型 {sel('type_', type_, ['', 'weekly_level', 'recurring', 'dom_profile', 'llm_insight'])} "
               f"搜索 <input name='q' value='{html.escape(q, quote=True)}' placeholder='payee/主体...'>"
               f"<button>过滤</button></form>"
               f"<span class='muted'>库存: {summary} · 当前显示 {len(pats)} 条</span></div>")
    rows = "".join(_pat_row(p) for p in pats)
    hidden = "".join(
        f"<input type='hidden' name='f_{k}' value='{html.escape(v, quote=True)}'>"
        for k, v in (("status", status), ("confidence", confidence), ("type_", type_), ("q", q)))
    table = (f"<form method='post' action='/patterns/action'>{hidden}"
             f"<div class='bar'>"
             f"<label><input type='checkbox' onclick=\"document.querySelectorAll('input[name=ids]')"
             f".forEach(c=>c.checked=this.checked)\"> 全选本页</label>"
             f"操作人 <input name='by' value='yale' size='8'>"
             f"<button name='action' value='approve'>✔ 批准选中</button>"
             f"否决理由 <input name='reason' size='24' placeholder='否决时必填'>"
             f"<button name='action' value='refute' class='danger'>✘ 否决选中</button>"
             f"<span class='muted'>写盘前自动备份 .bak；refuted 重算不复活</span></div>"
             f"<table><tr><th></th><th>id</th><th>状态</th><th>置信度</th><th>类型</th>"
             f"<th>key</th><th>claim</th><th>核验</th></tr>{rows}</table></form>")
    return layout("规律审批", "patterns", filters + table, msg)


@app.post("/patterns/action")
def patterns_action(action: str = Form(...), by: str = Form(...),
                    reason: str = Form(""), ids: list[str] = Form([]),
                    f_status: str = Form(""), f_confidence: str = Form(""),
                    f_type_: str = Form(""), f_q: str = Form("")):
    back = (f"/patterns?status={f_status}&confidence={f_confidence}"
            f"&type_={f_type_}&q={f_q}&msg=")
    if not ids:
        return RedirectResponse(back + "没有勾选任何规律", status_code=303)
    if not by.strip():
        return RedirectResponse(back + "操作人不能为空", status_code=303)
    doc = load_doc()
    try:
        targets = pick_by_ids(doc, ids)
    except SystemExit as e:
        raise HTTPException(400, str(e)) from e
    if action == "approve":
        apply_approve(targets, by.strip())
        note = f"已批准 {len(targets)} 条（by {by.strip()}）"
    elif action == "refute":
        if not reason.strip():
            return RedirectResponse(back + "否决必须填写理由（refuted 只能人为）", status_code=303)
        apply_refute(targets, reason.strip(), by.strip())
        note = f"已否决 {len(targets)} 条"
    else:
        raise HTTPException(400, f"未知操作 {action}")
    ps.save(pat_path(), doc, backup=True)
    return RedirectResponse(back + note, status_code=303)


# ---------- 预测明细页 ----------

@app.get("/forecast", response_class=HTMLResponse)
def forecast_page(review: str = "", currency: str = ""):
    d = latest_run()
    if d is None or not (d / "forecast.csv").exists():
        return layout("预测明细", "forecast", "<p>还没有 forecast.csv，先跑 engine.py。</p>")
    fc_all = pd.read_csv(d / "forecast.csv", encoding="utf-8-sig")
    fc = fc_all
    if review and "review" in fc.columns:
        fc = fc[fc["review"] == review]
    if currency:
        fc = fc[fc["currency"] == currency]
    cur_opts = "".join(f'<option {"selected" if c == currency else ""}>{c}</option>'
                       for c in ["", *sorted(fc_all["currency"].unique())])
    rev_opts = "".join(f'<option {"selected" if r == review else ""}>{r}</option>'
                       for r in ["", "auto_report", "flag_review", "require_human"])
    bar = (f"<div class='bar'><form method='get'>人审档 <select name='review' "
           f"onchange='this.form.submit()'>{rev_opts}</select> 币种 <select name='currency' "
           f"onchange='this.form.submit()'>{cur_opts}</select></form>"
           f"<span class='muted'>runs/{d.name}/forecast.csv · {len(fc)} 行</span></div>")
    return layout("预测明细", "forecast",
                  bar + fc.to_html(index=False, border=0, classes="fc", justify="left"))


def main():
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    print(f"审批台 → http://127.0.0.1:{args.port}  （只监听本机）")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
