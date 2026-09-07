"""一次性 Lark OAuth 回调：在 :3000/callback 收 code，换 user_access_token 写到同目录 user_token.json。

凭据从环境变量读（.env：LARK_APP_ID / LARK_APP_SECRET），本文件不含真值。
通常不直接跑，由 `fetch_lark.py --auth` 拉起并打印授权链接。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.parse
import urllib.request
from contextlib import suppress
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
EXPECTED_STATE = os.environ.get("LARK_OAUTH_STATE")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        q = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(q.query))
        if q.path != urllib.parse.urlparse(REDIRECT).path:
            self.send_response(404)
            self.end_headers()
            return
        if params.get("state") != EXPECTED_STATE:
            self._finish(False, "OAuth state 校验失败")
            return
        if params.get("error"):
            self._finish(False, "授权失败: " + params["error"])
            return
        if "code" not in params:
            self._finish(False, "回调缺少 code")
            return
        body = json.dumps({"grant_type": "authorization_code", "client_id": APP_ID,
                           "client_secret": APP_SECRET, "code": params["code"],
                           "redirect_uri": REDIRECT}).encode()
        req = urllib.request.Request(f"{BASE}/authen/v2/oauth/token", body,
                                     {"Content-Type": "application/json"})
        resp = json.load(urllib.request.urlopen(req))
        ok = "access_token" in resp
        if ok:
            OUT.write_text(json.dumps(resp, ensure_ascii=False, indent=1), encoding="utf-8")
        self._finish(ok, "授权成功，可以关掉此页" if ok else "换取 token 失败: " + json.dumps(resp, ensure_ascii=False))

    def _finish(self, ok: bool, message: str) -> None:
        self.server.auth_ok = ok
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(message.encode())
        print("TOKEN_RESULT:", "OK" if ok else message, flush=True)
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, *a) -> None:  # 静音访问日志
        pass


if __name__ == "__main__":
    if not (APP_ID and APP_SECRET):
        sys.exit("缺 LARK_APP_ID / LARK_APP_SECRET（.env）")
    if not EXPECTED_STATE:
        sys.exit("缺 LARK_OAUTH_STATE；请通过 fetch_lark.py --auth 启动")
    u = urllib.parse.urlparse(REDIRECT)
    srv = HTTPServer((u.hostname or "127.0.0.1", u.port or 3000), Handler)
    srv.auth_ok = False
    print(f"listening on :{u.port or 3000}", flush=True)
    with suppress(KeyboardInterrupt):
        srv.serve_forever()
    sys.exit(0 if srv.auth_ok else 1)
