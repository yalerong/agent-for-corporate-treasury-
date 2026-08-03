"""调拨建议引擎 v0 · 输入层：全部靠人工导出的文件投喂（finweb 不直连）。

四类输入：
  资金计划表.xlsx   「资金预算-周预估」sheet，按周分块，列0 有 YYYY.M.D-YYYY.M.D 周标记
  余额总览_*.xlsx   finweb 导出，取「公司/账户/币种/账户余额」四列
  paid.yaml         已付核销清单（对日记账人工确认后填写；lark 编号或 主体+币种+金额）
  transfers.yaml    在途调拨（arrived: false 才计入待到账）

解析纪律（2026-08-02 实战教训）：
  周块行只按「金额非空」保留——付款主体可能空白（空白≠无效单），
  空白主体行保留并标记 entity=None，由引擎单列人工确认，绝不静默丢弃。
"""
import re
from pathlib import Path

import pandas as pd
import yaml

WEEK_RE = re.compile(r"^20\d{2}\.\d{1,2}\.\d{1,2}-")

# 周预估块的列位（0 起算；列 0 是周标记列，明细从列 1 开始）
PLAN_COLS = {1: "entity", 2: "amount", 3: "currency", 4: "kind", 5: "pub_priv",
             6: "channel", 7: "memo", 8: "deadline", 9: "dept", 10: "project",
             11: "submitter", 12: "lark_submitted", 13: "lark_no"}


def load_plan_week(path: str | Path, week: str | None = None,
                   sheet: str = "资金预算-周预估") -> tuple[str, pd.DataFrame]:
    """取指定周（缺省=最新一周）的明细块。返回 (周标记, DataFrame)。

    保留所有金额非空的行；entity 可能为 NaN（主体空白，需人工确认）。
    """
    df = pd.read_excel(path, sheet_name=sheet, header=None)
    col0 = df[0].astype(str)
    marks = df[col0.str.match(WEEK_RE)].index.tolist()
    if not marks:
        raise SystemExit(f"{path} 的「{sheet}」里找不到周标记（YYYY.M.D-YYYY.M.D）")
    if week is None:
        i0 = marks[0]
    else:
        hit = [i for i in marks if str(df.iloc[i, 0]).strip() == week]
        if not hit:
            raise SystemExit(f"找不到周块 {week}；表内有：{[str(df.iloc[i, 0]) for i in marks[:5]]}")
        i0 = hit[0]
    nxt = [i for i in marks if i > i0]
    i1 = nxt[0] if nxt else len(df)
    blk = df.iloc[i0 + 1:i1, list(PLAN_COLS)].copy()
    blk.columns = list(PLAN_COLS.values())
    blk["amount"] = pd.to_numeric(blk["amount"], errors="coerce")
    blk = blk[blk["amount"].notna()].reset_index(drop=True)
    blk["currency"] = blk["currency"].astype(str).str.strip()
    # lark 编号统一成纯数字字符串，便于与 paid.yaml 匹配（Excel 里常是数值型）
    blk["lark_no"] = blk["lark_no"].map(
        lambda v: re.sub(r"\.0$", "", str(v).strip()) if pd.notna(v) else "")
    return str(df.iloc[i0, 0]).strip(), blk


def load_balances(path: str | Path) -> pd.DataFrame:
    """finweb 余额总览 → DataFrame[company, account, currency, balance]。"""
    df = pd.read_excel(path)
    df.columns = [str(c).strip() for c in df.columns]
    need = {"公司", "账户", "币种", "账户余额"}
    missing = need - set(df.columns)
    if missing:
        raise SystemExit(f"{path} 缺列：{missing}（要求 finweb 余额总览导出原样）")
    out = df[["公司", "账户", "币种", "账户余额"]].copy()
    out.columns = ["company", "account", "currency", "balance"]
    out["balance"] = pd.to_numeric(out["balance"], errors="coerce").fillna(0.0)
    out["currency"] = out["currency"].astype(str).str.strip()
    return out


def load_liushui(path: str | Path, since: str | None = None) -> pd.DataFrame:
    """finweb「流水查询_原始流水」导出 → 出账流水（列口径与 ingest_liushui 一致）。

    注意：流水的「项目归属」是业务线，≠计划表付款主体——自动核销只按
    币种+金额+时间窗匹配，主体不参与（这是已知映射坑，见 TASK_BRIEF）。
    """
    raw = pd.read_excel(path)
    need = ["日期", "币种", "支出原币"]
    missing = [c for c in need if c not in raw.columns]
    if missing:
        raise SystemExit(f"{path} 缺列 {missing}，这不是流水查询原始流水导出？")
    df = raw[pd.to_numeric(raw["支出原币"], errors="coerce") > 0].copy()
    out = pd.DataFrame({
        "date": pd.to_datetime(df["日期"]),
        "currency": df["币种"].astype(str).str.strip(),
        "amount": pd.to_numeric(df["支出原币"], errors="coerce"),
        "payee": df.get("交易对手", pd.Series(dtype=object)).reindex(df.index).fillna(""),
        "memo": df.get("摘要", pd.Series(dtype=object)).reindex(df.index).fillna(""),
    }).dropna(subset=["amount"])
    if since:
        out = out[out["date"] >= pd.Timestamp(since)]
    return out.reset_index(drop=True)


def load_yaml(path: str | Path, key: str) -> list[dict]:
    """读 paid.yaml / transfers.yaml 之类的 {key: [...]} 文件；文件不存在返回 []。"""
    p = Path(path)
    if not p.exists():
        return []
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return doc.get(key) or []


def load_rules(path: str | Path) -> list[dict]:
    """规则库：只返回 status==approved 的规则（与 patterns 三态纪律同构）。"""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    rules = doc.get("rules") or []
    return [r for r in rules if r.get("status") == "approved"]


def load_entity_map(path: str | Path) -> dict:
    """主体映射：计划表简称 → finweb 公司全称；channel_overrides：交易账户 → 公司。"""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {"entities": doc.get("entities") or {},
            "channel_overrides": doc.get("channel_overrides") or {}}
