"""Маппинг записей МИС -> поля регистра по конфигу (PRD §6.3): данные, а не код.

Белый список: на выходе только поля, описанные в конфиге. Всё прочее (ФИО, адрес,
телефон, полис…) отбрасывается. Свободный текст очищается (kalypso.deid_text).
Проблемы маппинга возвращаются списком — такой элемент куратор применить не может:
сначала правится конфиг, затем данные приходят следующим прогоном.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from kalypso.deid_text import deidentify

MAPPING_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "field_mapping"
SECTIONS = ("patient", "diagnosis", "lab", "document")


def load_mapping(name: str = "his_kz") -> dict:
    cfg = json.loads((MAPPING_DIR / f"{name}.json").read_text(encoding="utf-8"))
    for section in SECTIONS:
        for fname, rule in cfg[section].items():
            if "path" not in rule:
                raise ValueError(f"маппинг {section}.{fname}: нет path")
            if "lookup" in rule and rule["lookup"] not in cfg["lookups"]:
                raise ValueError(f"маппинг {section}.{fname}: нет справочника {rule['lookup']}")
    return cfg


@dataclass
class Mapped:
    fields: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    deid_report: dict = field(default_factory=dict)


def get_path(record: dict, path: str):
    cur = record
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _date(raw) -> str:
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    s = str(raw).strip()
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        return date(int(m[3]), int(m[2]), int(m[1])).isoformat()
    return date.fromisoformat(s[:10]).isoformat()


def map_section(cfg: dict, section: str, record: dict) -> Mapped:
    out = Mapped()
    for fname, rule in cfg[section].items():
        raw = get_path(record, rule["path"])
        if raw is None or raw == "":
            if rule.get("required"):
                out.issues.append(f"{fname}: нет значения в источнике ({rule['path']})")
            continue
        try:
            kind = rule.get("type")
            if kind == "date":
                value = _date(raw)
            elif kind == "number":
                value = float(str(raw).replace(",", ".").replace(" ", ""))
            else:
                value = str(raw).strip()
        except (ValueError, TypeError):
            out.issues.append(f"{fname}: не удалось разобрать значение как {rule.get('type')}")
            continue
        if "map" in rule:
            if value not in rule["map"]:
                out.issues.append(f"{fname}: значение «{value}» не описано в маппинге")
                continue
            value = rule["map"][value]
        if "lookup" in rule:
            table = cfg["lookups"][rule["lookup"]]
            if value not in table:
                out.issues.append(f"{fname}: «{value}» нет в справочнике {rule['lookup']}")
                continue
            value = table[value]
        if rule.get("deid"):
            r = deidentify(value)
            value, out.deid_report = r.text, r.report
            if not r.clean:
                out.issues.append(f"{fname}: после очистки остались идентификаторы")
        out.fields[fname] = value
    return out


def included(cfg: dict, patient_record: dict) -> list[dict]:
    """Диагнозы, попадающие под критерии включения (пусто — пациент не выгружается)."""
    inc = cfg["inclusion"]
    diagnoses = get_path(patient_record, inc["diagnoses_path"]) or []
    return [d for d in diagnoses if isinstance(d, dict) and d.get(inc["code_field"]) in inc["codes"]]
