"""两条防错设计的专项守护（TASK_BRIEF 明确"别改回去"）：
1. 稀疏组补零——无付款的周计为 0，否则稀疏付款组的周度基准被严重高估
2. recurring 从周度基线剔除——固定节奏付款单列，避免预测重复计数
直接单测 patterns.py 的函数，不走全链路。
"""
import pandas as pd
from patterns import recurring, weekly_level


def make_df(rows: list[dict]) -> pd.DataFrame:
    """模拟 patterns.load() 的派生列。"""
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df["week"] = df["date"].dt.to_period("W-SUN").dt.start_time
    df["dom"] = df["date"].dt.day
    return df


def pay(date, amount, entity="A Co", project="运营", currency="USD", payee="V"):
    return dict(date=date, entity=entity, project=project,
                currency=currency, payee=payee, amount=amount)


def test_sparse_group_zero_fill():
    # 10 周跨度里只有首尾两周各付 10000：补零后近 4 周应接近 0，而不是 10000
    df = make_df([pay("2026-01-05", 10000.0), pay("2026-03-09", 10000.0)])
    out = weekly_level(df)
    assert len(out) == 1
    base = out[0]["base_weekly"]
    assert base < 5000, f"稀疏组基准被高估: {base}（补零防错被破坏?）"
    assert out[0]["sample_weeks"] == 10  # 空周也计入样本


def test_recurring_excluded_from_weekly_baseline():
    # X/Y 各为月度固定付款（15日/22日各 100000，应识别为 recurring）；另有小额日常付款。
    # 基线是"近4周去最大值后的均值"，只放一个固定付款会被截尾吸收，
    # 两个错开日期的固定付款能暴露不剔除时的重复计数。
    rows = []
    for m in range(1, 7):
        rows.append(pay(f"2026-{m:02d}-15", 100000.0, payee="X"))
        rows.append(pay(f"2026-{m:02d}-22", 100000.0, payee="Y"))
    for d in pd.date_range("2026-01-01", "2026-06-30", freq="3D"):
        rows.append(pay(d.strftime("%Y-%m-%d"), 1000.0, payee=f"V-{d.day % 5}"))
    df = make_df(rows)

    recs = recurring(df)
    rec_payees = {p["key"]["payee"] for p in recs}
    assert {"X", "Y"} <= rec_payees, f"固定付款未被识别为 recurring: {rec_payees}"

    # 复刻 main() 的剔除逻辑
    base_with = weekly_level(df)[0]["base_weekly"]
    base_without = weekly_level(df[~df["payee"].isin(rec_payees)])[0]["base_weekly"]
    assert base_without < base_with, "剔除 recurring 后基线未下降——防重复计数被破坏?"
    assert base_without < 5000  # 只剩日常小额
