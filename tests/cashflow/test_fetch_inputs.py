import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import fetch_finweb
import fetch_lark
import ingest_approvals
import pandas as pd
import pytest


def test_totp_secret_is_normalized_before_padding(monkeypatch):
    monkeypatch.setattr(fetch_finweb.time, "time", lambda: 59)

    compact = fetch_finweb.totp_now("JBSWY3DPEHPK3PXP")
    spaced = fetch_finweb.totp_now(" jbsw y3dp ehpk 3pxp ")

    assert spaced == compact


def test_lark_auth_host_matches_api_base():
    assert fetch_lark.auth_host_for_api_base("https://open.feishu.cn/open-apis") == (
        "https://accounts.feishu.cn"
    )
    assert fetch_lark.auth_host_for_api_base("https://open.larksuite.com/open-apis") == (
        "https://accounts.larksuite.com"
    )


def test_tenant_token_requires_env_secret(monkeypatch):
    monkeypatch.delenv("LARK_APP_SECRET", raising=False)

    with pytest.raises(SystemExit, match="LARK_APP_SECRET"):
        fetch_lark.tenant_token()


def test_default_approvals_only_flow_does_not_require_user_token(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(sys, "argv", ["fetch_lark.py", "--out", str(tmp_path)])
    for env in fetch_lark.SHEETS.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("LARK_APPROVAL_CODE_DIAOBO", "approval-code")
    monkeypatch.setattr(fetch_lark, "load_token", lambda: pytest.fail("load_token should not run"))
    monkeypatch.setattr(fetch_lark, "fetch_approvals", lambda out, days: called.append((out, days)))

    fetch_lark.main()

    assert called == [(tmp_path, 30)]


def test_approval_query_splits_windows_paginates_and_dedupes(monkeypatch, tmp_path):
    query_windows = []
    detail_codes = []
    pages = {
        None: (["A", "B"], True, "next"),
        "next": (["B", "C"], False, None),
    }

    monkeypatch.setenv("LARK_APPROVAL_CODE_DIAOBO", "approval-code")
    monkeypatch.setattr(fetch_lark, "tenant_token", lambda: "tenant-token")
    monkeypatch.setattr(fetch_lark, "approval_windows", lambda end, days: [
        (datetime(2026, 1, 1), datetime(2026, 1, 31)),
        (datetime(2026, 1, 31), datetime(2026, 2, 15)),
    ])

    def fake_api(method, path, token, **kwargs):
        if path == "/approval/v4/instances/query":
            page_token = kwargs["params"].get("page_token")
            codes, has_more, next_token = pages[page_token]
            body = kwargs["json"]
            query_windows.append((
                body["instance_start_time_from"],
                body["instance_start_time_to"],
                page_token,
            ))
            return {
                "data": {
                    "instance_list": [{"instance": {"code": code}} for code in codes],
                    "has_more": has_more,
                    "page_token": next_token,
                }
            }
        detail_codes.append(path.rsplit("/", 1)[-1])
        return {
            "data": {
                "serial_number": path.rsplit("/", 1)[-1],
                "approval_name": "调拨",
                "status": "APPROVED",
                "start_time": "1767225600000",
                "end_time": "1767229200000",
                "form": "[]",
            }
        }

    monkeypatch.setattr(fetch_lark, "api", fake_api)

    fetch_lark.fetch_approvals(tmp_path, 45)

    assert len(query_windows) == 4
    assert detail_codes == ["A", "B", "C"]
    exported = pd.read_excel(next(tmp_path.glob("调拨申请_api_*.xlsx")))
    assert exported["发起时间"].tolist() == ["2026-01-01 08:00:00"] * 3


def test_approval_windows_never_exceed_api_limit():
    end = datetime(2026, 3, 2, tzinfo=fetch_lark.ZoneInfo("Asia/Shanghai"))
    windows = fetch_lark.approval_windows(end, 60)

    assert windows[0][0] == end - fetch_lark.timedelta(days=60)
    assert windows[-1][1] == end
    assert all(stop - start <= fetch_lark.timedelta(days=30) for start, stop in windows)


def test_ingest_approvals_accepts_api_and_manual_headers(tmp_path):
    api_file = tmp_path / "api.xlsx"
    manual_file = tmp_path / "manual.xlsx"
    data = pd.DataFrame({"申请编号": ["A-1"], "申请状态": ["已同意"], "金额": [10]})
    data.to_excel(api_file, index=False)
    with pd.ExcelWriter(manual_file) as writer:
        pd.DataFrame([["筛选条件"]]).to_excel(writer, header=False, index=False)
        data.to_excel(writer, startrow=1, index=False)

    assert list(ingest_approvals.read_approval_excel(api_file)["申请编号"]) == ["A-1"]
    assert list(ingest_approvals.read_approval_excel(manual_file)["申请编号"]) == ["A-1"]


def test_oauth_denial_stops_server_and_returns_nonzero(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {
        **os.environ,
        "LARK_APP_ID": "app-id",
        "LARK_APP_SECRET": "secret",
        "LARK_OAUTH_STATE": "expected-state",
        "LARK_OAUTH_REDIRECT": f"http://127.0.0.1:{port}/callback",
        "LARK_USER_TOKEN_FILE": str(tmp_path / "user_token.json"),
        "PYTHONIOENCODING": "utf-8",
    }
    proc = subprocess.Popen(
        [sys.executable, str(Path(fetch_lark.HERE) / "oauth_server.py")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        assert proc.stdout is not None
        assert "listening" in proc.stdout.readline()
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/callback?error=access_denied&state=expected-state",
                timeout=5,
            )
        assert exc.value.code == 400
        stdout, stderr = proc.communicate(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 1
    assert "TOKEN_RESULT" in stdout
    assert stderr == ""
