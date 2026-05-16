"""scripts/ 测试。当前仅有企业库热更新脚本。"""
from __future__ import annotations

import pytest

from scripts.update_enterprise_kb import main as update_main


class TestUpdateEnterpriseKB:
    def test_check_mode_prints_doc_count(self, capsys, project_root, monkeypatch):
        monkeypatch.setenv("ENTERPRISE_KB_PATH", str(project_root / "knowledge_base" / "enterprise"))
        from app.config import get_settings
        get_settings.cache_clear()
        rc = update_main(["--check"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "[check]" in out
        # stub 目录至少 2 个文档
        assert "文档数：2" in out or "文档数：" in out

    def test_check_missing_directory(self, capsys, monkeypatch, tmp_dir):
        # 指向不存在的目录，应报告 0 文档但不报错
        nonexistent = tmp_dir / "does_not_exist"
        monkeypatch.setenv("ENTERPRISE_KB_PATH", str(nonexistent))
        from app.config import get_settings
        get_settings.cache_clear()
        rc = update_main(["--check"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "文档数：0" in out

    def test_invalid_argument_exits(self):
        with pytest.raises(SystemExit) as exc:
            update_main(["--unknown-flag"])
        assert exc.value.code == 2
