"""
KALYPSO — движок регистра ЛПЗ — переносимая логика, не зависящая от фреймворка.

Реализует три вещи, которые задают «характер» регистра и должны быть одинаковыми
на любой платформе (custom / REDCap-экспорт / OMOP):

  1. canonicalize_lab  — нормализация лабораторных единиц (LOINC + перевод по показателю).
  2. kaplan_meier      — расчёт выживаемости (PFS/OS) с медианой; движок для «песочницы».
  3. validate_record   — контроль качества (те же флаги, что и AI-проверка в прототипе).
  4. validate_codes / completeness_flags — связка кодов WHO5 -> ICD-O -> МКБ и
     полнота обязательного ядра нозологии (FR-1, FR-7).

Зависимостей нет (только стандартная библиотека). Log-rank и Cox сознательно НЕ здесь —
их считают на выгрузке (lifelines / R), см. README.
"""

from __future__ import annotations
import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "config")

MISSING_CODES = {"not_done", "done_unavailable", "normal_negative", "unknown"}


def _load(name: str) -> dict:
    with open(os.path.join(CONFIG_DIR, name), encoding="utf-8") as f:
        return json.load(f)


LAB_UNITS = _load("lab_units.json")
NOSOLOGY_TREE = _load("nosology_tree.json")


def _as_date(value) -> Optional[date]:
    """Приводит дату к date: принимает date, datetime и ISO-строку 'YYYY-MM-DD'.
    Пустое значение -> None; нераспознанная строка -> ValueError (не сравниваем молча)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value.strip())
    raise ValueError(f"не дата: {value!r}")


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
    if not isinstance(spec, dict):  # нет показателя или служебный ключ вроде "_comment"
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
    Возвращает кривую (список точек) и медиану выживаемости.
    Отрицательное время (событие раньше time-zero), событие не 0/1 или разная длина
    списков -> ValueError: это ошибка данных, её нужно исправить, а не усреднить."""
    durations, events = list(durations), list(events)
    if len(durations) != len(events):
        raise ValueError(f"durations ({len(durations)}) и events ({len(events)}) разной длины")
    for d, e in zip(durations, events):
        if d < 0:
            raise ValueError(f"отрицательное время наблюдения: {d}")
        if e not in (0, 1):
            raise ValueError(f"событие должно быть 0 или 1, получено {e!r}")
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
    dx = _as_date(rec.get("dx_date"))
    resp = _as_date(rec.get("response_date"))
    ev = _as_date(rec.get("event_date"))
    if dx and resp and resp < dx:
        flags.append("response_before_diagnosis")
    if dx and ev and ev < dx:
        flags.append("event_before_diagnosis")
    if rec.get("status_last_contact") == "dead" and not rec.get("last_contact_date"):
        flags.append("death_without_date")
    stage_sys = rec.get("stage_system")
    who = rec.get("who_haem5")
    if who == "cll_sll" and stage_sys not in (None, "Rai", "Binet"):
        flags.append("staging_system_mismatch")
    if rec.get("event_type") == "death" and not rec.get("death_cause"):
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


def _node_path(node: dict, node_id: str) -> Optional[list[dict]]:
    """Путь от корня до узла (включительно) или None."""
    if node.get("id") == node_id:
        return [node]
    for ch in node.get("children", []):
        path = _node_path(ch, node_id)
        if path:
            return [node] + path
    return None


def resolve_codes(node_id: str) -> Optional[dict]:
    """Коды узла с наследованием: ребёнок переопределяет коды родителя."""
    path = _node_path(NOSOLOGY_TREE, node_id)
    if path is None:
        return None
    codes: dict = {}
    for n in path:
        codes.update(n.get("codes") or {})
    return codes


def _norm_icd_o(code: Optional[str]) -> str:
    """'M-9823/3', 'M9823/3', ' 9823/3 ' -> '9823/3'."""
    c = (code or "").strip().upper()
    if c.startswith("M-"):
        c = c[2:]
    elif c.startswith("M"):
        c = c[1:]
    return c


def _mkb_matches(mkb: str, ref: str) -> bool:
    """МКБ сверяется по префиксу рубрики: узел C82 допускает C82.0 … C82.9."""
    return mkb == ref or mkb.startswith(ref + ".")


