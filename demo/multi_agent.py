"""多智能体协作最小 demo：三节点编排 + 审批红线 Gate（离线可跑，无需任何 API Key）。

把 DESIGN.md §4.2 的多智能体设计落成能跑的最小实证：LangGraph 编排三个角色节点，
数据全部来自 cashflow/ 确定性流水线的产物——**只调用，不侵入**。

    [现金流分析 Agent] --只读 MCP 工具--> [调拨建议 Agent] --草案--> [人工审批 Gate]

三条纪律（与仓库既有约束一致，见 CLAUDE.md §4.3 / §5）：
1. 分析 Agent 只走 mcp_server 的聚合口径工具，绝不读单笔流水
2. 任何"会动钱"的草案必须过 Gate：跨主体调拨一律要人批；超硬限直接拒，并说明触发了哪条红线
3. 无 LLM_API_KEY / ANTHROPIC_API_KEY 时角色叙述退化为确定性模板，全流程照常跑通

用法:
    python demo/multi_agent.py              # 交互：中断处等人工 y/n
    python demo/multi_agent.py --auto       # 非交互：预置应答，供测试/CI
    python demo/multi_agent.py --root DIR   # 复用已有数据根目录，跳过合成流水线自举
"""
import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path
from typing import TypedDict

# 演示时终端只该出现协作轨迹：langchain_core 会重置全局 warning 过滤器，
# 所以在 catch_warnings 上下文里一次性导入，把弃用提醒挡在 import 那一刻
try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.graph import END, StateGraph
        from langgraph.types import Command, interrupt
except ImportError as e:  # pragma: no cover - 依赖缺失时给人话
    raise SystemExit(f"缺少 langgraph（{e}）。先装依赖: pip install -r requirements.txt") from e

REPO = Path(__file__).resolve().parent.parent
CASHFLOW_DIR = REPO / "cashflow"

# 角色名——终端轨迹靠它区分"谁做了什么"
ORCH = "编排器"
ANALYST = "现金流分析 Agent"
ADVISOR = "调拨建议 Agent"
GATE = "人工审批 Gate"

# 合成数据自举链路（与 tests/cashflow/conftest.py 同款，外加批准 high 置信度规律以走 strict 口径）
PIPELINE = (
    ("make_sample.py",),
    ("ingest.py",),
    ("ingest_balances.py",),
    ("patterns.py",),
    ("approve.py", "approve", "--all", "--confidence", "high", "--by", "demo"),
    ("validate.py",),
    ("engine.py",),
)

# 业务红线：这些动作无论金额多小都必须人工确认（动的是别人主体的钱）
ALWAYS_HUMAN_KINDS = {"transfer"}
# 超过此金额（原币口径，与 policy.yaml 同哲学：不做汇率折算）连人工通道都不给，退回线下流程
DEFAULT_HARD_LIMIT = 120_000.0
TIER_ORDER = ("auto_report", "flag_review", "require_human")


_LLM = None  # LLM 句柄：run() 初始化后节点共享（不可序列化，故不放进 state）


class DemoState(TypedDict, total=False):
    """节点间只通过本状态传递；trace 为覆盖式全量列表，驱动循环按增量打印。"""
    hard_limit: float
    analysis: dict
    proposals: list
    cursor: int
    decisions: list
    trace: list


# ---------- 数据自举 ----------

