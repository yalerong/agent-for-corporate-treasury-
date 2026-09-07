"""finweb 五件套自动落地 · 第二半（Lark：三张云表格 + 调拨审批）。

三张表（银行账户余额表 / 支付账户余额表 / 资金计划表）走 Drive 导出任务，
拿到的 xlsx 与你在 Lark 里点"下载为 Excel"完全一样，现有 convert_*.py 不用改。
调拨审批走审批 v4 实例查询，拉最近 N 天该审批定义下的全部实例，摊平成一张 xlsx。

云表格走用户身份 OAuth（user_access_token，一次性回调脚本 oauth_server.py 换 code）；
审批走应用身份（tenant_access_token）——2026-09-06 实测 instances/query 对 user token 返回 "not support"。
实测无需额外加 scope：应用已有的用户身份权限就能跑 Drive 导出；审批实例查询用 tenant token 即通。
user_access_token 2 小时过期：过期时本脚本会提示重跑授权（--auth 会替你起回调服务并打印授权链接）。

环境变量（.env）：
    LARK_APP_ID / LARK_APP_SECRET   应用凭据（只从环境变量/.env 读取）
    LARK_OAUTH_SERVER       一次性 OAuth 回调脚本路径（默认与本文件同目录 oauth_server.py）
    LARK_USER_TOKEN_FILE    user token 缓存（默认与回调脚本同目录 user_token.json）
    LARK_SHEET_BANK / LARK_SHEET_PAY / LARK_SHEET_BUDGET   三张表的 spreadsheet token（URL 里 /sheets/<token>）
    LARK_APPROVAL_CODE_DIAOBO   调拨申请审批定义 code（instances/query 返回的 approval.code）
    LARK_APPROVAL_TIMEZONE      审批时间输出时区（默认 Asia/Shanghai）

用法：
    python cashflow/fetch_lark.py            # 三表 + 最近 30 天调拨审批
    python cashflow/fetch_lark.py --sheets   # 只导三表
    python cashflow/fetch_lark.py --approvals --days 60
    python cashflow/fetch_lark.py --auth     # token 过期时：起回调服务 + 打印授权链接
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE.parent / ".env")
DATA_ROOT = Path(os.environ.get("CASHFLOW_ROOT") or HERE) / "data"
BASE = os.environ.get("LARK_API_BASE", "https://open.larksuite.com/open-apis")  # 国际版；国内版换 open.feishu.cn
APP_ID = os.environ.get("LARK_APP_ID", "")
REDIRECT = os.environ.get("LARK_OAUTH_REDIRECT", "http://localhost:3000/callback")
OAUTH_SERVER = Path(os.environ.get("LARK_OAUTH_SERVER") or HERE / "oauth_server.py")
TOKEN_FILE = Path(os.environ.get("LARK_USER_TOKEN_FILE") or OAUTH_SERVER.with_name("user_token.json"))
BUSINESS_TZ = os.environ.get("LARK_APPROVAL_TIMEZONE", "Asia/Shanghai")

SHEETS = {  # 文件名前缀 → env 键
    "银行账户余额表 2026": "LARK_SHEET_BANK",
    "支付账户余额表 2026": "LARK_SHEET_PAY",
    "资金计划表": "LARK_SHEET_BUDGET",
}


def auth_host_for_api_base(api_base: str = BASE) -> str:
    if "open.feishu.cn" in api_base:
        return "https://accounts.feishu.cn"
    return "https://accounts.larksuite.com"


def sheet_envs_configured() -> bool:
    return any(os.environ.get(env) for env in SHEETS.values())


def business_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(BUSINESS_TZ)
    except ZoneInfoNotFoundError:
        raise SystemExit(f"未知 LARK_APPROVAL_TIMEZONE={BUSINESS_TZ!r}，请使用 IANA 时区名，例如 Asia/Shanghai") from None


def approval_windows(end: datetime, days: int) -> list[tuple[datetime, datetime]]:
    start = end - timedelta(days=days)
    windows = []
    cursor = start
    while cursor < end:
        nxt = min(cursor + timedelta(days=30), end)
        windows.append((cursor, nxt))
        cursor = nxt
    return windows or [(start, end)]


# ---------- token ----------
def load_token() -> str:
    try:
        d = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"没有 {TOKEN_FILE}，先 `python cashflow/fetch_lark.py --auth` 授权一次") from None
    tok = d.get("access_token")
    if not tok:
        raise SystemExit(f"{TOKEN_FILE} 里没有 access_token，重新授权")
    mtime = TOKEN_FILE.stat().st_mtime
    ttl = d.get("expires_in", 7200)
    if time.time() > mtime + ttl - 120:
        raise SystemExit("user_access_token 已过期（2 小时），重跑 `--auth` 授权")
    return tok


def do_auth() -> None:
    if not OAUTH_SERVER.exists():
        raise SystemExit(f"找不到 {OAUTH_SERVER}")
    state = secrets.token_urlsafe(24)
    url = (f"{auth_host_for_api_base()}/open-apis/authen/v1/authorize?app_id={APP_ID}"
           f"&redirect_uri={requests.utils.quote(REDIRECT, safe='')}&state={state}")
    print("1) 回调服务已在 :3000 等待\n2) 浏览器打开下面链接并点同意：\n\n   " + url + "\n", flush=True)
    env = {**os.environ, "LARK_OAUTH_STATE": state}
    subprocess.run([sys.executable, str(OAUTH_SERVER)], check=True, env=env)
    print("token 写入", TOKEN_FILE)


def api(method: str, path: str, token: str, **kw) -> dict:
    r = None
    for attempt in range(4):  # open.larksuite.com 偶发读超时（2026-09-06 实测），指数退避重试
        try:
            r = requests.request(method, f"{BASE}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=60, **kw)
            break
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt == 3:
                raise SystemExit(f"{path} 网络失败（已重试 3 次）: {e}") from None
            time.sleep(2 ** attempt)
    try:
        d = r.json()
    except ValueError:
        raise SystemExit(f"{path} 非 JSON 响应 {r.status_code}: {r.text[:200]}") from None
    if r.status_code != 200 or d.get("code", 0) != 0:
        hint = ""
        if d.get("code") in (99991672, 99991663, 99991661) or r.status_code == 403:
            hint = "（应用缺权限：去开发者后台给用户身份加对应 scope 并重新发布）"
        raise SystemExit(f"{path} 失败 {r.status_code} code={d.get('code')} {d.get('msg')} {hint}")
    return d


# ---------- 云表格导出 ----------
def export_sheet(token: str, sheet_token: str, out: Path) -> Path:
    d = api("POST", "/drive/v1/export_tasks", token,
            json={"file_extension": "xlsx", "token": sheet_token, "type": "sheet"})
    ticket = d["data"]["ticket"]
    for _ in range(60):
        time.sleep(2)
        q = api("GET", f"/drive/v1/export_tasks/{ticket}", token, params={"token": sheet_token})
        res = q["data"]["result"]
        st = res.get("job_status")
        if st == 0:
            file_token = res["file_token"]
            break
        if st not in (1, 2):
            raise SystemExit(f"导出任务失败 job_status={st} {res.get('job_error_msg')}")
    else:
        raise SystemExit("导出任务超时（120s）")
    r = requests.get(f"{BASE}/drive/v1/export_tasks/file/{file_token}/download",
                     headers={"Authorization": f"Bearer {token}"}, timeout=180)
    if r.status_code != 200:
        raise SystemExit(f"下载失败 {r.status_code}: {r.text[:200]}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(r.content)
    return out


def fetch_sheets(token: str, out_dir: Path) -> None:
    today = date.today().isoformat()
    for prefix, env in SHEETS.items():
        st = os.environ.get(env)
        if not st:
            print(f"跳过 {prefix}：.env 没填 {env}")
            continue
        p = export_sheet(token, st, out_dir / f"{prefix} ({today}).xlsx")
        print(f"{prefix} → {p} ({p.stat().st_size:,} B)")


# ---------- 审批实例（应用身份 tenant_access_token；用户身份 token 不支持 instances/query） ----------
def tenant_token() -> str:
    sec = os.environ.get("LARK_APP_SECRET")
    if not sec:
        raise SystemExit("拿不到应用密钥：请在 .env 填 LARK_APP_SECRET")
    d = requests.post(f"{BASE}/auth/v3/tenant_access_token/internal",
                      json={"app_id": APP_ID, "app_secret": sec}, timeout=30).json()
    if d.get("code") != 0:
        raise SystemExit(f"tenant_access_token 失败: {d}")
    return d["tenant_access_token"]


def _flatten_form(form_json: str) -> dict:
    """审批表单 → 一行：普通控件直接取 value；fieldList（明细/收款方/资金支付信息）取第一组并按 <组名>-<字段名> 展开，
    多组时额外给 <组名>_组数 与 <组名>_json 便于 convert 侧按明细拆行。"""
    row: dict = {}
    try:
        form = json.loads(form_json or "[]")
    except ValueError:
        return row
    for f in form:
        name, typ, val = f.get("name"), f.get("type"), f.get("value")
        if typ == "fieldList" and isinstance(val, list):
            row[f"{name}_组数"] = len(val)
            if val:
                for sub in val[0]:
                    row[f"{name}-{sub.get('name')}"] = sub.get("value")
            if len(val) > 1:
                row[f"{name}_json"] = json.dumps(val, ensure_ascii=False)
        elif isinstance(val, (list, dict)):
            row[name] = json.dumps(val, ensure_ascii=False)
        else:
            row[name] = val
    return row


def fetch_approvals(out_dir: Path, days: int) -> None:
    code = os.environ.get("LARK_APPROVAL_CODE_DIAOBO")
    if not code:
        print("跳过审批：.env 没填 LARK_APPROVAL_CODE_DIAOBO")
        return
    tok = tenant_token()
    tz = business_timezone()
    end = datetime.now(tz)
    start = end - timedelta(days=days)
    items: list[dict] = []
    seen_codes: set[str] = set()
    for win_start, win_end in approval_windows(end, days):
        page_token = None
        while True:
            params = {"page_size": 100, "user_id_type": "open_id"}
            if page_token:
                params["page_token"] = page_token
            d = api("POST", "/approval/v4/instances/query", tok, params=params,
                    json={"approval_code": code,
                          "instance_start_time_from": str(int(win_start.timestamp() * 1000)),
                          "instance_start_time_to": str(int(win_end.timestamp() * 1000))})
            data = d.get("data", {})
            for item in data.get("instance_list", []):
                instance_code = item.get("instance", {}).get("code")
                if instance_code and instance_code not in seen_codes:
                    seen_codes.add(instance_code)
                    items.append(item)
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
    print(f"调拨审批实例 {len(items)} 单（{start:%m-%d} → {end:%m-%d}）")

    st_map = {"PENDING": "审批中", "APPROVED": "已同意", "REJECTED": "已拒绝", "CANCELED": "已撤回", "DELETED": "已删除"}
    rows, raw = [], []
    for i, it in enumerate(items, 1):
        ic = it["instance"]["code"]
        d = api("GET", f"/approval/v4/instances/{ic}", tok, params={"user_id_type": "open_id"})
        inst = d["data"]
        raw.append(inst)
        def ts(v) -> str:
            # Convert with an explicit business timezone, but keep the workbook
            # shape compatible with Lark's timezone-naive manual export.
            return datetime.fromtimestamp(int(v) / 1000, tz).strftime("%Y-%m-%d %H:%M:%S") if v and str(v) != "0" else ""

        def elapsed(start_value, end_value) -> str:
            if not start_value or not end_value or str(end_value) == "0":
                return ""
            return f"{max(0, int(end_value) - int(start_value)) / 1000:g}s"

        rows.append({"申请编号": inst.get("serial_number"), "标题": inst.get("approval_name"),
                     "申请状态": st_map.get(inst.get("status"), inst.get("status")),
                     "发起时间": ts(inst.get("start_time")), "完成时间": ts(inst.get("end_time")),
                     "审批耗时": elapsed(inst.get("start_time"), inst.get("end_time")),
                     "实例code": ic, **_flatten_form(inst.get("form"))})
        if i % 25 == 0:
            print(f"  …{i}/{len(items)}")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "调拨申请_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    import pandas as pd
    p = out_dir / f"调拨申请_api_{date.today().isoformat()}.xlsx"
    pd.DataFrame(rows).to_excel(p, index=False)
    print(f"调拨申请 → {p}（{len(rows)} 单；多明细单见 *_json 列）")


def main() -> None:
    ap = argparse.ArgumentParser(description="Lark 三表 + 调拨审批落地")
    ap.add_argument("--auth", action="store_true", help="起 OAuth 回调服务并打印授权链接")
    ap.add_argument("--sheets", action="store_true", help="只导三张表")
    ap.add_argument("--approvals", action="store_true", help="只拉审批")
    ap.add_argument("--days", type=int, default=30, help="审批回看天数（默认 30）")
    ap.add_argument("--out", help="落地目录（默认 cashflow/data/raw/<今天>）")
    a = ap.parse_args()
    if a.auth:
        do_auth()
        return
    out_dir = Path(a.out) if a.out else DATA_ROOT / "raw" / date.today().isoformat()
    both = not (a.sheets or a.approvals)
    if (a.sheets or both) and (a.sheets or sheet_envs_configured()):
        fetch_sheets(load_token(), out_dir)
    if a.approvals or both:
        fetch_approvals(out_dir, a.days)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
