"""Bearer-token authentication helpers for the FastAPI boundary."""
from __future__ import annotations

import secrets
from collections.abc import Iterable

from pydantic import SecretStr

from app.config import Settings, UserRole


def configured_api_tokens(settings: Settings) -> Iterable[tuple[UserRole, SecretStr]]:
    return (
        (UserRole.CASHIER, settings.api_cashier_token),
        (UserRole.TREASURY_SUPERVISOR, settings.api_supervisor_token),
        (UserRole.TREASURY_MANAGER, settings.api_manager_token),
        (UserRole.ADMIN, settings.api_admin_token),
    )


def role_for_bearer_token(token: str, settings: Settings) -> UserRole | None:
    for role, secret in configured_api_tokens(settings):
        expected = secret.get_secret_value()
        if expected and secrets.compare_digest(token, expected):
            return role
    return None
