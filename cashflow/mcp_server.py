"""cashflow 只读 MCP Server（Phase B）：把资金数据核心暴露给 Claude Code / 对话智能体。

十个工具**全部只读**——审批走 approve.py/ui.py、核验走 validate.py、重算走 patterns.py，
LLM 通道永远不经此写库；数字全部来自确定性产物（report/forecast/lineage/patterns/SQLite）。
付款明细只出聚合口径（按主体/项目/币种/月份汇总），不吐单笔流水。

启动(stdio): python mcp_server.py
Claude Code 注册: 仓库根 .mcp.json（restart 后 /mcp 可见 treasury-cashflow）。
"""
import json
import sqlite3

import pandas as pd
import pattern_store as ps
from constants import get_root
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("treasury-cashflow")


def _runs():
    d = get_root() / "runs"
    return sorted((p for p in d.iterdir() if p.is_dir()), reverse=True) if d.exists() else []


def _run_dir(run: str = ""):
    if run:
        d = get_root() / "runs" / run
        if not d.exists():
            raise ValueError(f"runs/{run} 不存在，可用: {[p.name for p in _runs()]}")
        return d
    if not _runs():
        raise ValueError("还没有任何报告，先跑 engine.py")
    return _runs()[0]


def _load_patterns() -> dict:
    p = get_root() / "patterns" / "patterns.yaml"
    if not p.exists():
        raise ValueError("patterns.yaml 不存在，先跑 patterns.py")
    return ps.load(p)


def _query_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    con = sqlite3.connect(get_root() / "data" / "db" / "treasury.db")
    try:
        return pd.read_sql(sql, con, params=params)
    finally:
        con.close()


# ---------- 报告与血缘 ----------

def list_runs() -> str:
    """列出所有报告日期（runs/ 目录）。想看历史某天的报告先用这个查可用日期。"""
    return json.dumps([p.name for p in _runs()], ensure_ascii=False)


def get_report(run: str = "") -> str:
    """取一期资金月报全文(markdown)。run 缺省=最新一期；八节含预算差异/归因/预测/头寸/外汇/关联方/审批/核验。"""
    return (_run_dir(run) / "report.md").read_text(encoding="utf-8")


def get_report_section(metric_id: str, run: str = "") -> str:
    """按 metric_id 取报告单节（budget_variance/mom_attribution/forecast_4w/position/
    fx_advice/related_party/approvals_profile/pattern_validation）。只关心某一块时用这个省上下文。"""
    text = (_run_dir(run) / "report.md").read_text(encoding="utf-8")
    marker = f"<!-- metric: {metric_id} -->"
    lines, out, taking = text.splitlines(), [], False
    for ln in lines:
        if ln.startswith("## "):
            taking = marker in ln
        if taking:
            out.append(ln)
    if not out:
        raise ValueError(f"报告里没有 metric {metric_id}（该节缺数据时会整节缺席）")
    return "\n".join(out)


def get_lineage(run: str = "") -> str:
    """取一期报告的数值血缘 lineage.json（每指标的 pattern_ids/数据范围/生成时间）。回答"这个数字怎么来的"用。"""
    return (_run_dir(run) / "lineage.json").read_text(encoding="utf-8")


# ---------- 预测 ----------

def query_forecast(currency: str = "", entity: str = "", review: str = "",
                   run: str = "", limit: int = 50) -> str:
    """查未来4周预测明细（forecast.csv）。可按币种/主体/人审档(auto_report|flag_review|require_human)过滤；
    返回行数上限 limit(默认50) + 分币种合计。问"下月要付多少/哪些要人审"用。"""
    fc = pd.read_csv(_run_dir(run) / "forecast.csv", encoding="utf-8-sig")
    if currency:
        fc = fc[fc["currency"] == currency]
    if entity:
        fc = fc[fc["entity"] == entity]
    if review and "review" in fc.columns:
        fc = fc[fc["review"] == review]
    totals = fc.groupby("currency")["forecast"].sum().round(2).to_dict()
    return json.dumps({"rows": len(fc), "total_by_currency": totals,
                       "detail": fc.head(limit).to_dict("records")}, ensure_ascii=False)


# ---------- 规律库 ----------

