"""cashflow 本地审批台（单文件 FastAPI，零新增依赖）。

启动: python ui.py [--port 8787]  →  http://127.0.0.1:8787
页面: /  报告   /patterns  规律审批（过滤/勾选/批准/否决）   /forecast  预测明细

只监听本机回环地址（本地工具，无鉴权设计，勿绑 0.0.0.0）。
写操作与 approve.py 共用同一套函数（apply_approve/apply_refute），写盘前自动 .bak；
批量批准只动 candidate、否决必须给理由、refuted 重算不复活等纪律不变。
设计语言对齐 yalerong/factor-analysis-demo（暖纸色+深侧栏+珊瑚强调+等宽数字）。
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


# ---------- 设计系统（对齐 factor-analysis-demo） ----------

CSS = """
:root{
  --bg:#EFECE3; --surface:#FAF8F2; --surface-2:#F3F0E6;
  --side-bg:#2B2823; --side-line:rgba(255,255,255,.09);
  --line:#E2DECF; --line-soft:#EBE7DA; --line-strong:#D2CCBA;
  --text:#2A271F; --muted:#6E6855; --subtle:#98917E;
  --coral:#C15F3C; --coral-bright:#DC8159; --coral-soft:rgba(193,95,60,.10);
  --green:#3F7D55; --green-soft:rgba(63,125,85,.12);
  --red:#BE4A3A; --red-soft:rgba(190,74,58,.10);
  --amber:#B7842F; --amber-soft:rgba(183,132,47,.12);
  --slate:#5E7287; --slate-soft:rgba(94,114,135,.12);
  --mono:ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace;
  --sans:Inter,ui-sans-serif,system-ui,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:13px;
  background-image:linear-gradient(var(--line-soft) 1px,transparent 1px),
                   linear-gradient(90deg,var(--line-soft) 1px,transparent 1px);
  background-size:48px 48px;background-position:-1px -1px}
::selection{background:rgba(193,95,60,.20)}
.app{min-height:100vh;display:grid;grid-template-columns:222px minmax(0,1fr)}
.sidebar{position:sticky;top:0;height:100vh;background:var(--side-bg);
  border-right:1px solid #3a352d;display:flex;flex-direction:column}
.brand{padding:18px 16px 14px;border-bottom:1px solid var(--side-line)}
.brand h1{font-size:13px;margin:0;font-weight:700;letter-spacing:2.5px;font-family:var(--mono);color:#F3EFE4}
.brand p{margin:6px 0 0;font-size:9.5px;color:#9C9079;letter-spacing:1.5px;font-family:var(--mono)}
.brand .dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--coral-bright);
  margin-right:7px;box-shadow:0 0 0 0 rgba(220,129,89,.5);animation:pulse 2.4s infinite;vertical-align:middle}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(220,129,89,.5)}70%{box-shadow:0 0 0 7px rgba(220,129,89,0)}
  100%{box-shadow:0 0 0 0 rgba(220,129,89,0)}}
.nav{display:flex;flex-direction:column;padding:10px 8px;gap:1px}
.nav a{color:#B6AE9B;min-height:40px;padding:0 10px 0 12px;display:grid;
  grid-template-columns:18px 1fr auto;gap:11px;align-items:center;text-decoration:none;
  font-size:12.5px;border-left:2px solid transparent;transition:background .12s,color .12s}
