"""演示站（treasury-demo.pages.dev）的构建与响应头闸门。

这个站是纯静态、无后端、数据全合成，所以这里钉的不是数据安全，而是三件
"本地看不出来、部署上去才发现"的事：

- `_headers` 有没有被拼进部署产物——漏掉它安全头就静默消失，页面照常打开；
- 首页取的是不是 `docs/multi-agent.html`——线上首页是它，不是 `docs/index.html`；
- 页面有没有引到 CSP 放行不了的东西——本地双击打开 HTML 不发 CSP，加个 CDN 脚本
  在本地一切正常，线上才发现被挡。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "scripts" / "build_site.py"
DOCS = ROOT / "docs"


def _load_build_module():
    spec = importlib.util.spec_from_file_location("build_site", BUILD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD_SITE = _load_build_module()


@pytest.fixture(scope="module")
def site(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("site") / "build"
    proc = subprocess.run(
        [sys.executable, str(BUILD), str(out)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
        # 构建脚本的提示是中文，子进程按控制台编码写出去、这边按 utf-8 解会失败，
        # subprocess 会把 stdout/stderr 留成 None（Windows 上必挂）。钉死编码。
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, proc.stderr
    return out


def test_deploy_bundle_has_exactly_the_files_it_should(site: Path):
    got = sorted(p.name for p in site.iterdir())
    assert got == ["404.html", "_headers", "index.html", "robots.txt"]


def test_index_comes_from_the_multi_agent_page(site: Path):
    # 线上首页是 docs/multi-agent.html。docs/index.html 是另一张页（当前没上线，
    # 而且它 fetch data.json，那份数据不在部署清单里）。传错会整站换一张页。
    assert (site / "index.html").read_bytes() == (DOCS / "multi-agent.html").read_bytes()


def test_data_json_never_ships(site: Path):
    assert not (site / "data.json").exists(), "data.json 不在部署清单里，不该上公网"


def test_security_headers_cover_every_path(site: Path):
    text = (site / "_headers").read_text(encoding="utf-8")
    assert text.split("\n", 1)[0] == "/*", "规则行必须是 /*，否则头只盖到某个子路径"
    for header in ("Content-Security-Policy", "X-Content-Type-Options",
                   "X-Frame-Options", "Referrer-Policy", "X-Robots-Tag"):
        assert f"  {header}: " in text, f"缺 {header}"
    # 这条是这份 _headers 里唯一挡得住实际攻击的：别人把演示站嵌进自己页面做
    # 点击劫持。写错成 'self' 的话文件照样在、其余断言照样过。
    assert "frame-ancestors 'none'" in text


def test_page_loads_nothing_the_csp_would_block(site: Path):
    """CSP 是 `'self'` 单源且没给 `'unsafe-eval'`。"""
    html = (site / "index.html").read_text(encoding="utf-8")
    assert "https://cdn." not in html and "http://" not in html.replace("http://www.w3.org", "")
    assert "new Function(" not in html
    assert "eval(" not in html


@pytest.mark.parametrize(
    "page",
    (
        '<link rel="stylesheet" href="https://cdn.example/style.css">',
        '<img src="//cdn.example/image.png">',
        '<img srcset="/local.png 1x, https://cdn.example/remote.png 2x">',
        '<style>.hero { background: url(https://cdn.example/hero.png) }</style>',
        '<script>fetch("https://api.example/data")</script>',
        '<script type="module">import "//cdn.example/module.js"</script>',
    ),
)
def test_csp_preflight_rejects_external_resource_patterns(page: str):
    with pytest.raises(SystemExit, match="CSP"):
        BUILD_SITE.check_page(page)


def test_build_refuses_to_wipe_source_directories():
    """构建前会 rmtree 输出目录，所以危险参数必须在删之前就被拒。"""
    for arg in ("docs", ".", "scripts"):
        proc = subprocess.run(
            [sys.executable, str(BUILD), arg],
            capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        assert proc.returncode != 0, f"传 {arg} 居然构建成功了"
        assert "拒绝" in (proc.stdout + proc.stderr)
    assert (DOCS / "multi-agent.html").exists(), "源文件被删了"


def test_build_allows_fresh_custom_output_but_preserves_existing_one(tmp_path: Path):
    fresh = tmp_path / "fresh-site"
    proc = subprocess.run(
        [sys.executable, str(BUILD), str(fresh)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, proc.stderr

    marker = fresh / "do-not-delete"
    marker.write_text("keep", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(BUILD), str(fresh)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode != 0
    assert marker.read_text(encoding="utf-8") == "keep"


def test_build_rebuilds_only_its_dedicated_default_output(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    for name, content in (
        ("multi-agent.html", "<main>safe</main>"),
        ("robots.txt", "User-agent: *\nAllow: /\n"),
        ("404.html", "not found"),
        ("_headers", "/*\n"),
    ):
        (docs / name).write_text(content, encoding="utf-8")

    out = repo / "_site"
    out.mkdir()
    (out / "old-output").write_text("replace me", encoding="utf-8")
    monkeypatch.setattr(BUILD_SITE, "ROOT", repo)
    monkeypatch.setattr(BUILD_SITE, "DOCS", docs)
    monkeypatch.setattr(BUILD_SITE, "DEFAULT_OUTPUT", out)
    monkeypatch.setattr(sys, "argv", [str(BUILD), str(out)])

    assert BUILD_SITE.main() == 0
    assert not (out / "old-output").exists()
    assert (out / "index.html").read_text(encoding="utf-8") == "<main>safe</main>"


def test_build_refuses_fresh_repo_descendant():
    out = ROOT / "docs" / "build-output"
    proc = subprocess.run(
        [sys.executable, str(BUILD), str(out)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode != 0
    assert not out.exists()


def test_robots_allows_crawlers_to_receive_the_noindex_header(site: Path):
    robots = (site / "robots.txt").read_text(encoding="utf-8")
    assert "Allow: /" in robots
    assert "Disallow: /" not in robots