def bootstrap(root: Path) -> None:
    """在指定根目录跑一遍合成流水线（固定种子，离线确定性）。"""
    for step in PIPELINE:
        env = {**os.environ, "CASHFLOW_ROOT": str(root), "PYTHONIOENCODING": "utf-8"}
        r = subprocess.run([sys.executable, *step], cwd=CASHFLOW_DIR, env=env,
                           capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            raise SystemExit(f"流水线自举失败: {step[0]}\n{r.stdout}\n{r.stderr}")


# ---------- LLM 通道（有 key 走真模型，无 key 走模板） ----------

NARRATE_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


def llm_handle():
    """返回 llm_client 句柄或 None。任何异常都退化为 None——演示环境不因网络翻车。"""
    try:
        import llm_client
        return llm_client.get_client()
    except Exception:
        return None


def narrate(handle, role: str, facts: dict, fallback: str) -> str:
    """角色叙述：无 LLM 用确定性模板；有 LLM 只喂聚合数字，产出一句话。"""
    if handle is None:
        return fallback
    import json

    import llm_client
    try:
        out = llm_client.chat_json(
            handle,
            system=f"你是企业资金管理系统里的「{role}」。只依据给定的聚合数字，用一句中文陈述"
                   f"你的判断，不超过 60 字，不得编造未给出的数字。",
            user=json.dumps(facts, ensure_ascii=False, default=str),
            schema=NARRATE_SCHEMA,
        )
        return str(out.get("summary") or fallback)
    except Exception as e:
        print(f"  ! LLM 调用失败({type(e).__name__})，本条退回模板输出")
        return fallback


# ---------- 节点 1：现金流分析 Agent ----------

def analyst_node(state: DemoState) -> dict:
    """只调 mcp_server 的聚合口径只读工具，产出头寸/预测/核验摘要。"""
    import json

    import mcp_server as srv

    bal = json.loads(srv.balances_summary())
    fc = json.loads(srv.query_forecast(limit=200))
    vf = json.loads(srv.validation_findings())
    pay = json.loads(srv.payments_summary())
    tools = ["balances_summary", "query_forecast", "validation_findings", "payments_summary"]

    analysis = {
        "as_of": bal["as_of"],
        "positions": bal["positions"],
        "forecast_rows": fc["rows"],
        "forecast_total": fc["total_by_currency"],
        "checked": vf["checked"],
        "violated": len(vf["violated"]),
        "payment_groups": pay["groups"],
        "tools_used": tools,
    }
    total = "、".join(f"{c} {v:,.0f}" for c, v in sorted(analysis["forecast_total"].items()))
    fallback = (f"截至 {analysis['as_of']}，未来 4 周待付 {total}；"
                f"核验 {analysis['checked']} 条规律中 {analysis['violated']} 条失效。")
    say = narrate(_LLM, ANALYST, {
        "as_of": analysis["as_of"], "positions": analysis["positions"],
        "forecast_total": analysis["forecast_total"],
        "violated": analysis["violated"], "checked": analysis["checked"],
    }, fallback)

    trace = list(state.get("trace") or [])
    trace.append(f"[{ANALYST}] 调用只读工具（聚合口径，无单笔流水）: {', '.join(tools)}")
    for p in analysis["positions"]:
        trace.append(f"[{ANALYST}]   余额 {p['entity']} {p['currency']}: {p['balance']:,.2f}")
    trace.append(f"[{ANALYST}] {say}")
    return {"analysis": analysis, "trace": trace}


# ---------- 节点 2：调拨建议 Agent ----------

def advisor_node(state: DemoState) -> dict:
    """基于分析结果产出"会动钱"的草案：主体间调拨 / 购汇 / 大额付款备付。

    草案全部来自 metrics 指标层的确定性产物，本节点不重算业务逻辑。
    """
    import metrics

    ctx = metrics.build_context(None, True)
    mets = metrics.compute_all(ctx)
    proposals: list[dict] = []

    pos = mets["position"]["value"] or {}
    for e in pos.get("events", []):
        if e["kind"] == "transfer":
            proposals.append({
                "kind": "transfer", "amount": e["amount"], "currency": e["currency"],
                "title": f"{e['donor']} → {e['recipient']} 调拨 {e['currency']} {e['amount']:,.2f}",
                "text": f"{e['donor']}|{e['recipient']}|主体间调拨",
                "why": f"{e['recipient']} 该币种缺口 {e['need_before']:,.2f}，"
                       f"{e['donor']} 可用 {e['donor_avail_before']:,.2f}",
            })

    for it in (mets["fx_advice"]["value"] or {}).get("items", []):
        if it.get("need", 0) > 0:
            proposals.append({
                "kind": "fx_purchase", "amount": it["need"], "currency": it["currency"],
                "title": f"购汇 {it['currency']} {it['need']:,.2f}",
                "text": f"购汇|{it['currency']}",
                "why": "余额与调拨后仍未覆盖的净缺口",
            })

    fc = mets["forecast_4w"]["value"]
    if fc is not None and not fc.empty:
        big = fc[fc["review"] != "auto_report"].sort_values("forecast", ascending=False)
        seen = set()
        for _, r in big.iterrows():
            payee = str(r.get("payee") or "").strip() or "（未指明收款方）"
            key = (payee, r["currency"])
            if key in seen:
                continue
            seen.add(key)
            proposals.append({
                "kind": "prefund", "amount": float(r["forecast"]), "currency": r["currency"],
                "title": f"为 {payee} 备付 {r['currency']} {float(r['forecast']):,.2f}",
                "text": f"{r.get('entity') or ''}|{r.get('project') or ''}|{payee}",
                "why": f"预测来源 {r['source']}（{r['confidence']}/{r['status']}）",
            })

    fallback = f"依据头寸与预测生成 {len(proposals)} 条待批草案，全部需过审批 Gate。"
    say = narrate(_LLM, ADVISOR,
                  {"proposals": [{k: p[k] for k in ("kind", "amount", "currency", "why")}
                                 for p in proposals]}, fallback)

    trace = list(state.get("trace") or [])
    trace.append(f"[{ADVISOR}] 产出 {len(proposals)} 条草案（尚未生效，全部待批）:")
    for i, p in enumerate(proposals, 1):
        trace.append(f"[{ADVISOR}]   草案{i} [{p['kind']}] {p['title']} —— {p['why']}")
    trace.append(f"[{ADVISOR}] {say}")
    return {"proposals": proposals, "cursor": 0, "decisions": [], "trace": trace}


# ---------- 节点 3：人工审批 Gate ----------

def _tier_of(p: dict, pol: dict) -> tuple[str, list[str]]:
    """判定审批档位并给出理由：policy 分档与"动作固有红线"取更严的一档。"""
    import policy

    base = policy.review_tier(p["amount"], p["currency"], p["text"], pol)
    reasons = []
    for kw in pol.get("keywords_require_human") or []:
        if kw in p["text"]:
            reasons.append(f"policy 关键词命中「{kw}」")
    th = (pol.get("by_currency") or {}).get(p["currency"]) or pol["default"]
    if p["amount"] >= th["require_human"]:
        reasons.append(f"金额 ≥ {p['currency']} require_human 阈值 {th['require_human']:,.0f}")
    elif p["amount"] >= th["flag_review"]:
        reasons.append(f"金额 ≥ {p['currency']} flag_review 阈值 {th['flag_review']:,.0f}")
    tier = base
    if p["kind"] in ALWAYS_HUMAN_KINDS:
        reasons.append("跨主体调拨：动其他主体的钱，一律人工确认")
        if TIER_ORDER.index(base) < TIER_ORDER.index("require_human"):
            tier = "require_human"
    return tier, reasons


def judge(p: dict, pol: dict, hard_limit: float) -> dict:
    """纯函数裁决：返回 {tier, redlines, verdict}，verdict ∈ 放行/待人工/硬拒。

    硬拒条件：已属 require_human 档，且金额突破硬限——此时连人工通道都不开。
    """
    tier, reasons = _tier_of(p, pol)
    if tier == "require_human" and p["amount"] >= hard_limit:
        reasons.append(f"金额 {p['amount']:,.2f} {p['currency']} ≥ 硬限 {hard_limit:,.2f}"
                       f"（demo 红线：超此额度不走 Agent 通道，退线下双签）")
        return {"tier": tier, "redlines": reasons, "verdict": "rejected"}
    if tier == "require_human":
        return {"tier": tier, "redlines": reasons, "verdict": "needs_human"}
    return {"tier": tier, "redlines": reasons, "verdict": "passed"}


def gate_node(state: DemoState) -> dict:
    """每次处理一条草案；需要人批时 interrupt 暂停，恢复后本节点重放（纯计算，幂等）。"""
    import policy

    props = state["proposals"]
    i = state["cursor"]
    p = props[i]
    pol = policy.load_policy()
    d = judge(p, pol, state["hard_limit"])

    trace = list(state.get("trace") or [])
    head = f"[{GATE}] 草案{i + 1}/{len(props)} {p['title']} → 档位 {d['tier']}"
    if d["verdict"] == "rejected":
        trace.append(f"{head}｜❌ 拒绝")
        for r in d["redlines"]:
            trace.append(f"[{GATE}]   触发红线: {r}")
        trace.append(f"[{GATE}]   处置: 不进入执行队列，转线下审批流程")
    elif d["verdict"] == "needs_human":
        answer = interrupt({
            "kind": "approval_request",
            "title": p["title"], "amount": p["amount"], "currency": p["currency"],
            "tier": d["tier"], "reasons": d["redlines"], "why": p["why"],
        })
        ok = str(answer).strip().lower() in ("y", "yes", "是", "true", "1")
        d["verdict"] = "approved" if ok else "declined"
        # 要人批的理由已由驱动循环在暂停时打印，这里只记结论，避免同一条理由刷两遍
        trace.append(f"{head}｜⏸ 已暂停请人工裁决 → {'批准 ✅' if ok else '驳回 ❌'}")
    else:
        trace.append(f"{head}｜✅ 放行（仅标注，不改动任何账务）")
        for r in d["redlines"]:
            trace.append(f"[{GATE}]   备注: {r}")

    decisions = list(state.get("decisions") or [])
    decisions.append({**d, "proposal": p})
    return {"cursor": i + 1, "decisions": decisions, "trace": trace}


def after_gate(state: DemoState) -> str:
    return "gate" if state["cursor"] < len(state["proposals"]) else "summary"


def summary_node(state: DemoState) -> dict:
    from collections import Counter
    c = Counter(d["verdict"] for d in state["decisions"])
    trace = list(state.get("trace") or [])
    trace.append(f"[{ORCH}] 汇总: 共 {len(state['decisions'])} 条草案 → "
                 f"放行 {c['passed']}、人工批准 {c['approved']}、人工驳回 {c['declined']}、"
                 f"红线拒绝 {c['rejected']}")
    trace.append(f"[{ORCH}] 本 demo 全程只读：没有任何付款执行工具，Agent 手上没有这把枪")
    return {"trace": trace}


def build_graph():
    g = StateGraph(DemoState)
    g.add_node("analyst", analyst_node)
    g.add_node("advisor", advisor_node)
    g.add_node("gate", gate_node)
    g.add_node("summary", summary_node)
    g.set_entry_point("analyst")
    g.add_edge("analyst", "advisor")
    g.add_conditional_edges("advisor", lambda s: "gate" if s["proposals"] else "summary",
                            {"gate": "gate", "summary": "summary"})
    g.add_conditional_edges("gate", after_gate, {"gate": "gate", "summary": "summary"})
    g.add_edge("summary", END)
    return g.compile(checkpointer=MemorySaver())


# ---------- 驱动 ----------

def _flush(state: dict, printed: int) -> int:
    trace = state.get("trace") or []
    for line in trace[printed:]:
        print(line)
    return len(trace)


def _interrupts(state: dict):
    v = state.get("__interrupt__")
    return list(v) if v else []


def run(root: Path, hard_limit: float, auto_answers: list[str] | None) -> dict:
    """跑完整条协作链路，返回终态。auto_answers 非 None 即非交互模式。"""
    global _LLM
    _LLM = handle = llm_handle()
    print(f"[{ORCH}] LLM 通道: " + ("已配置，角色叙述走真模型" if handle
                                     else "未配置（无 ANTHROPIC_API_KEY / LLM_API_KEY），"
                                          "角色叙述走确定性模板，流程照常"))
    print(f"[{ORCH}] 数据根目录: {root}")
    print(f"[{ORCH}] 审批硬限: {hard_limit:,.2f}（原币口径）\n")

    graph = build_graph()
    cfg = {"configurable": {"thread_id": "multi-agent-demo"}}
    state = graph.invoke({"hard_limit": hard_limit, "trace": []}, cfg)
    printed = _flush(state, 0)

    pending = list(auto_answers or [])
    while _interrupts(state):
        req = _interrupts(state)[0].value
        print(f"\n[{GATE}] ⏸ 需要人工确认: {req['title']}")
        print(f"[{GATE}]   档位 {req['tier']}｜理由: {'；'.join(req['reasons'])}")
        if auto_answers is None:
            ans = input(f"[{GATE}]   批准吗? (y/n): ")
        else:
            ans = pending.pop(0) if pending else "n"
            print(f"[{GATE}]   (--auto 预置应答: {ans})")
        print()
        state = graph.invoke(Command(resume=ans), cfg)
        printed = _flush(state, printed)
    return state


def main(argv=None) -> int:
    with contextlib.suppress(Exception):  # Windows 控制台默认 ANSI 代码页，中文会乱码
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="已有数据根目录；缺省=临时目录跑一遍合成流水线")
    ap.add_argument("--auto", action="store_true", help="非交互，用预置应答替代人工输入")
    ap.add_argument("--auto-answers", default="y", help="--auto 时的应答序列，逗号分隔（默认 y）")
    ap.add_argument("--hard-limit", type=float,
                    default=float(os.environ.get("DEMO_GATE_HARD_LIMIT") or DEFAULT_HARD_LIMIT),
                    help=f"审批硬限，超过直接拒绝（默认 {DEFAULT_HARD_LIMIT:,.0f}）")
    args = ap.parse_args(argv)

    tmp = None
    if args.root:
        root = Path(args.root)
    else:
        tmp = Path(tempfile.mkdtemp(prefix="treasury_demo_"))
        root = tmp
        print(f"[{ORCH}] 自举合成数据（{len(PIPELINE)} 步流水线，无需任何凭据）…")
        bootstrap(root)

    # cashflow 是平铺脚本目录：必须先定 CASHFLOW_ROOT 再 import（metrics 模块级解析 ROOT）
    os.environ["CASHFLOW_ROOT"] = str(root)
    sys.path.insert(0, str(CASHFLOW_DIR))
    try:
        answers = [a.strip() for a in args.auto_answers.split(",")] if args.auto else None
        run(root, args.hard_limit, answers)
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
