"""Подписи для кодов, флагов и сообщений. В БД — коды, здесь — их названия (ru, kk)."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

LOCALES = Path(__file__).resolve().parent / "locales"
DEFAULT = "ru"


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if k.startswith("_"):
            continue
        out[k] = _merge(base.get(k, {}), v) if isinstance(v, dict) else v
    return out


@lru_cache
def catalog(lang: str = DEFAULT) -> dict:
    base = json.loads((LOCALES / f"{DEFAULT}.json").read_text(encoding="utf-8"))
    if lang == DEFAULT:
        return base
    over = json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
    return _merge(base, over)


def options(group: str, lang: str = DEFAULT) -> dict[str, str]:
    """Код -> подпись для выпадающих списков."""
    return catalog(lang)["codes"][group]


def label(group: str, code, lang: str = DEFAULT) -> str:
    if code is None or code == "":
        return "—"
    return catalog(lang)["codes"].get(group, {}).get(str(code), str(code))


@lru_cache
def _field_labels() -> dict[str, str]:
    """Подписи полей модулей из дерева нозологий — для флагов «нет поля» без отдельной строки."""
    import registry_core as core

    out: dict[str, str] = {}

    def walk(n):
        for f in n.get("core_fields", []):
            out.setdefault(f["key"], f["label_ru"])
        for ch in n.get("children", []):
            walk(ch)
    walk(core.NOSOLOGY_TREE)
    return out


def flag(code: str, lang: str = DEFAULT) -> str:
    flags = catalog(lang)["flags"]
    if code in flags:
        return flags[code]
    if code.startswith("missing_response_line_"):
        return flags["missing_response_line"].format(n=code.rsplit("_", 1)[-1])
    if code.startswith("missing_") and code[len("missing_"):] in _field_labels():
        return f"Нет «{_field_labels()[code[len('missing_'):]]}» (значения или кода пропуска)"
    return flags.get(code, code)


def msg(code: str | None, lang: str = DEFAULT) -> str | None:
    if not code:
        return None
    return catalog(lang)["msg"].get(code)
