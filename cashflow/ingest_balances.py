"""银行余额快照入库（Excel 路径）：finweb「余额总览」导出 → SQLite balances 表。

用法:
    python ingest_balances.py                          # 处理 data/raw/balances/ 全部文件
    python ingest_balances.py --file 导出.xlsx --asof 2026-07-31
列名映射走 balance_map.yaml（缺省用 balance_map.example.yaml，默认对齐 finweb 总览导出）。
finweb 总览导出没有日期列，as_of 用 --asof（默认今天）；导出带日期列时映射 as_of 优先。
同一文件重跑先删旧记录，幂等。接口直取见 ingest_balances_api.py。
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
                raw = yaml.safe_load(p.read_text(encoding="utf-8"))
                # 新格式 {columns: {...}, entity_alias: {...}}；旧格式=整个文件就是列映射
                if "columns" in raw:
                    return {"columns": raw["columns"],
                            "entity_alias": raw.get("entity_alias") or {}}
                return {"columns": raw, "entity_alias": {}}
    raise SystemExit("缺 balance_map.yaml")


def apply_alias(df: pd.DataFrame, alias: dict) -> pd.DataFrame:
    if alias:
        df["entity"] = df["entity"].map(lambda e: alias.get(e, e))
    return df


def upsert_balances(df: pd.DataFrame, source: str):
    """写入 balances 表：同 source 先删后插，幂等。df 需含 STD_COLS。"""
    df = df[STD_COLS].copy()
    df["source_file"] = source
    df["ingested_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS balances(
        as_of TEXT, entity TEXT, bank TEXT, account TEXT,
        currency TEXT, balance REAL, source_file TEXT, ingested_at TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_bal_asof ON balances(as_of)")
    con.execute("DELETE FROM balances WHERE source_file=?", (source,))
    df.to_sql("balances", con, if_exists="append", index=False)
    con.commit()
    n, hi = con.execute("SELECT COUNT(*), MAX(as_of) FROM balances").fetchone()
    con.close()
    print(f"入库 {source}: {len(df)} 行；balances 表现有 {n} 行，最新快照 {hi}")


def read_one(path: Path, bmap: dict, asof: str) -> pd.DataFrame:
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path, encoding="utf-8-sig")
    df = df.rename(columns={k: v for k, v in bmap["columns"].items() if k in df.columns})
    missing = [c for c in ("entity", "currency", "balance") if c not in df.columns]
    if missing:
        raise SystemExit(f"{path.name} 缺必需列 {missing}；实际列: {list(df.columns)}")
    if "as_of" not in df.columns:
        df["as_of"] = asof  # finweb 总览导出无日期列
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
    return apply_alias(df, bmap["entity_alias"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=None)
    ap.add_argument("--asof", default=datetime.now(UTC).strftime("%Y-%m-%d"),
                    help="导出无日期列时的快照日期，默认今天")
    args = ap.parse_args()

    bmap = load_balance_map()
    files = [RAW / args.file] if args.file else (sorted(
        p for p in RAW.iterdir() if p.suffix.lower() in (".xlsx", ".xls", ".csv")
    ) if RAW.exists() else [])
    if not files:
        raise SystemExit("data/raw/balances/ 下没有可入库文件")

    for f in files:
        upsert_balances(read_one(f, bmap, args.asof), f.name)


if __name__ == "__main__":
    main()
