"""适配器：预算汇总工作簿「周付款预测」sheet → data/budget.csv。

前置结论（2026-07-31 预算簿质量鉴定）：
  - 周付款预测是全簿唯一维护认真的明细表，可聚合到月
  - 往来计划是内部调拨且质量差，不得混入预算（会双计支出）
  - v1 口径：月度 × 币种（对应主体→业务线是多对多，留待清洗表就绪后细化）

用法: python ingest_budget.py --file "C:\\...\\预算汇总（新版） (1).xlsx"
"""
import argparse

import pandas as pd
from constants import get_root

ROOT = get_root()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--sheet", default="周付款预测")
    args = ap.parse_args()

    df = pd.read_excel(args.file, sheet_name=args.sheet)
    need = ["预算时间", "预算金额", "币种"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise SystemExit(f"缺列 {missing}；实际列: {list(df.columns)[:12]}")

    df["预算时间"] = pd.to_datetime(df["预算时间"], errors="coerce")
    df["预算金额"] = pd.to_numeric(df["预算金额"], errors="coerce")
    df["币种"] = df["币种"].astype(str).str.strip().str.upper()
    ok = df.dropna(subset=["预算时间", "预算金额"])
    ok = ok[ok["币种"].ne("NAN") & ok["预算金额"].gt(0)]
    dropped = len(df) - len(ok)

    bud = (ok.assign(月份=ok["预算时间"].dt.strftime("%Y-%m"))
             .groupby(["月份", "币种"], as_index=False)["预算金额"].sum())
    bud.insert(1, "付款主体", "全部")
    bud.insert(2, "项目", "全部")

    out = ROOT / "data" / "budget.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    bud.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"预算 {len(bud)} 行（月×币种）→ {out}；"
          f"源 {len(df)} 行，剔除缺时间/金额/币种 {dropped} 行；"
          f"月份范围 {bud['月份'].min()} ~ {bud['月份'].max()}")


if __name__ == "__main__":
    main()
