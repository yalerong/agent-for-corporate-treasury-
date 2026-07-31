"""适配器：Lark 审批导出（调拨/付款/报销申请）→ approvals 表。

导出文件第一行是筛选条件，真表头在第二行（skiprows=1）。
用法: python ingest_approvals.py --file "调拨申请_全部_xxx.xlsx" --kind 调拨
"""
import argparse
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from constants import get_root

ROOT = get_root()
DB = ROOT / "data" / "db" / "treasury.db"


def parse_hours(s) -> float | None:
    """'7h14m50.296s' / '3d2h' → 小时数"""
    if not isinstance(s, str):
        return None
    m = {k: float(v) for v, k in re.findall(r"([\d.]+)([dhms])", s)}
    return round(m.get("d", 0) * 24 + m.get("h", 0) + m.get("m", 0) / 60 + m.get("s", 0) / 3600, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--kind", default="调拨")
    args = ap.parse_args()
    src = Path(args.file)

    df = pd.read_excel(src, skiprows=1)
    if "申请编号" not in df.columns:
        raise SystemExit(f"没找到'申请编号'列，表头行不对？实际列: {list(df.columns)[:10]}")

    def col(name, default=""):
        return df[name] if name in df.columns else default

    out = pd.DataFrame({
        "apply_no": df["申请编号"].astype(str),
        "kind": args.kind,
        "status": col("申请状态"),
        "created": pd.to_datetime(col("发起时间"), errors="coerce").dt.strftime("%Y-%m-%d %H:%M"),
        "finished": pd.to_datetime(col("完成时间"), errors="coerce").dt.strftime("%Y-%m-%d %H:%M"),
        "amount": pd.to_numeric(col("金额"), errors="coerce"),
        "currency": col("金额币种"),
        "nature": col("调拨性质", col("管报一级分类")),
        "category1": col("管报一级分类"),
        "biz_entity": col("费用所属项目", col("账号所属项目")),
        "approvers": col("历史审批人"),
        "n_approvers": pd.to_numeric(col("审批人数"), errors="coerce"),
        "elapsed_hours": col("审批耗时").map(parse_hours) if "审批耗时" in df.columns else None,
        "reason": col("调拨原因", col("标题")).astype(str).str.slice(0, 120),
    })
    out["source_file"] = src.name
    out["ingested_at"] = datetime.now(UTC).isoformat(timespec="seconds")

    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS approvals(
        apply_no TEXT, kind TEXT, status TEXT, created TEXT, finished TEXT,
        amount REAL, currency TEXT, nature TEXT, category1 TEXT, biz_entity TEXT,
        approvers TEXT, n_approvers REAL, elapsed_hours REAL, reason TEXT,
        source_file TEXT, ingested_at TEXT)""")
    con.execute("DELETE FROM approvals WHERE source_file=?", (src.name,))
    out.to_sql("approvals", con, if_exists="append", index=False)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
    print(f"入库 {args.kind} 审批 {len(out)} 条；approvals 表现有 {n} 条")
    con.close()


if __name__ == "__main__":
    main()