def query_patterns(status: str = "", confidence: str = "", type: str = "",  # noqa: A002
                   q: str = "", limit: int = 50) -> str:
    """查规律库（三态 schema v2）。可按 status(candidate|approved|refuted)/confidence(high|provisional)/
    type(weekly_level|recurring|dom_profile|llm_insight)/关键词过滤。返回计数+前 limit 条摘要。"""
    pats = _load_patterns()["patterns"]
    hit = [p for p in pats
           if (not status or p["status"] == status)
           and (not confidence or p["confidence"] == confidence)
           and (not type or p["type"] == type)
           and (not q or q.lower() in json.dumps(p, ensure_ascii=False).lower())]
    brief = [{"id": p["id"], "type": p["type"], "key": p["key"], "claim": p["claim"],
              "status": p["status"], "confidence": p["confidence"]} for p in hit[:limit]]
    return json.dumps({"matched": len(hit), "total": len(pats), "patterns": brief},
                      ensure_ascii=False)


def get_pattern(pattern_id: str) -> str:
    """按 pattern_id 取单条规律全量字段（含 evidence 核验证据、审批审计字段）。"""
    for p in _load_patterns()["patterns"]:
        if p["id"] == pattern_id:
            return json.dumps(p, ensure_ascii=False, default=str)
    raise ValueError(f"没有 id 为 {pattern_id} 的规律")


def validation_findings() -> str:
    """规律核验结果汇总：违反明细（人审清单）与自动降级记录。validate.py 没跑过则为空。"""
    pats = _load_patterns()["patterns"]
    checked = [p for p in pats if (p.get("evidence") or {}).get("last_validated")]
    violated = [{"id": p["id"], "key": p["key"], "claim": p["claim"],
                 "evidence": p["evidence"], "status": p["status"]}
                for p in checked if p["evidence"]["misses"] > 0]
    demoted = [{"id": p["id"], "claim": p["claim"], "demoted_at": p["demoted_at"],
                "reason": p.get("demoted_reason")} for p in pats if p.get("demoted_at")]
    return json.dumps({"checked": len(checked), "violated": violated, "demoted": demoted},
                      ensure_ascii=False, default=str)


# ---------- 数据层（聚合口径，不吐单笔） ----------

def payments_summary(month: str = "", currency: str = "", entity: str = "") -> str:
    """付款流水聚合（按 主体×项目×币种×月 汇总金额与笔数，**不含单笔明细**）。
    month 形如 2026-07；问"某月/某主体付了多少"用。"""
    sql = ("SELECT entity, project, currency, substr(date,1,7) AS month, "
           "SUM(amount) AS total, COUNT(*) AS n FROM payments WHERE 1=1")
    params: list = []
    for col, val in (("substr(date,1,7)", month), ("currency", currency), ("entity", entity)):
        if val:
            sql += f" AND {col}=?"
            params.append(val)
    sql += " GROUP BY entity, project, currency, month ORDER BY total DESC"
    df = _query_df(sql, tuple(params))
    return json.dumps({"groups": len(df), "detail": df.head(80).to_dict("records")},
                      ensure_ascii=False)


def balances_summary() -> str:
    """最新余额快照聚合（主体×币种，含快照日期）。balances 表为空则报错提示先入库。"""
    try:
        bal = _query_df("SELECT * FROM balances")
    except Exception as e:
        raise ValueError("balances 表不存在，先跑 ingest_balances.py") from e
    if bal.empty:
        raise ValueError("balances 表为空，先跑 ingest_balances.py")
    key = ["entity", "bank", "account", "currency"]
    bal[["bank", "account"]] = bal[["bank", "account"]].fillna("")
    latest = bal.groupby(key)["as_of"].transform("max")
    bal = bal[bal["as_of"] == latest]
    g = bal.groupby(["entity", "currency"], as_index=False)["balance"].sum()
    return json.dumps({"as_of": bal["as_of"].max(), "positions": g.to_dict("records")},
                      ensure_ascii=False)


TOOLS = [list_runs, get_report, get_report_section, get_lineage, query_forecast,
         query_patterns, get_pattern, validation_findings, payments_summary,
         balances_summary]
for _fn in TOOLS:
    mcp.tool()(_fn)


if __name__ == "__main__":
    mcp.run()
