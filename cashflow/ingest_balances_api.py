"""银行余额快照入库（finweb 接口路径）：/api/v1/daily-balances/overview → balances 表。

用法:
    set FINWEB_BASE_URL=https://<finweb地址>
    set FINWEB_TOKEN=<JWT token>          # 登录 finweb 后的 Bearer token
    python ingest_balances_api.py [--asof 2026-07-31] [--company-id 3]

取 finweb 余额总览（account_balance 为 DS-1/2/3 交叉校验后的口径，与 Excel 导出同源），
as_of 默认今天（总览是"当前"余额）。source_file 固定 finweb_api，重跑覆盖，幂等。
entity 归一沿用 balance_map.yaml 的 entity_alias。Excel 导入见 ingest_balances.py。
"""
import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import UTC, datetime

import pandas as pd
from ingest_balances import apply_alias, load_balance_map, upsert_balances

SOURCE = "finweb_api"


def fetch_overview(base_url: str, token: str, company_id: int | None) -> list[dict]:
    params = {"company_id": company_id} if company_id is not None else {}
    url = (f"{base_url.rstrip('/')}/api/v1/daily-balances/overview"
           + ("?" + urllib.parse.urlencode(params) if params else ""))
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    # 兼容 {code,data:[...]} 与直接返回列表两种包法
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        raise SystemExit(f"接口返回结构不认识: {str(payload)[:200]}")
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default=datetime.now(UTC).strftime("%Y-%m-%d"))
    ap.add_argument("--company-id", type=int, default=None)
    args = ap.parse_args()

    base_url = os.environ.get("FINWEB_BASE_URL")
    token = os.environ.get("FINWEB_TOKEN")
    if not base_url or not token:
        raise SystemExit("需要环境变量 FINWEB_BASE_URL 和 FINWEB_TOKEN（finweb 登录后的 JWT）")

    rows = fetch_overview(base_url, token, args.company_id)
    df = pd.DataFrame([{
        "as_of": args.asof,
        "entity": r.get("company_name", ""),
        "bank": "",
        "account": r.get("account_name", ""),
        "currency": r.get("currency", ""),
        "balance": r.get("account_balance"),
    } for r in rows])
    if df.empty:
        raise SystemExit("接口没有返回任何账户")
    df["balance"] = pd.to_numeric(df["balance"], errors="coerce")
    df = df.dropna(subset=["balance"])
    df = apply_alias(df, load_balance_map()["entity_alias"])
    upsert_balances(df, SOURCE)


if __name__ == "__main__":
    main()
