"""Настройки из окружения. Секреты — только из окружения, не из репозитория.

KALYPSO_DSN               строка подключения (логин — член роли kalypso_app)
KALYPSO_HMAC_KEY          ключ хэша ИИН, ≥ 32 байт; один на все филиалы, не менять
KALYPSO_HASH_KEY_VERSION  версия ключа (по умолчанию 1)
KALYPSO_SESSION_SECRET    секрет для CSRF-токенов, ≥ 32 байт
KALYPSO_SECURE_COOKIES    1/0 — cookie только по HTTPS (по умолчанию 1)
KALYPSO_SESSION_HOURS     срок сессии в часах (по умолчанию 8)
KALYPSO_AI_EXTRACTOR      off | rules | claude (по умолчанию off: все документы — в ручную очередь)
KALYPSO_AI_MODEL          модель для claude (по умолчанию claude-opus-5-5)
KALYPSO_AI_EFFORT         low | medium | high (по умолчанию medium)
ANTHROPIC_API_KEY         ключ Anthropic — только на сервере, для KALYPSO_AI_EXTRACTOR=claude
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    dsn: str
    hmac_key: bytes = field(repr=False)
    session_secret: bytes = field(repr=False)
    hash_key_version: int = 1
    secure_cookies: bool = True
    session_hours: int = 8
    ai_extractor: str = "off"
    ai_model: str | None = None
    ai_effort: str = "medium"

    @property
    def ai_enabled(self) -> bool:
        return self.ai_extractor != "off"


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
        hmac_key=_secret(env, "KALYPSO_HMAC_KEY"),
        session_secret=_secret(env, "KALYPSO_SESSION_SECRET"),
        hash_key_version=int(env.get("KALYPSO_HASH_KEY_VERSION", "1")),
        secure_cookies=env.get("KALYPSO_SECURE_COOKIES", "1") != "0",
        session_hours=int(env.get("KALYPSO_SESSION_HOURS", "8")),
        ai_extractor=_choice(env, "KALYPSO_AI_EXTRACTOR", ("off", "rules", "claude"), "off"),
        ai_model=env.get("KALYPSO_AI_MODEL") or None,
        ai_effort=_choice(env, "KALYPSO_AI_EFFORT", ("low", "medium", "high"), "medium"),
    )


def _choice(env, name: str, allowed: tuple[str, ...], default: str) -> str:
    value = env.get(name, default)
    if value not in allowed:
        raise ConfigError(f"{name}: одно из {', '.join(allowed)}")
    return value
