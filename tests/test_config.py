"""配置模块测试：枚举值、权限矩阵、阈值、Settings 加载。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import (
    AUTHORIZATION_MATRIX,
    Intent,
    Settings,
    Thresholds,
    UserRole,
    get_settings,
    role_can,
)


class TestEnumValues:
    """枚举 .value 必须与 DESIGN.md §4.2.1 TreasuryState 字段字面量完全一致。
    任何重命名都会让路由代码静默失败，这里用硬编码字符串做反向锁定。"""

    def test_user_role_values(self):
        assert UserRole.CASHIER.value == "cashier"
        assert UserRole.TREASURY_SUPERVISOR.value == "treasury_supervisor"
        assert UserRole.TREASURY_MANAGER.value == "treasury_manager"
        assert UserRole.ADMIN.value == "admin"

    def test_intent_values(self):
        expected = {
            "inquiry", "reconciliation", "transfer", "planning",
            "position", "account", "fx", "aml", "investment", "knowledge",
        }
        assert {i.value for i in Intent} == expected


class TestAuthorizationMatrix:
    def test_all_roles_covered(self):
        assert set(AUTHORIZATION_MATRIX.keys()) == set(UserRole)

    def test_cashier_cannot_initiate_high_risk(self):
        for forbidden in [Intent.FX, Intent.AML, Intent.INVESTMENT, Intent.PLANNING]:
            assert not role_can(UserRole.CASHIER, forbidden), (
                f"出纳不应能发起 {forbidden.value}"
            )

    def test_cashier_can_query_and_carry_approved_transfer(self):
        assert role_can(UserRole.CASHIER, Intent.INQUIRY)
        assert role_can(UserRole.CASHIER, Intent.RECONCILIATION)
        assert role_can(UserRole.CASHIER, Intent.KNOWLEDGE)
        # TRANSFER 在矩阵中允许，但 route_by_intent 会用 approved_instruction_id 二次校验。
        assert role_can(UserRole.CASHIER, Intent.TRANSFER)

    def test_supervisor_cannot_fx_aml_investment(self):
        assert not role_can(UserRole.TREASURY_SUPERVISOR, Intent.FX)
        assert not role_can(UserRole.TREASURY_SUPERVISOR, Intent.AML)
        assert not role_can(UserRole.TREASURY_SUPERVISOR, Intent.INVESTMENT)

    def test_supervisor_can_position_planning_account_transfer(self):
        for allowed in [Intent.POSITION, Intent.PLANNING, Intent.ACCOUNT, Intent.TRANSFER]:
            assert role_can(UserRole.TREASURY_SUPERVISOR, allowed)

    def test_manager_has_all_intents(self):
        for intent in Intent:
            assert role_can(UserRole.TREASURY_MANAGER, intent), (
                f"经理应能发起 {intent.value}"
            )

    def test_admin_has_all_intents(self):
        for intent in Intent:
            assert role_can(UserRole.ADMIN, intent)


class TestThresholds:
    def test_large_transfer_matches_design(self):
        # DESIGN.md §4.2.3 route_by_intent 直接引用此常量
        assert Thresholds.LARGE_TRANSFER == Decimal("5000000")

    def test_all_thresholds_are_decimal(self):
        for name in ["LARGE_TRANSFER", "AML_REPORT_CORP", "AML_REPORT_PERSONAL", "CROSS_BORDER_DAILY"]:
            value = getattr(Thresholds, name)
            assert isinstance(value, Decimal), f"{name} 必须是 Decimal，不能是 float"

    def test_thresholds_positive(self):
        for name in ["LARGE_TRANSFER", "AML_REPORT_CORP", "AML_REPORT_PERSONAL", "CROSS_BORDER_DAILY"]:
            assert getattr(Thresholds, name) > Decimal("0")


class TestSettings:
    def test_defaults(self):
        s = Settings()
        assert s.llm_provider in ("openai", "qwen", "deepseek")
        assert s.bank_api_enabled is False
        assert s.fx_market_api_enabled is False
        assert s.payment_mock_mode is True
        assert isinstance(s.qdrant_path, Path)
        assert isinstance(s.industry_kb_path, Path)
        assert s.embedding_model == "BAAI/bge-large-zh-v1.5"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_MODEL", "gpt-4o")
        monkeypatch.setenv("BANK_API_ENABLED", "true")
        s = get_settings()
        assert s.llm_provider == "openai"
        assert s.llm_model == "gpt-4o"
        assert s.bank_api_enabled is True

    def test_get_settings_is_cached(self):
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_invalid_provider_rejected(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "not_a_real_provider")
        with pytest.raises(ValidationError):
            Settings()

    def test_invalid_temperature_rejected(self):
        with pytest.raises(ValidationError):
            Settings(llm_temperature=3.0)

    def test_api_key_is_secret(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-do-not-leak")
        s = Settings()
        # SecretStr 在 repr/str 中应被掩码，防止日志泄露
        assert "sk-do-not-leak" not in repr(s)
        assert "sk-do-not-leak" not in str(s)
        # 但可以通过 get_secret_value() 显式读取
        assert s.llm_api_key.get_secret_value() == "sk-do-not-leak"
