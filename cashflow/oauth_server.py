"""一次性 Lark OAuth 回调：在 :3000/callback 收 code，换 user_access_token 写到同目录 user_token.json。

凭据从环境变量读（.env：LARK_APP_ID / LARK_APP_SECRET），本文件不含真值。
通常不直接跑，由 `fetch_lark.py --auth` 拉起并打印授权链接。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE.parent / ".env")
BASE = os.environ.get("LARK_API_BASE", "https://open.larksuite.com/open-apis")
APP_ID = os.environ.get("LARK_APP_ID", "")
APP_SECRET = os.environ.get("LARK_APP_SECRET", "")
REDIRECT = os.environ.get("LARK_OAUTH_REDIRECT", "http://localhost:3000/callback")
OUT = Path(os.environ.get("LARK_USER_TOKEN_FILE") or HERE / "user_token.json")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        q = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(q.query))
        if q.path != urllib.parse.urlparse(REDIRECT).path or "code" not in params:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({"grant_type": "authorization_code", "client_id": APP_ID,
                           "client_secret": APP_SECRET, "code": params["code"],
                           "redirect_uri": REDIRECT}).encode()
        req = urllib.request.Request(f"{BASE}/authen/v2/oauth/token", body,
                                     {"Content-Type": "application/json"})
        resp = json.load(urllib.request.urlopen(req))
        OUT.write_text(json.dumps(resp, ensure_ascii=False, indent=1), encoding="utf-8")
        ok = "access_token" in resp
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(("授权成功，可以关掉此页" if ok else "换取 token 失败: " + json.dumps(resp)).encode())
        print("TOKEN_RESULT:", "OK" if ok else resp, flush=True)
        raise KeyboardInterrupt  # 一次性：收完即停

    def log_message(self, *a) -> None:  # 静音访问日志
        pass


if __name__ == "__main__":
    if not (APP_ID and APP_SECRET):
        sys.exit("缺 LARK_APP_ID / LARK_APP_SECRET（.env）")
    u = urllib.parse.urlparse(REDIRECT)
    srv = HTTPServer((u.hostname or "127.0.0.1", u.port or 3000), Handler)
    print(f"listening on :{u.port or 3000}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