def validate_codes(rec: dict) -> list[str]:
    """Сверяет связку WHO-HAEM5 -> ICD-O-3.2 -> МКБ-10 с кодами узла дерева.
    Допустимы основная пара кодов узла и альтернативные (alt_codes: например,
    ЛМЛ внутри ХЛЛ/ЛМЛ). Возвращает флаги; не правит."""
    who = rec.get("who_haem5")
    if not who:
        return ["who_haem5_missing"]
    node = find_node(NOSOLOGY_TREE, who)
    if node is None:
        return ["unknown_who_haem5"]
    flags = []
    if node.get("status") != "active":
        flags.append("nosology_not_active")

    pairs = [resolve_codes(who) or {}] + list(node.get("alt_codes", []))
    pairs = [p for p in pairs if p.get("icd_o_3_2") or p.get("mkb10")]
    if not pairs:
        return flags
    icd_o = _norm_icd_o(rec.get("icd_o_3_2"))
    mkb = (rec.get("mkb") or "").strip().upper()
    icd_ok = [p for p in pairs if p.get("icd_o_3_2") in (None, icd_o)]
    mkb_ok = [p for p in pairs if p.get("mkb10") is None or _mkb_matches(mkb, p["mkb10"])]
    if not icd_ok:
        flags.append("icd_o_mismatch")
    if not mkb_ok:
        flags.append("mkb_mismatch")
    if icd_ok and mkb_ok and not any(p in mkb_ok for p in icd_ok):
        flags.append("code_pair_mismatch")  # каждый код допустим, но из разных пар
    return flags


def completeness_flags(rec: dict) -> list[str]:
    """FR-7: флаги незаполненных полей обязательного ядра нозологии.
    Поле заполнено, если есть значение ИЛИ код пропуска (MISSING_CODES);
    флаг ставится, только когда нет ни того, ни другого.

    rec: who_haem5, status_last_contact,
         module_data   {field_key: значение | код пропуска},
         therapy_lines [{line_id, line_no, end_date}],
         observations  [{line_id, response_value, event_type, death_cause}]."""
    flags = []
    node = find_node(NOSOLOGY_TREE, rec.get("who_haem5") or "") or {}
    md = rec.get("module_data") or {}
    for field in node.get("core_fields", []):
        if md.get(field["key"]) in (None, ""):
            flags.append(f"missing_{field['key']}")

    obs = rec.get("observations") or []
    for line in rec.get("therapy_lines") or []:
        if not line.get("end_date"):
            continue  # линия продолжается — оценки ответа ещё может не быть
        if not any(o.get("line_id") == line.get("line_id") and o.get("response_value")
                   for o in obs):
            flags.append(f"missing_response_line_{line.get('line_no')}")

    if rec.get("status_last_contact") == "dead":
        deaths = [o for o in obs if o.get("event_type") == "death"]
        if not deaths or not deaths[0].get("death_cause"):
            flags.append("missing_death_cause")
    return flags


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
                             "event_type": "death"})
    assert "event_before_diagnosis" in flags and "death_cause_missing" in flags

    node = find_node(NOSOLOGY_TREE, "cll_sll")
    assert node["status"] == "active" and node["codes"]["mkb10"] == "C91.1"

    assert "event_before_diagnosis" in validate_record(
        {"dx_date": date(2020, 1, 1), "event_date": "2019-01-01"})          # str vs date
    assert validate_codes({"who_haem5": "cll_sll", "icd_o_3_2": "M-9823/3", "mkb": "C91.1"}) == []
    assert "mkb_mismatch" in validate_codes(
        {"who_haem5": "cll_sll", "icd_o_3_2": "9823/3", "mkb": "C83.3"})
    assert "missing_TP53" in completeness_flags({"who_haem5": "cll_sll", "module_data": {}})
    assert canonicalize_lab("_comment", 1, "x").flag == "unknown_loinc"
    try:
        kaplan_meier([-1], [1])
        raise AssertionError("отрицательное время должно отвергаться")
    except ValueError:
        pass
    print("registry_core self-test OK")
