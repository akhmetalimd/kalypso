"""Журналы без ИИН: любое отдельно стоящее 12-значное число в записи журнала -> ***.

Сервисы не пишут ИИН в журнал, но текст ошибки БД может его содержать (например,
DETAIL нарушения уникальности patient_iin_key). Фильтр стоит на всех обработчиках
журнала сервера (см. log_config) и маскирует сообщение, аргументы и трассировку.
"""
from __future__ import annotations

import copy
import logging
import re

_IIN = re.compile(r"(?<!\d)\d{12}(?!\d)")
MASK = "***"


def mask(text: str) -> str:
    return _IIN.sub(MASK, text)


def _mask_arg(value):
    return mask(value) if isinstance(value, str) else value


class MaskIIN(logging.Filter):
    """Маскирует ИИН, не ломая форматтеры: аргументы остаются кортежем/словарём
    (форматтер журнала доступа uvicorn разбирает args по позициям)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = mask(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_mask_arg(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _mask_arg(v) for k, v in record.args.items()}
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = mask(record.exc_text)
        if record.stack_info:
            record.stack_info = mask(record.stack_info)
        return True


def log_config() -> dict:
    """Конфигурация журналов uvicorn + журнал приложения, фильтр — на каждом обработчике."""
    from uvicorn.config import LOGGING_CONFIG

    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg.setdefault("filters", {})["mask_iin"] = {"()": "kalypso.logsafe.MaskIIN"}
    for handler in cfg["handlers"].values():
        handler.setdefault("filters", []).append("mask_iin")
    cfg["loggers"]["kalypso"] = {"handlers": ["default"], "level": "INFO", "propagate": False}
    return cfg
