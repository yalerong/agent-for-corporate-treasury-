"""企业资金智能体全局配置。

集中管理角色枚举、权限矩阵、审批阈值、沙箱开关和运行时配置。
所有 Agent / Tool / 路由代码必须从此处 import 常量，禁止重复定义或硬编码。
"""
from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class UserRole(StrEnum):
    CASHIER = "cashier"
    TREASURY_SUPERVISOR = "treasury_supervisor"
    TREASURY_MANAGER = "treasury_manager"
    ADMIN = "admin"


class Intent(StrEnum):
    INQUIRY = "inquiry"
    RECONCILIATION = "reconciliation"
    TRANSFER = "transfer"
    PLANNING = "planning"
    POSITION = "position"
    ACCOUNT = "account"
    FX = "fx"
    AML = "aml"
    INVESTMENT = "investment"
    KNOWLEDGE = "knowledge"


# CASHIER 的 TRANSFER 受 state.approved_instruction_id 二次校验：
# 无已审批指令时 route_by_intent 返回 "reject_unauthorized"。
AUTHORIZATION_MATRIX: dict[UserRole, frozenset[Intent]] = {
    UserRole.CASHIER: frozenset({
        Intent.INQUIRY,
        Intent.RECONCILIATION,
        Intent.KNOWLEDGE,
        Intent.TRANSFER,
    }),
    UserRole.TREASURY_SUPERVISOR: frozenset({
        Intent.INQUIRY,
        Intent.RECONCILIATION,
        Intent.KNOWLEDGE,
        Intent.PLANNING,
        Intent.POSITION,
        Intent.ACCOUNT,
        Intent.TRANSFER,
    }),
    UserRole.TREASURY_MANAGER: frozenset({
        Intent.INQUIRY,
        Intent.RECONCILIATION,
        Intent.KNOWLEDGE,
        Intent.PLANNING,
        Intent.POSITION,
        Intent.ACCOUNT,
        Intent.TRANSFER,
        Intent.FX,
        Intent.AML,
        Intent.INVESTMENT,
    }),
    UserRole.ADMIN: frozenset(Intent),
}


def role_can(role: UserRole, intent: Intent) -> bool:
    return intent in AUTHORIZATION_MATRIX.get(role, frozenset())


class Thresholds:
    """金额阈值，统一以 CNY 为基准；外币由 Tool 调用前折算。"""
    LARGE_TRANSFER: Decimal = Decimal("5000000")
    AML_REPORT_CORP: Decimal = Decimal("200000")
    AML_REPORT_PERSONAL: Decimal = Decimal("50000")
    CROSS_BORDER_DAILY: Decimal = Decimal("10000000")


LLMProvider = Literal["openai", "qwen", "deepseek"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    llm_provider: LLMProvider = "qwen"
    llm_model: str = "qwen-max"
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)

    embedding_model: str = "BAAI/bge-large-zh-v1.5"
    embedding_device: Literal["cpu", "cuda"] = "cpu"

    qdrant_path: Path = Path("./qdrant_data")
    industry_collection: str = "treasury_industry"
    enterprise_collection: str = "treasury_enterprise"

    industry_kb_path: Path = Path("./knowledge_base/industry")
    enterprise_kb_path: Path = Path("./knowledge_base/enterprise")

    bank_api_enabled: bool = False
    fx_market_api_enabled: bool = False
    payment_mock_mode: bool = True

    audit_log_path: Path = Path("./logs/audit.jsonl")
    api_auth_token: SecretStr = SecretStr("")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """运行时配置单例。测试中清除缓存：get_settings.cache_clear()。"""
    return Settings()


__all__ = [
    "UserRole",
    "Intent",
    "AUTHORIZATION_MATRIX",
    "role_can",
    "Thresholds",
    "LLMProvider",
    "Settings",
    "get_settings",
]
