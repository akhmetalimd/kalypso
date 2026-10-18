"""
KALYPSO — движок регистра ЛПЗ — переносимая логика, не зависящая от фреймворка.

Реализует три вещи, которые задают «характер» регистра и должны быть одинаковыми
на любой платформе (custom / REDCap-экспорт / OMOP):

  1. canonicalize_lab  — нормализация лабораторных единиц (LOINC + перевод по показателю).
  2. kaplan_meier      — расчёт выживаемости (PFS/OS) с медианой; движок для «песочницы».
  3. validate_record   — контроль качества (те же флаги, что и AI-проверка в прототипе).

Зависимостей нет (только стандартная библиотека). Log-rank и Cox сознательно НЕ здесь —
их считают на выгрузке (lifelines / R), см. README.
"""

from __future__ import annotations
import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "config")

MISSING_CODES = {"not_done", "done_unavailable", "normal_negative", "unknown"}


def _load(name: str) -> dict:
    with open(os.path.join(CONFIG_DIR, name), encoding="utf-8") as f:
        return json.load(f)


LAB_UNITS = _load("lab_units.json")
NOSOLOGY_TREE = _load("nosology_tree.json")


# ---------------------------------------------------------------------------
# 1. Нормализация лабораторных единиц
# ---------------------------------------------------------------------------
@dataclass
class CanonicalLab:
    loinc: str
    value_raw: float
    unit_raw: str
    value_canonical: Optional[float]
    unit_canonical: Optional[str]
    flag: Optional[str] = None  # None = ок; иначе причина для ручной проверки


def canonicalize_lab(loinc: str, value: float, unit: str) -> CanonicalLab:
    """Приводит значение к канонической единице показателя.
    Исходное значение и единица сохраняются всегда; при непознанной единице —
    флаг для ручной проверки (не приводим молча)."""
    spec = LAB_UNITS.get(loinc)
    if spec is None:
        return CanonicalLab(loinc, value, unit, None, None, flag="unknown_loinc")
    factor = spec["conversions"].get(unit)
    if factor is None:
        return CanonicalLab(loinc, value, unit, None, spec["canonical_unit"],
                            flag="unknown_unit")
    return CanonicalLab(loinc, value, unit,
                        round(value * factor, 6), spec["canonical_unit"])


# ---------------------------------------------------------------------------
# 2. Каплан–Майер (для «Исследователя выживаемости»)
# ---------------------------------------------------------------------------
@dataclass
class KMPoint:
    t: float
    survival: float
    at_risk: int
    events: int


def kaplan_meier(durations: Iterable[float], events: Iterable[int]) -> dict:
    """Оценка функции выживания. events: 1 = событие, 0 = цензурирование.
    Возвращает кривую (список точек) и медиану выживаемости."""
    data = sorted(zip(durations, events), key=lambda x: x[0])
    n = len(data)
    if n == 0:
        return {"curve": [], "median": None, "n": 0}

    curve: list[KMPoint] = []
    surv = 1.0
    median = None
    i = 0
    at_risk = n
    while i < n:
        t = data[i][0]
        d = 0          # событий в момент t
        censored = 0
        j = i
        while j < n and data[j][0] == t:
            if data[j][1] == 1:
                d += 1
            else:
                censored += 1
            j += 1
        if d > 0:
            surv *= (1 - d / at_risk)
            curve.append(KMPoint(t, round(surv, 6), at_risk, d))
            if median is None and surv <= 0.5:
                median = t
        at_risk -= (d + censored)
        i = j
    return {
        "curve": [p.__dict__ for p in curve],
        "median": median,
        "n": n,
    }


def survival_by_threshold(rows: list[dict], analyte_key: str, threshold: float) -> dict:
    """Делит когорту по порогу показателя и строит KM для групп «выше»/«не выше».
    rows: [{value, duration, event}]. Возвращает две кривые. Порог берётся из
    литературы/внешней когорты — не подбирается по этим же данным (см. README)."""
    high = [r for r in rows if r.get("value") is not None and r["value"] > threshold]
    low = [r for r in rows if r.get("value") is not None and r["value"] <= threshold]
    return {
        "threshold": threshold,
        "analyte": analyte_key,
        "high": kaplan_meier([r["duration"] for r in high], [r["event"] for r in high]),
        "low": kaplan_meier([r["duration"] for r in low], [r["event"] for r in low]),
        "note": "Ассоциация, не причинность. Log-rank/Cox — на выгрузке. Порог — внешний.",
    }


# ---------------------------------------------------------------------------
# 3. Контроль качества (те же флаги, что и AI-проверка в прототипе)
# ---------------------------------------------------------------------------
def validate_record(rec: dict) -> list[str]:
    """Возвращает список флагов противоречий. Не правит, только помечает."""
    flags = []
    dx = rec.get("dx_date")
    resp = rec.get("response_date")
    ev = rec.get("event_date")
    if dx and resp and resp < dx:
        flags.append("response_before_diagnosis")
    if dx and ev and ev < dx:
        flags.append("event_before_diagnosis")
    if rec.get("status_last_contact") == "умер" and not rec.get("last_contact_date"):
        flags.append("death_without_date")
    stage_sys = rec.get("stage_system")
    who = rec.get("who_haem5")
    if who == "cll_sll" and stage_sys not in (None, "Rai", "Binet"):
        flags.append("staging_system_mismatch")
    if rec.get("event_type") == "смерть" and not rec.get("death_cause"):
        flags.append("death_cause_missing")
    return flags


def find_node(node: dict, node_id: str) -> Optional[dict]:
    """Поиск узла нозологии в дереве (для навигации/наследования кодов)."""
    if node.get("id") == node_id:
        return node
    for ch in node.get("children", []):
        found = find_node(ch, node_id)
        if found:
            return found
    return None


if __name__ == "__main__":
    # Быстрый самотест
    assert canonicalize_lab("8302-2", 14, "g/dL").value_canonical == 140.0
    assert canonicalize_lab("8302-2", 140, "g/L").value_canonical == 140.0
    assert canonicalize_lab("2160-0", 1.0, "mg/dL").value_canonical == 88.4
    assert canonicalize_lab("8302-2", 14, "g/pL").flag == "unknown_unit"

    km = kaplan_meier([5, 10, 10, 20, 25], [1, 0, 1, 1, 0])
    assert km["n"] == 5 and 0 < km["curve"][0]["survival"] < 1

    sb = survival_by_threshold(
        [{"value": 8, "duration": 12, "event": 1},
         {"value": 3, "duration": 30, "event": 0},
         {"value": 9, "duration": 6, "event": 1}],
        "IL-6", 5.0)
    assert sb["high"]["n"] == 2 and sb["low"]["n"] == 1

    flags = validate_record({"dx_date": date(2020, 1, 1), "event_date": date(2019, 1, 1),
                             "event_type": "смерть"})
    assert "event_before_diagnosis" in flags and "death_cause_missing" in flags

    node = find_node(NOSOLOGY_TREE, "cll_sll")
    assert node["status"] == "active" and node["codes"]["mkb10"] == "C91.1"
    print("registry_core self-test OK")
