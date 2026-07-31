"""生成合成付款数据 + 月度预算，用于真实数据到位前跑通链路。
产出: data/raw/sample_payments.csv, data/raw/budget.csv, related_parties.yaml
"""
import numpy as np
import pandas as pd
from pathlib import Path

rng = np.random.default_rng(7)
ROOT = Path(__file__).parent
RAW = ROOT / "data" / "raw"
RAW.mkdir(parents=True, exist_ok=True)

rows = []
def add(date, entity, project, currency, payee, amount, purpose):
    rows.append(dict(日期=date.strftime("%Y-%m-%d"), 付款主体=entity, 项目=project,
                     币种=currency, 收款方=payee, 金额=round(float(amount), 2), 用途=purpose))

days = pd.date_range("2026-01-01", "2026-07-30", freq="D")
for d in days:
    # HK 主体: 发薪 15 日, 房租 5 日, 关联方资金往来月末, 日常采购
    if d.day == 15:
        add(d, "HK Co", "运营", "USD", "ADP Payroll", rng.normal(120000, 3000), "工资")
    if d.day == 5:
        add(d, "HK Co", "运营", "USD", "Landlord Ltd", 18000, "房租")
    if d.day >= 27 and d.weekday() == 4:
        add(d, "HK Co", "资金池", "USD", "母公司(关联方)", rng.normal(200000, 40000), "资金归集")
    if rng.random() < 0.45:
        add(d, "HK Co", "运营", "USD", f"Vendor-{rng.integers(1, 15)}",
            rng.lognormal(9.2, 0.7), "采购")
    # SG 主体: 营销投放(周中), 税(季度), 技术服务费给关联方
    if d.weekday() in (1, 2, 3) and rng.random() < 0.6:
        add(d, "SG Co", "营销", "SGD", f"AdPlatform-{rng.integers(1, 4)}",
            rng.lognormal(8.8, 0.5), "投放")
    if d.day == 20 and d.month in (1, 4, 7):
        add(d, "SG Co", "运营", "SGD", "IRAS", rng.normal(90000, 5000), "税款")
    if d.day == 10:
        add(d, "SG Co", "运营", "CNY", "深圳子公司(关联方)", rng.normal(150000, 10000), "技术服务费")
    if rng.random() < 0.3:
        add(d, "SG Co", "运营", "SGD", f"Vendor-SG-{rng.integers(1, 8)}",
            rng.lognormal(8.5, 0.6), "杂项")

pay = pd.DataFrame(rows)
pay.to_csv(RAW / "sample_payments.csv", index=False, encoding="utf-8-sig")

# 月度预算: 以实际月度汇总加噪声反推（故意让部分月份超支/结余，便于差异分析演示）
p = pay.copy()
p["月份"] = p["日期"].str[:7]
bud = p.groupby(["月份", "付款主体", "项目", "币种"], as_index=False)["金额"].sum()
bud["预算金额"] = (bud["金额"] * rng.normal(1.05, 0.12, len(bud))).round(-2)
bud = bud.drop(columns=["金额"])
bud.to_csv(ROOT / "data" / "budget.csv", index=False, encoding="utf-8-sig")

(ROOT / "related_parties.yaml").write_text(
    "related_parties:\n  - 母公司(关联方)\n  - 深圳子公司(关联方)\n", encoding="utf-8")

print(f"付款 {len(pay)} 笔, 预算 {len(bud)} 行 → data/raw/")
