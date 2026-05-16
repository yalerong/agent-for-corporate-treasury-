"""MaskingMap 测试：映射稳定性、分类隔离、unmask 反查。"""
from __future__ import annotations

import pytest

from app.tools.masking import MaskingMap, get_masking_map


class TestMaskingMap:
    def test_mask_returns_code_format(self):
        m = MaskingMap()
        code = m.mask("6222021234567890", "ACC")
        assert code == "ACC-0001"

    def test_same_real_returns_same_code(self):
        m = MaskingMap()
        c1 = m.mask("6222021234567890", "ACC")
        c2 = m.mask("6222021234567890", "ACC")
        assert c1 == c2

    def test_different_reals_get_different_codes(self):
        m = MaskingMap()
        c1 = m.mask("ACC-A", "ACC")
        c2 = m.mask("ACC-B", "ACC")
        assert c1 != c2

    def test_counters_independent_per_category(self):
        m = MaskingMap()
        m.mask("real-acc", "ACC")
        m.mask("real-cust", "CUST")
        assert m.mask("real-acc-2", "ACC") == "ACC-0002"
        assert m.mask("real-cust-2", "CUST") == "CUST-0002"

    def test_unmask_roundtrip(self):
        m = MaskingMap()
        code = m.mask("6222021234567890", "ACC")
        assert m.unmask(code) == "6222021234567890"

    def test_unmask_unknown_returns_none(self):
        m = MaskingMap()
        assert m.unmask("ACC-9999") is None

    def test_empty_real_raises(self):
        m = MaskingMap()
        with pytest.raises(ValueError):
            m.mask("", "ACC")

    def test_clear_resets_state(self):
        m = MaskingMap()
        m.mask("x", "ACC")
        m.clear()
        # 重新 mask 应从 0001 开始
        assert m.mask("y", "ACC") == "ACC-0001"

    def test_singleton_via_get_masking_map(self):
        get_masking_map.cache_clear()
        a = get_masking_map()
        b = get_masking_map()
        assert a is b
        get_masking_map.cache_clear()
