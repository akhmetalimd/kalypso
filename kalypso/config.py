"""Настройки из окружения. Секреты — только из окружения, не из репозитория.

KALYPSO_DSN               строка подключения (логин — член роли kalypso_app)
KALYPSO_SESSION_SECRET    секрет для CSRF-токенов, ≥ 32 байт
KALYPSO_SECURE_COOKIES    1/0 — cookie только по HTTPS (по умолчанию 1)
KALYPSO_SESSION_HOURS     срок сессии в часах (по умолчанию 8)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    dsn: str
    session_secret: bytes = field(repr=False)
    secure_cookies: bool = True
    session_hours: int = 8


def _secret(env, name: str) -> bytes:
    value = env.get(name, "").encode("utf-8")
    if len(value) < 32:
        raise ConfigError(f"{name} не задан или короче 32 байт")
    return value


def load_settings(env=None) -> Settings:
    env = os.environ if env is None else env
    dsn = env.get("KALYPSO_DSN")
    if not dsn:
        raise ConfigError("KALYPSO_DSN не задан")
    return Settings(
        dsn=dsn,
        session_secret=_secret(env, "KALYPSO_SESSION_SECRET"),
        secure_cookies=env.get("KALYPSO_SECURE_COOKIES", "1") != "0",
        session_hours=int(env.get("KALYPSO_SESSION_HOURS", "8")),
    )
