"""适配器：finweb「流水查询_原始流水」导出 → payments 标准表。

映射口径:
  entity   = 项目归属（业务线: 集团/CFI/PRF/EEL/BantuSaku...）
  project  = 实质分类（管报口径: 手续费出款/账单出款/工资薪酬/关联方拆借出款...）
  payee    = 交易对手（缺失时回退实质分类）
  amount   = 支出原币 > 0 的行（本工具聚焦付款端）
  purpose  = 摘要 | 流水性质

用法: python ingest_liushui.py --file "C:\\...\\流水查询_原始流水_2026-07-31.xlsx"
幂等：同一 source_file 重跑先删旧记录。
"""
import argparse
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
DB = ROOT / "data" / "db" / "treasury.db"
STD_COLS = ["date", "entity", "project", "currency", "payee", "amount", "purpose"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    args = ap.parse_args()
    src = Path(args.file)

    raw = pd.read_excel(src)
    need = ["日期", "币种", "支出原币", "实质分类", "项目归属"]
    missing = [c for c in need if c not in raw.columns]
    if missing:
        raise SystemExit(f"缺列 {missing}，这不是流水查询原始流水导出？实际列: {list(raw.columns)}")

    df = raw[raw["支出原币"] > 0].copy()
    out = pd.DataFrame({
        "date": pd.to_datetime(df["日期"]).dt.strftime("%Y-%m-%d"),
        "entity": df["项目归属"].fillna("未归属").astype(str),
        "project": df["实质分类"].fillna("未分类").astype(str),
        "currency": df["币种"].astype(str),
        "payee": df["交易对手"].fillna(df["实质分类"]).astype(str),
        "amount": pd.to_numeric(df["支出原币"], errors="coerce"),
        "purpose": (df["摘要"].fillna("").astype(str) + "|" + df["流水性质"].fillna("").astype(str)),
    }).dropna(subset=["amount"])
    out["source_file"] = src.name
    out["row_hash"] = [
        hashlib.md5("|".join(str(x) for x in r).encode()).hexdigest()[:16]
        for r in out[STD_COLS].itertuples(index=False)
    ]
    out["ingested_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS payments(
        date TEXT, entity TEXT, project TEXT, currency TEXT,
        payee TEXT, amount REAL, purpose TEXT,
        source_file TEXT, row_hash TEXT, ingested_at TEXT)""")
    con.execute("DELETE FROM payments WHERE source_file=?", (src.name,))
    out.to_sql("payments", con, if_exists="append", index=False)
    con.commit()
    n, lo, hi = con.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM payments").fetchone()
    print(f"入库出账 {len(out)} 笔（原始 {len(raw)} 行）；payments 表现有 {n} 行 {lo}~{hi}")
    con.close()


if __name__ == "__main__":
    main()