.nav a:hover{background:rgba(255,255,255,.04);color:#ECE5D6}
.nav a.on{background:linear-gradient(90deg,rgba(220,129,89,.16),transparent);
  color:#F6F0E4;border-left-color:var(--coral-bright)}
.nav .ico{font-size:15px;text-align:center;color:#8A8270}
.nav a.on .ico{color:var(--coral-bright)}
.nav small{color:#827A68;font-size:9.5px;font-family:var(--mono);letter-spacing:.5px;
  border:1px solid var(--side-line);padding:1px 5px;border-radius:2px}
.nav a.on small{color:var(--coral-bright);border-color:rgba(220,129,89,.4)}
.side-status{margin:auto 12px 14px;border:1px solid var(--side-line);
  background:rgba(255,255,255,.04);padding:11px 12px}
.side-status .t{font-size:10px;font-weight:700;color:#ECE5D6;font-family:var(--mono);letter-spacing:.6px}
.side-status .d{font-size:10.5px;color:#9C9079;margin-top:8px;line-height:1.75;font-family:var(--mono)}
.side-status .d b{color:var(--coral-bright);font-weight:600}
.side-status .ok{margin-top:10px;font-size:10px;font-weight:600;color:#8FCBA6;
  background:rgba(63,125,85,.20);border:1px solid rgba(63,125,85,.32);padding:4px 8px;
  display:block;font-family:var(--mono);letter-spacing:.4px}
.content{min-width:0}
.topbar{height:58px;background:rgba(239,236,227,.86);backdrop-filter:blur(10px);
  border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:16px;
  align-items:center;padding:0 22px;position:sticky;top:0;z-index:5}
.topbar h1{font-size:16px;margin:0;font-weight:700;letter-spacing:.4px}
.topbar .tag{color:var(--muted);border:1px solid var(--line-strong);padding:4px 9px;
  letter-spacing:.5px;font-family:var(--mono);font-size:10.5px}
.topbar .tag b{color:var(--amber)}
.body{padding:20px 22px 40px;display:grid;gap:16px}
.kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line)}
.kpi{background:var(--surface);padding:13px 15px}
.kpi .k{font-size:10px;color:var(--subtle);letter-spacing:1px;font-family:var(--mono);text-transform:uppercase}
.kpi .v{font-size:24px;font-weight:600;margin-top:8px;font-family:var(--mono);
  font-variant-numeric:tabular-nums;letter-spacing:-.5px;line-height:1.1}
.kpi .v.g{color:var(--green)}.kpi .v.r{color:var(--red)}.kpi .v.a{color:var(--amber)}
.kpi .s{font-size:10.5px;margin-top:5px;color:var(--muted);font-family:var(--mono)}
.panel{background:var(--surface);border:1px solid var(--line);overflow:hidden}
.panel-head{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;
  align-items:center;gap:10px;flex-wrap:wrap;
  background:linear-gradient(180deg,var(--surface-2),var(--surface))}
.panel-title{font-size:12.5px;margin:0;font-weight:600;letter-spacing:.5px;
  display:flex;align-items:center;gap:9px}
.panel-title::before{content:"";width:3px;height:13px;background:var(--coral);display:inline-block}
.panel-sub{color:var(--muted);font-size:10.5px;font-family:var(--mono)}
.tagb{margin-left:auto;font-family:var(--mono);font-size:9.5px;font-weight:600;letter-spacing:.8px;
  color:var(--coral);border:1px solid rgba(193,95,60,.32);background:var(--coral-soft);padding:3px 8px}
.tagb::before{content:"[ "}.tagb::after{content:" ]"}
.panel-body{padding:14px 16px 16px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12px;font-family:var(--mono)}
th{text-align:left;color:var(--subtle);font-weight:600;font-size:9.5px;letter-spacing:.9px;
  text-transform:uppercase;padding:9px 10px;border-bottom:1px solid var(--line-strong);
  position:sticky;top:0;background:var(--surface);z-index:1}
td{padding:8px 10px;border-bottom:1px solid var(--line-soft);
  font-variant-numeric:tabular-nums;vertical-align:top}
tr:last-child td{border-bottom:0}
tbody tr:hover td{background:rgba(193,95,60,.05)}
code{color:var(--coral);font-family:var(--mono);font-size:11.5px}
.badge{display:inline-block;padding:2px 8px;font-size:10px;font-weight:600;
  font-family:var(--mono);letter-spacing:.4px;border:1px solid transparent;white-space:nowrap}
.badge.b-approved{background:var(--green-soft);color:var(--green);border-color:rgba(63,125,85,.28)}
.badge.b-refuted{background:var(--red-soft);color:var(--red);border-color:rgba(190,74,58,.28)}
.badge.b-candidate{background:var(--slate-soft);color:var(--slate);border-color:rgba(94,114,135,.28)}
.badge.b-high{background:var(--amber-soft);color:var(--amber);border-color:rgba(183,132,47,.34)}
.badge.b-provisional{background:#f0eee6;color:var(--subtle);border-color:var(--line-strong)}
.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;font-size:12.5px}
.bar input,.bar select{padding:5px 8px;border:1px solid var(--line-strong);background:var(--surface);
  font-size:12px;font-family:var(--mono);color:var(--text)}
button{background:var(--coral);color:#FAF8F2;border:0;padding:6px 14px;cursor:pointer;
  font-size:12px;font-family:var(--mono);letter-spacing:.5px}
button:hover{background:var(--coral-bright)}
button.danger{background:var(--red)}
.msg{background:var(--green-soft);border:1px solid rgba(63,125,85,.32);border-left:3px solid var(--green);
  padding:10px 14px;font-size:12.5px}
.muted{color:var(--subtle);font-size:11px;font-family:var(--mono)}
.report h1{font-size:18px;letter-spacing:.3px;margin:4px 0 10px}
.report h2{font-size:13.5px;font-weight:700;letter-spacing:.4px;margin:26px 0 10px;
  padding-left:9px;border-left:3px solid var(--coral)}
.report blockquote{border-left:3px solid var(--line-strong);margin:8px 0;padding:6px 12px;
  color:var(--muted);background:var(--surface-2);font-size:12.5px}
.report ul{margin:8px 0;padding-left:20px}
.report li{margin:3px 0;line-height:1.7}
.report li.sub{margin-left:20px;list-style:circle}
.report hr{border:0;border-top:1px solid var(--line-strong);margin:18px 0}
.report p{line-height:1.7}
"""


def _nav_counts():
    try:
        doc = ps.load(pat_path())
        n = len(doc["patterns"])
        appr = sum(1 for p in doc["patterns"] if p.get("status") == "approved")
        meta = doc.get("meta", {})
    except Exception:
        n, appr, meta = 0, 0, {}
    runs = len(list(runs_dir().iterdir())) if runs_dir().exists() else 0
    return n, appr, runs, meta


def layout(title: str, active: str, body: str, msg: str = "") -> str:
    n, appr, runs, meta = _nav_counts()
    tabs = [("/", "报告", "report", "📄", str(runs)),
            ("/patterns", "规律审批", "patterns", "⚖", str(n)),
            ("/forecast", "预测明细", "forecast", "📈", "4W")]
    nav = "".join(
        f'<a href="{u}" class="{"on" if k == active else ""}"><span class="ico">{ico}</span>'
        f'<span>{t}</span><small>{c}</small></a>' for u, t, k, ico, c in tabs)
    gen = str(meta.get("generated_at", "—"))[:19]
    rng = meta.get("data_range") or ["—", "—"]
    side = (f'<div class="side-status"><div class="t">PATTERN STORE</div>'
            f'<div class="d">规律 <b>{n}</b> 条 · 已批准 <b>{appr}</b><br>'
            f'范围 {rng[0]} ~ {rng[1]}<br>版本 {gen}</div>'
            f'<span class="ok">✓ DETERMINISTIC ENGINE</span></div>')
    banner = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{title} · Treasury Agent"
            f"</title><style>{CSS}</style></head><body><div class='app'>"
            f"<aside class='sidebar'><div class='brand'><h1><span class='dot'></span>TREASURY AGENT"
            f"</h1><p>审批台 · LLM 总结规律 / 代码执行规律</p></div>"
            f"<nav class='nav'>{nav}</nav>{side}</aside>"
            f"<div class='content'><div class='topbar'><h1>{title}</h1>"
            f"<span class='tag'>schema <b>v2</b> · approved 才进计算 · 写盘自动 .bak</span></div>"
            f"<div class='body'>{banner}{body}</div></div></div></body></html>")


def kpis(items: list[tuple]) -> str:
    tiles = "".join(
        f"<div class='kpi'><div class='k'>{k}</div><div class='v {cls}'>{v}</div>"
        f"<div class='s'>{s}</div></div>" for k, v, cls, s in items)
    return f"<div class='kpis'>{tiles}</div>"


def panel(title: str, sub: str, body: str, tag: str = "") -> str:
    tagb = f"<span class='tagb'>{tag}</span>" if tag else ""
    return (f"<div class='panel'><div class='panel-head'><h2 class='panel-title'>{title}</h2>"
            f"<span class='panel-sub'>{sub}</span>{tagb}</div>"
            f"<div class='panel-body'>{body}</div></div>")


# ---------- 报告页 ----------

@app.get("/", response_class=HTMLResponse)
def report_page(run: str = ""):
    d = runs_dir() / run if run else latest_run()
    if d is None or not (d / "report.md").exists():
        return layout("报告", "report", panel("报告", "runs/ 为空", "<p>还没有报告，先跑 engine.py。</p>"))
    options = "".join(
        f'<option value="{p.name}" {"selected" if p.name == d.name else ""}>{p.name}</option>'
        for p in sorted((x for x in runs_dir().iterdir() if x.is_dir()), reverse=True))
    picker = (f"<div class='bar'><form method='get'>报告日期 <select name='run' "
              f"onchange='this.form.submit()'>{options}</select></form>"
              f"<span class='muted'>runs/{d.name}/ · report.md + forecast.csv + lineage.json</span></div>")
    content = f"<div class='report'>{md_to_html((d / 'report.md').read_text(encoding='utf-8'))}</div>"
    return layout("资金月报", "report",
                  panel("确定性引擎报告", f"runs/{d.name}", picker + content, "AUDITABLE"))


# ---------- 规律审批页 ----------

def _pat_row(p: dict) -> str:
    ev = p.get("evidence") or {}
    ev_txt = (f"hit {ev['hits']}/miss {ev['misses']} · 率 {ev['hit_rate']}"
              if ev.get("last_validated") else "—")
    extra = ""
    if p.get("demoted_at"):
        extra += f"<div class='muted'>降级: {html.escape(str(p.get('demoted_reason', '')))}</div>"
    if p.get("refuted_reason"):
        extra += f"<div class='muted'>{html.escape(str(p.get('refuted_reason', '')))}</div>"
    key = ", ".join(f"{v}" for v in p["key"].values())
    return (f"<tr><td><input type='checkbox' name='ids' value='{p['id']}'></td>"
            f"<td><code>{p['id']}</code></td>"
            f"<td><span class='badge b-{p['status']}'>{p['status']}</span></td>"
            f"<td><span class='badge b-{p['confidence']}'>{p['confidence']}</span></td>"
            f"<td>{html.escape(p['type'])}</td><td>{html.escape(key)}</td>"
            f"<td style='font-family:var(--sans)'>{html.escape(p['claim'])}{extra}</td>"
            f"<td class='muted'>{ev_txt}</td></tr>")


@app.get("/patterns", response_class=HTMLResponse)
def patterns_page(status: str = "", confidence: str = "", type_: str = "", q: str = "",
                  msg: str = ""):
    doc = load_doc()
    pats = [p for p in doc["patterns"]
            if (not status or p["status"] == status)
            and (not confidence or p["confidence"] == confidence)
            and (not type_ or p["type"] == type_)
            and (not q or q.lower() in json.dumps(p, ensure_ascii=False).lower())]
    c = {}
    for p in doc["patterns"]:
        c[p["status"]] = c.get(p["status"], 0) + 1
    pend_high = sum(1 for p in doc["patterns"]
                    if p["confidence"] == "high" and p["status"] == "candidate")
    strip = kpis([("规律总数", len(doc["patterns"]), "", "patterns.yaml"),
                  ("APPROVED", c.get("approved", 0), "g", "进计算口径"),
                  ("CANDIDATE", c.get("candidate", 0), "", "待人工审批"),
                  ("REFUTED", c.get("refuted", 0), "r", "重算不复活"),
                  ("高置信待批", pend_high, "a", "candidate · high")])

    def sel(name, cur, opts):
        o = "".join(f'<option value="{v}" {"selected" if v == cur else ""}>{v or "全部"}</option>'
                    for v in opts)
        return f"<select name='{name}' onchange='this.form.submit()'>{o}</select>"

    filters = (f"<div class='bar'><form method='get' class='bar'>状态 "
               f"{sel('status', status, ['', *ps.STATUSES])} 置信度 "
               f"{sel('confidence', confidence, ['', *ps.CONFIDENCES])} 类型 "
               f"{sel('type_', type_, ['', 'weekly_level', 'recurring', 'dom_profile', 'llm_insight'])} "
               f"搜索 <input name='q' value='{html.escape(q, quote=True)}' placeholder='payee / 主体…'>"
               f"<button>过滤</button></form></div>")
    hidden = "".join(
        f"<input type='hidden' name='f_{k}' value='{html.escape(v, quote=True)}'>"
        for k, v in (("status", status), ("confidence", confidence), ("type_", type_), ("q", q)))
    rows = "".join(_pat_row(p) for p in pats)
    actions = ("<div class='bar' style='margin-bottom:10px'>"
               "<label><input type='checkbox' onclick=\"document.querySelectorAll('input[name=ids]')"
               ".forEach(x=>x.checked=this.checked)\"> 全选本页</label>"
               "操作人 <input name='by' value='yale' size='7'>"
               "<button name='action' value='approve'>✔ 批准选中</button>"
               "否决理由 <input name='reason' size='22' placeholder='否决时必填'>"
               "<button name='action' value='refute' class='danger'>✘ 否决选中</button></div>")
    table = (f"<form method='post' action='/patterns/action'>{hidden}{actions}"
             f"<table><tr><th></th><th>id</th><th>状态</th><th>置信度</th><th>类型</th>"
             f"<th>key</th><th>claim</th><th>核验</th></tr>{rows}</table></form>")
    return layout("规律审批", "patterns",
                  strip + panel("三态规律库", f"当前显示 {len(pats)} 条", filters + table,
                                "HUMAN GATE"), msg)


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
        return layout("预测明细", "forecast",
                      panel("预测明细", "runs/ 为空", "<p>还没有 forecast.csv，先跑 engine.py。</p>"))
    fc_all = pd.read_csv(d / "forecast.csv", encoding="utf-8-sig")
    fc = fc_all
    if review and "review" in fc.columns:
        fc = fc[fc["review"] == review]
    if currency:
        fc = fc[fc["currency"] == currency]
    rv = fc_all["review"].value_counts().to_dict() if "review" in fc_all.columns else {}
    strip = kpis([("预测行数", len(fc_all), "", f"runs/{d.name}"),
                  ("AUTO REPORT", rv.get("auto_report", 0), "g", "自动进报告"),
                  ("FLAG REVIEW", rv.get("flag_review", 0), "a", "建议复核"),
                  ("REQUIRE HUMAN", rv.get("require_human", 0), "r", "必须人工确认"),
                  ("币种数", fc_all["currency"].nunique(), "", "4 周窗口")])
    cur_opts = "".join(f'<option {"selected" if c == currency else ""}>{c}</option>'
                       for c in ["", *sorted(fc_all["currency"].unique())])
    rev_opts = "".join(f'<option {"selected" if r == review else ""}>{r}</option>'
                       for r in ["", "auto_report", "flag_review", "require_human"])
    bar = (f"<div class='bar'><form method='get' class='bar'>人审档 <select name='review' "
           f"onchange='this.form.submit()'>{rev_opts}</select> 币种 <select name='currency' "
           f"onchange='this.form.submit()'>{cur_opts}</select></form>"
           f"<span class='muted'>只标注不拦截 · 阈值见 policy.example.yaml</span></div>")
    return layout("预测明细", "forecast",
                  strip + panel("未来 4 周预测", f"{len(fc)} 行", bar + fc.to_html(
                      index=False, border=0, justify="left"), "PREVIEW-ONLY"))


def main():
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    print(f"审批台 → http://127.0.0.1:{args.port}  （只监听本机）")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
