"""银行余额快照入库：把余额导出（xlsx/csv）标准化后写入 SQLite balances 表。

用法:
    python ingest_balances.py                 # 处理 data/raw/balances/ 全部文件
    python ingest_balances.py --file 导出.xlsx
列名映射走 balance_map.yaml（缺省用 balance_map.example.yaml）；
同一文件重跑先删旧记录，幂等。头寸视图取每账户最新 as_of 的快照。
"""
import argparse
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import yaml
from constants import CODE_DIR, get_root

ROOT = get_root()
DB = ROOT / "data" / "db" / "treasury.db"
RAW = ROOT / "data" / "raw" / "balances"

STD_COLS = ["as_of", "entity", "bank", "account", "currency", "balance"]


def load_balance_map() -> dict:
    for base in (ROOT, CODE_DIR):
        for name in ("balance_map.yaml", "balance_map.example.yaml"):
            p = base / name
            if p.exists():
                return yaml.safe_load(p.read_text(encoding="utf-8"))
    raise SystemExit("缺 balance_map.yaml")


def read_one(path: Path, colmap: dict) -> pd.DataFrame:
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path, encoding="utf-8-sig")
    df = df.rename(columns={k: v for k, v in colmap.items() if k in df.columns})
    missing = [c for c in ("as_of", "entity", "currency", "balance") if c not in df.columns]
    if missing:
        raise SystemExit(f"{path.name} 缺必需列 {missing}；实际列: {list(df.columns)}")
    for c in STD_COLS:
        if c not in df.columns:
            df[c] = ""
    df = df[STD_COLS].copy()
    df["as_of"] = pd.to_datetime(df["as_of"]).dt.strftime("%Y-%m-%d")
    df["balance"] = pd.to_numeric(df["balance"], errors="coerce")
    bad = df["balance"].isna().sum()
    if bad:
        print(f"  警告: {path.name} 有 {bad} 行余额无法解析，已丢弃")
        df = df.dropna(subset=["balance"])
    df["source_file"] = path.name
    df["ingested_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=None)
    args = ap.parse_args()

    colmap = load_balance_map()
    files = [RAW / args.file] if args.file else (sorted(
        p for p in RAW.iterdir() if p.suffix.lower() in (".xlsx", ".xls", ".csv")
    ) if RAW.exists() else [])
    if not files:
        raise SystemExit("data/raw/balances/ 下没有可入库文件")

    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS balances(
        as_of TEXT, entity TEXT, bank TEXT, account TEXT,
        currency TEXT, balance REAL, source_file TEXT, ingested_at TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_bal_asof ON balances(as_of)")

    total = 0
    for f in files:
        df = read_one(f, colmap)
        con.execute("DELETE FROM balances WHERE source_file=?", (f.name,))
        df.to_sql("balances", con, if_exists="append", index=False)
        total += len(df)
        print(f"入库 {f.name}: {len(df)} 行")
    con.commit()

    n, hi = con.execute("SELECT COUNT(*), MAX(as_of) FROM balances").fetchone()
    print(f"\nbalances 表现有 {n} 行，最新快照 {hi}（本次 {total} 行）")
    con.close()


if __name__ == "__main__":
    main()
