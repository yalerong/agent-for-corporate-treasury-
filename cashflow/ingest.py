"""把 data/raw/ 下的付款导出（xlsx/csv）标准化后写入 SQLite。

用法:
    python ingest.py                      # 处理 data/raw/ 全部文件
    python ingest.py --file 某导出.xlsx    # 只处理一个
每次入库带 source_file + ingested_at，同一文件重跑先删旧记录，幂等。
"""
import argparse
import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import yaml
from constants import CODE_DIR, get_root

ROOT = get_root()
DB = ROOT / "data" / "db" / "treasury.db"
RAW = ROOT / "data" / "raw"

STD_COLS = ["date", "entity", "project", "currency", "payee", "amount", "purpose"]


def load_column_map() -> dict:
    # 数据根目录优先（真实映射），随代码走的 example 兜底
    for base in (ROOT, CODE_DIR):
        for name in ("column_map.yaml", "column_map.example.yaml"):
            p = base / name
            if p.exists():
                return yaml.safe_load(p.read_text(encoding="utf-8"))
    raise SystemExit("缺 column_map.yaml")


def read_one(path: Path, colmap: dict) -> pd.DataFrame:
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path, encoding="utf-8-sig")
    df = df.rename(columns={k: v for k, v in colmap.items() if k in df.columns})
    missing = [c for c in ("date", "entity", "currency", "amount") if c not in df.columns]
    if missing:
        raise SystemExit(f"{path.name} 缺必需列 {missing}；实际列: {list(df.columns)}")
    for c in STD_COLS:
        if c not in df.columns:
            df[c] = ""
    df = df[STD_COLS].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    bad = df["amount"].isna().sum()
    if bad:
        print(f"  警告: {path.name} 有 {bad} 行金额无法解析，已丢弃")
        df = df.dropna(subset=["amount"])
    df["source_file"] = path.name
    df["row_hash"] = [
        hashlib.md5("|".join(str(x) for x in row).encode()).hexdigest()[:16]
        for row in df[STD_COLS].itertuples(index=False)
    ]
    df["ingested_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=None)
    args = ap.parse_args()

    colmap = load_column_map()
    files = [RAW / args.file] if args.file else sorted(
        p for p in RAW.iterdir() if p.suffix.lower() in (".xlsx", ".xls", ".csv")
    )
    if not files:
        raise SystemExit("data/raw/ 下没有可入库文件")

    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS payments(
        date TEXT, entity TEXT, project TEXT, currency TEXT,
        payee TEXT, amount REAL, purpose TEXT,
        source_file TEXT, row_hash TEXT, ingested_at TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_pay_date ON payments(date)")

    total = 0
    for f in files:
        df = read_one(f, colmap)
        con.execute("DELETE FROM payments WHERE source_file=?", (f.name,))
        df.to_sql("payments", con, if_exists="append", index=False)
        total += len(df)
        print(f"入库 {f.name}: {len(df)} 行")
    con.commit()

    n, lo, hi = con.execute(
        "SELECT COUNT(*), MIN(date), MAX(date) FROM payments").fetchone()
    print(f"\npayments 表现有 {n} 行，日期范围 {lo} ~ {hi}（本次 {total} 行）")
    con.close()


if __name__ == "__main__":
    main()
