"""knowledge_base/ 是用户放自己文档的目录：仓库里必须是空的，不能自带任何“已批准”的东西。

否则用户把文档放进去，会悄悄继承目录级的 status: approved，“默认待审”的从严约定就失效了。
"""
from __future__ import annotations

import shutil
import subprocess

import pytest


def test_tracked_knowledge_base_is_empty(project_root):
    if shutil.which("git") is None or not (project_root / ".git").exists():
        pytest.skip("非 git 检出，无法列出已跟踪文件")
    out = subprocess.run(
        ["git", "ls-files", "knowledge_base"], cwd=project_root,
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout.split()
    stray = [f for f in out if not f.endswith(".gitkeep")]
    assert not stray, f"knowledge_base/ 里不应有已跟踪的文档或元数据: {stray}"


def test_examples_are_explicitly_approved_not_inherited_by_user_dirs(project_root):
    for track in ("industry", "enterprise"):
        meta = project_root / "examples" / "kb" / track / "_meta.yaml"
        assert meta.is_file() and "status: approved" in meta.read_text(encoding="utf-8")
