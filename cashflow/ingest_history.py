"""适配器：历史管报日记账 xlsm（月度定稿版）→ payments 标准表。

读 history_manifest.yaml（本地文件，不入库）:
  months:
    "202501": "C:\\...\\管报日记账逻辑-202501-v5.xlsm"
    ...
每个文件 sheet=日记账，老代列名自动改名；只保留日期落在文件月份内的行（防跨月污染）。
口径与 ingest_liushui 对齐: entity=项目(业务线), project=辅助字段1(分类), amount=出账>0。
"""
import hashlib
import sqlite3
from datetime import UTC, datetime

import pandas as pd
import yaml
from constants import CODE_DIR, get_root

ROOT = get_root()
DB = ROOT / "data" / "db" / "treasury.db"
STD_COLS = ["date", "entity", "project", "currency", "payee", "amount", "purpose"]

RENAME = {"付款时间": "日期", "资金流入": "入账", "资金流出": "出账",
          "备注": "摘要", "付款编码": "唯一编码", "Unnamed: 1": "我方账户"}

_ALIAS_CACHE = None


def entity_alias() -> dict:
    global _ALIAS_CACHE
    if _ALIAS_CACHE is None:
        _ALIAS_CACHE = load_entity_alias()
    return _ALIAS_CACHE


def load_entity_alias() -> dict:
    """历史(项目列)与权威流水(项目归属列)的业务线命名归一表。

    真实业务线命名不入库：复制 entity_alias.example.yaml 为 entity_alias.yaml
    （已 gitignore）后填写。找不到时用示例表并显式提示——静默套错映射
    比直接报错更危险，会把两条业务线悄悄并成一条。
    """
    for base in (ROOT, CODE_DIR):
        p = base / "entity_alias.yaml"
        if p.exists():
            return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    ex = CODE_DIR / "entity_alias.example.yaml"
    print(f"! 未找到 entity_alias.yaml，暂用示例映射（{ex.name}）；"
          f"接真实数据前请复制该文件为 entity_alias.yaml 并填入你的业务线命名")
    return yaml.safe_load(ex.read_text(encoding="utf-8")) or {}


def read_month(month: str, path: str, cutoff: str | None) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name="日记账")
    # 仅当目标列不存在时才改名（部分文件新老列名并存），再去重列
    ren = {k: v for k, v in RENAME.items() if k in df.columns and v not in df.columns}
    df = df.rename(columns=ren)
    df = df.loc[:, ~df.columns.duplicated()]
    need = ["日期", "币种", "出账"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise SystemExit(f"{month}: 缺列 {missing}")
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    n0 = len(df)
    df = df[df["日期"].dt.strftime("%Y%m") == month]          # 防跨月污染
    if cutoff:
        df = df[df["日期"] <= cutoff]                          # 与权威流水衔接
    df = df[pd.to_numeric(df["出账"], errors="coerce") > 0]
    dropped = n0 - len(df)

    def col(name):
        return df[name] if name in df.columns else pd.Series("", index=df.index)

    out = pd.DataFrame({
        "date": df["日期"].dt.strftime("%Y-%m-%d"),
        "entity": (col("项目").fillna("未归属").replace("", "未归属").astype(str)
                   .str.strip().replace(entity_alias())),
        "project": col("辅助字段1").fillna("未分类").replace("", "未分类").astype(str),
        "currency": df["币种"].astype(str).str.strip().str.upper(),
        "payee": col("交易对手").fillna(col("摘要")).fillna("未知").astype(str),
        "amount": pd.to_numeric(df["出账"], errors="coerce"),
        "purpose": col("摘要").fillna("").astype(str),
    }).dropna(subset=["amount"])
    print(f"  {month}: 保留出账 {len(out)} 笔（原 {n0} 行，滤跨月/非出账 {dropped}）")
    return out


def main():
    mf = ROOT / "history_manifest.yaml"
    if not mf.exists():
        raise SystemExit("缺 history_manifest.yaml（参考 history_manifest.example.yaml）")
    cfg = yaml.safe_load(mf.read_text(encoding="utf-8"))

    frames = []
    for month, path in sorted(cfg["months"].items()):
        cutoff = cfg.get("cutoffs", {}).get(month)
        frames.append(read_month(str(month), path, cutoff))
    out = pd.concat(frames, ignore_index=True)
    out["source_file"] = "history_mgmt_journal"
    out["row_hash"] = [
        hashlib.md5("|".join(str(x) for x in r).encode()).hexdigest()[:16]
        for r in out[STD_COLS].itertuples(index=False)
    ]
    out["ingested_at"] = datetime.now(UTC).isoformat(timespec="seconds")

    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS payments(
        date TEXT, entity TEXT, project TEXT, currency TEXT,
        payee TEXT, amount REAL, purpose TEXT,
        source_file TEXT, row_hash TEXT, ingested_at TEXT)""")
    con.execute("DELETE FROM payments WHERE source_file='history_mgmt_journal'")
    out.to_sql("payments", con, if_exists="append", index=False)
    con.commit()
    n, lo, hi = con.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM payments").fetchone()
    print(f"合计入库历史出账 {len(out)} 笔；payments 表现有 {n} 行 {lo}~{hi}")
    con.close()


if __name__ == "__main__":
    main()
