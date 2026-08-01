"""归因两函数（全确定性，LLM 不参与）。

contrib          环比贡献分解：任意维度聚合 当月/上月 金额，按 |delta| 排贡献。
calendar_align   事件日历对齐：贡献金额集中落在预期日（发薪/固定日/月末）→ 打"预期内"，砍误报。
"""
import calendar

import pandas as pd
from constants import ALIGN_SHARE, MONTH_END_DAYS


def _agg(df: pd.DataFrame, by: list[str], name: str) -> pd.Series:
    """聚合并保证空输入也带正确命名的(Multi)Index——否则 concat 后维度列名丢失。"""
    if len(df):
        return df.groupby(by)["amount"].sum().rename(name)
    idx = (pd.MultiIndex.from_arrays([[] for _ in by], names=by) if len(by) > 1
           else pd.Index([], name=by[0]))
    return pd.Series([], index=idx, dtype=float, name=name)


def contrib(cur: pd.DataFrame, prev: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    """按 by 维度聚合 cur/prev 金额，delta=cur-prev，按 |delta| 降序。

    返回列: by + [cur, prev, delta]。任一侧为空照常工作（如某币种仅上月出现）。
    """
    df = pd.concat([_agg(cur, by, "cur"), _agg(prev, by, "prev")], axis=1).fillna(0.0)
    if df.empty:
        return pd.DataFrame(columns=[*by, "cur", "prev", "delta"])
    df["delta"] = df["cur"] - df["prev"]
    df = df.reset_index()
    return df.sort_values("delta", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)


def calendar_align(g: pd.DataFrame, expected_days: set[int], month: str) -> bool:
    """该组当月付款金额落在预期日（expected_days ∪ 月末最后 MONTH_END_DAYS 天）的
    占比 ≥ ALIGN_SHARE 则视为"预期内"。g 需含 date/amount 列。"""
    if g.empty:
        return False
    y, m = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(y, m)[1]
    days = set(expected_days) | set(range(last - MONTH_END_DAYS + 1, last + 1))
    on = g[g["date"].dt.day.isin(days)]["amount"].sum()
    total = g["amount"].sum()
    return bool(total and on / total >= ALIGN_SHARE)
