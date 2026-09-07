"""finweb 五件套自动落地 · 第一半（流水 + 日余额）。

做的事：用账号密码（+TOTP 动态码）登录 finweb 拿 JWT，调两个导出接口，把 xlsx 落到
    cashflow/data/raw/<YYYY-MM-DD>/流水查询_原始流水_<YYYY-MM-DD>.xlsx
    cashflow/data/raw/<YYYY-MM-DD>/finweb余额总览_<YYYY-MM-DD>.xlsx
和你在页面上点"导出"拿到的是同一个文件（同一接口、同一鉴权）。

环境变量（.env，不入库）：
    FINWEB_BASE_URL       站点根（浏览器里登录 finweb 的那个地址）
    FINWEB_USERNAME / FINWEB_PASSWORD
    FINWEB_TOTP_SECRET    可选。Google Authenticator 里那个 base32 密钥；不填则每次登录提示手输 6 位码
    FINWEB_AUTH_PREFIX    可选，默认 /api        （主后端：登录/TOTP）
    FINWEB_FUND_PREFIX    可选，默认 /fund-api   （资金后端：导出）
token 缓存在 cashflow/data/.finweb_token.json（正式 JWT 8 小时有效），过期自动重登。

用法：
    python cashflow/fetch_finweb.py                    # 流水取最近 14 天，余额取当日
    python cashflow/fetch_finweb.py --days 30
    python cashflow/fetch_finweb.py --from 2026-08-31 --to 2026-09-06
    python cashflow/fetch_finweb.py --totp-code 123456 # 手输动态码
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import struct
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE.parent / ".env")
DATA_ROOT = Path(os.environ.get("CASHFLOW_ROOT") or HERE) / "data"
TOKEN_CACHE = DATA_ROOT / ".finweb_token.json"


def totp_now(secret_b32: str, step: int = 30, digits: int = 6) -> str:
    """RFC 6238，与 pyotp / Google Authenticator 一致；不引第三方包。"""
    key = base64.b32decode(secret_b32.strip().replace(" ", "").upper() + "=" * (-len(secret_b32.strip()) % 8))
    counter = struct.pack(">Q", int(time.time()) // step)
    mac = hmac.new(key, counter, hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


class Finweb:
    def __init__(self) -> None:
        self.base = (os.environ.get("FINWEB_BASE_URL") or "").rstrip("/")
        self.auth_prefix = os.environ.get("FINWEB_AUTH_PREFIX", "/api")
        self.fund_prefix = os.environ.get("FINWEB_FUND_PREFIX", "/fund-api")
        self.user = os.environ.get("FINWEB_USERNAME")
        self.pwd = os.environ.get("FINWEB_PASSWORD")
        self.totp_secret = os.environ.get("FINWEB_TOTP_SECRET")
        if not (self.base and self.user and self.pwd):
            raise SystemExit("缺 FINWEB_BASE_URL / FINWEB_USERNAME / FINWEB_PASSWORD（写在仓库根 .env）")
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "treasury-agent-fetch/0.1"
        self.token: str | None = None

    # ---------- 登录 ----------
    def _cached_token(self) -> str | None:
        try:
            d = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
            if d.get("base") == self.base and d.get("user") == self.user and d.get("exp", 0) > time.time() + 300:
                return d["token"]
        except Exception:
            pass
        return None

    def _save_token(self, token: str, hours: float = 8) -> None:
        TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE.write_text(json.dumps({"base": self.base, "user": self.user, "token": token,
                                           "exp": time.time() + hours * 3600}), encoding="utf-8")

    def login(self, totp_code: str | None = None) -> str:
        cached = self._cached_token()
        if cached:
            self.token = cached
            return cached
        r = self.s.post(f"{self.base}{self.auth_prefix}/auth/login",
                        json={"username": self.user, "password": self.pwd}, timeout=30)
        if r.status_code != 200:
            raise SystemExit(f"登录失败 {r.status_code}: {r.text[:300]}")
        d = r.json()
        if d.get("requires_registration"):
            raise SystemExit("该账号被要求完成注册，先在浏览器登一次")
        if d.get("requires_totp_setup"):
            raise SystemExit("该账号尚未绑定 TOTP，先在浏览器登一次完成绑定")
        if d.get("requires_totp"):
            code = totp_code or (totp_now(self.totp_secret) if self.totp_secret else None)
            if not code:
                code = input("finweb 6 位动态码: ").strip()
            r2 = self.s.post(f"{self.base}{self.auth_prefix}/auth/verify-totp",
                             json={"temp_token": d["temp_token"], "code": code}, timeout=30)
            if r2.status_code != 200:
                raise SystemExit(f"TOTP 验证失败 {r2.status_code}: {r2.text[:300]}")
            d = r2.json()
        token = d.get("access_token")
        if not token:
            raise SystemExit(f"登录响应里没有 access_token: {json.dumps(d, ensure_ascii=False)[:300]}")
        self.token = token
        self._save_token(token)
        return token

    # ---------- 导出 ----------
    def _download(self, path: str, params: dict, out: Path) -> Path:
        r = self.s.get(f"{self.base}{self.fund_prefix}{path}", params=params,
                       headers={"Authorization": f"Bearer {self.token}"}, timeout=180)
        if r.status_code == 401:
            TOKEN_CACHE.unlink(missing_ok=True)
            raise SystemExit("401：token 失效，已清缓存，重跑一次")
        if r.status_code != 200:
            raise SystemExit(f"导出失败 {path} {r.status_code}: {r.text[:300]}")
        ctype = r.headers.get("Content-Type", "")
        if "spreadsheet" not in ctype and "octet-stream" not in ctype:
            raise SystemExit(f"导出返回的不是 xlsx（Content-Type={ctype}）：{r.text[:200]}")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(r.content)
        return out

    def export_transactions(self, d_from: date, d_to: date, out: Path) -> Path:
        return self._download("/transactions/export",
                              {"date_from": d_from.isoformat(), "date_to": d_to.isoformat()}, out)

    def export_balances(self, out: Path) -> Path:
        return self._download("/daily-balances/export", {}, out)


def main() -> None:
    ap = argparse.ArgumentParser(description="finweb 流水/余额导出落地")
    ap.add_argument("--days", type=int, default=14, help="流水回看天数（默认 14）")
    ap.add_argument("--from", dest="d_from", help="流水起始日 YYYY-MM-DD（给了就忽略 --days）")
    ap.add_argument("--to", dest="d_to", help="流水截止日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--totp-code", help="手输 6 位动态码")
    ap.add_argument("--out", help="落地目录（默认 cashflow/data/raw/<今天>）")
    a = ap.parse_args()

    today = date.today()
    d_to = date.fromisoformat(a.d_to) if a.d_to else today
    d_from = date.fromisoformat(a.d_from) if a.d_from else d_to - timedelta(days=a.days)
    out_dir = Path(a.out) if a.out else DATA_ROOT / "raw" / today.isoformat()

    fw = Finweb()
    fw.login(a.totp_code)
    print(f"登录 OK（{fw.user} @ {fw.base}）")

    p1 = fw.export_transactions(d_from, d_to, out_dir / f"流水查询_原始流水_{d_to.isoformat()}.xlsx")
    print(f"流水 {d_from} → {d_to}：{p1}  ({p1.stat().st_size:,} B)")
    p2 = fw.export_balances(out_dir / f"finweb余额总览_{today.isoformat()}.xlsx")
    print(f"余额总览：{p2}  ({p2.stat().st_size:,} B)")

    # 落地即做一次结构自检，早发现导出格式变化
    try:
        import pandas as pd
        fl = pd.read_excel(p1)
        need = {"日期", "我方账户", "币种", "收入原币", "支出原币", "实质分类", "流水审批号"}
        miss = need - set(fl.columns)
        print(f"流水 {len(fl)} 行，日期 {fl['日期'].min()} → {fl['日期'].max()}" + (f"，⚠️ 缺列 {miss}" if miss else "，列齐"))
    except Exception as e:  # noqa: BLE001
        print("自检跳过:", e)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
