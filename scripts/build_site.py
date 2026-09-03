"""把 `docs/` 里的演示页拼成一个干净的部署目录（Cloudflare Pages）。

存在的理由不是"构建"（这些页面是自包含单文件），是两件手工做不可靠的事：

1. **线上的首页是 `docs/multi-agent.html`，不是 `docs/index.html`。**
   部署时要改名，靠记的话迟早传错一张页面上去。
2. **别整目录传。** `docs/data.json` 是给 `docs/index.html`（另一张、当前没上线的页）
   用的，不该出现在公网上；`_headers` 反过来必须传上去，漏掉它安全头就静默消失。

用法：

    python scripts/build_site.py [输出目录]     # 默认 _site

然后：

    npx wrangler pages deploy _site --project-name treasury-demo --branch main
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

# 部署清单：(仓库里的文件, 部署后的名字)。
# 加条目要同时想清楚它该不该被公网看见。
FILES = (
    ("multi-agent.html", "index.html"),
    ("robots.txt", "robots.txt"),
    ("404.html", "404.html"),
    ("_headers", "_headers"),
)


def guard_output_dir(out: Path) -> None:
    """构建前会 rmtree 输出目录，所以先确认它不是个能删坏东西的路径。

    `python scripts/build_site.py docs` 会删掉演示页源文件本身，
    这个参数看着挺自然，不能靠使用者小心。
    """
    protected = {ROOT, DOCS, ROOT / "app", ROOT / "cashflow", ROOT / "scripts",
                 ROOT / "tests", ROOT / ".git"}
    if out in protected:
        raise SystemExit(f"拒绝：{out} 是源目录，构建会先删空它")
    if out == Path(out.anchor):
        raise SystemExit(f"拒绝：{out} 是盘符/根目录")
    if ROOT.is_relative_to(out):
        raise SystemExit(f"拒绝：{out} 包含着仓库本身，删它会删掉源码")


def check_page(html: str) -> None:
    """页面与 `_headers` 的一致性，不成立就该在部署前炸掉，而不是线上才发现。"""
    # CSP 只放行同源，页面引到任何外部资源都会被浏览器静默挡掉。
    # 本地直接双击打开 HTML 时不发 CSP，所以这类回归在本地看不出来。
    external = set()
    for match in re.finditer(r"""(?:src)\s*=\s*["'](https?://[^"'/]+)""", html):
        external.add(match.group(1))
    if external:
        raise SystemExit(
            "演示页引了外部资源，CSP 只放行同源，部署上去会被静默挡掉：\n  "
            + "\n  ".join(sorted(external))
            + "\n要么把资源内联/同源化，要么同时放宽 docs/_headers 里的 CSP。"
        )
    # 没有 'unsafe-eval'，这几个 API 会被 CSP 拒掉
    for pattern in (r"\bnew Function\s*\(", r"(?<![\w.])eval\s*\("):
        if re.search(pattern, html):
            raise SystemExit(f"演示页里有 {pattern}，但 CSP 没给 'unsafe-eval'")


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "_site").resolve()
    guard_output_dir(out)

    missing = [src for src, _ in FILES if not (DOCS / src).exists()]
    if missing:
        raise SystemExit("缺文件：" + "、".join(f"docs/{name}" for name in missing))

    check_page((DOCS / "multi-agent.html").read_text(encoding="utf-8"))

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for src, dst in FILES:
        shutil.copy(DOCS / src, out / dst)

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"输出目录: {out}")
    print(f"文件数: {len(FILES)}，合计 {size / 1024:.0f} KB")
    print("首页来源: docs/multi-agent.html")
    print("部署: npx wrangler pages deploy " + str(out)
          + " --project-name treasury-demo --branch main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
