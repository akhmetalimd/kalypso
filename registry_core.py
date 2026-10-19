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
from datetime import date, datetime, timedelta
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
    node = find_node(NOSOLOGY_TREE, rec.get("who_haem5") or "") or {}
    if node.get("staging") and stage_sys not in (None, *node["staging"]):
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
        if field.get("optional"):
            continue  # дополнительное поле: пустое — не пропуск ядра
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


# ---------------------------------------------------------------------------
# 5. Конечные точки выживаемости (П3) — одинаковые для платформы и выгрузки
# ---------------------------------------------------------------------------
def impute_date(value, precision: Optional[str] = "day") -> Optional[date]:
    """Неточная дата -> середина периода (месяц -> 15-е, год -> 1 июля). Только для расчёта."""
    d = _as_date(value)
    if d is None:
        return None
    if precision == "month":
        return d.replace(day=15)
    if precision == "year":
        return date(d.year, 7, 1)
    return d


ENDPOINTS = {
    "OS": "Общая выживаемость: от диагноза до смерти от любой причины",
    "TTFT": "Время до первой терапии: от диагноза до старта 1-й линии (смерть без лечения — цензура)",
    "PFS": "Выживаемость без прогрессирования: от старта линии до прогрессии, рецидива или смерти",
    "mPFS": "Модифицированная PFS: как PFS, плюс старт следующей линии",
    "EFS": "Бессобытийная выживаемость: от старта линии до прогрессии, рецидива, новой линии или смерти",
    "TTNT": "Время до следующей терапии: от старта линии до старта следующей линии или смерти",
}
LINE_ENDPOINTS = {"PFS", "mPFS", "EFS", "TTNT"}
_EVENT_TYPES = {
    "OS": {"death"},
    "PFS": {"progression", "relapse", "death"},
    "mPFS": {"progression", "relapse", "death"},
    "EFS": {"progression", "relapse", "death", "new_line_start"},
    "TTNT": {"death"},
}


def endpoint(name: str, patient: dict, lines: list[dict], observations: list[dict],
             line_no: int = 1) -> Optional[dict]:
    """Длительность и событие одной конечной точки для пациента.

    patient: dx_date, dx_date_precision, last_contact_date, last_contact_precision.
    lines: [{line_no, start_date, start_precision}]; observations: [{event_type, event_date,
    obs_precision}] — только действующие записи. Возвращает None, если точка неприменима
    (нет линии N для PFS/EFS/TTNT), иначе {start, end, days, event, reason} или
    {excluded: причина} при противоречии дат (такие записи не входят в расчёт)."""
    if name not in ENDPOINTS:
        raise ValueError(f"неизвестная конечная точка: {name}")
    by_no = {l["line_no"]: l for l in lines}
    if name in LINE_ENDPOINTS:
        line = by_no.get(line_no)
        if line is None or not line.get("start_date"):
            return None
        start = impute_date(line["start_date"], line.get("start_precision"))
    else:
        start = impute_date(patient["dx_date"], patient.get("dx_date_precision"))
    censor = impute_date(patient["last_contact_date"], patient.get("last_contact_precision"))

    candidates = []                       # (дата события, причина)
    types = _EVENT_TYPES.get(name, set())
    for o in observations:
        if o.get("event_type") in types and o.get("event_date"):
            d = impute_date(o["event_date"], o.get("obs_precision") if o.get("event_date") == o.get("obs_date") else "day")
            if name in LINE_ENDPOINTS and d <= start:
                continue                  # событие до/в день старта линии — повод для неё, не исход
            candidates.append((d, o["event_type"]))
    if name == "TTFT" and 1 in by_no and by_no[1].get("start_date"):
        candidates.append((impute_date(by_no[1]["start_date"], by_no[1].get("start_precision")), "first_line"))
    if name in {"mPFS", "EFS", "TTNT"} and (line_no + 1) in by_no and by_no[line_no + 1].get("start_date"):
        nxt = by_no[line_no + 1]
        candidates.append((impute_date(nxt["start_date"], nxt.get("start_precision")), "next_line"))

    if candidates:
        end, reason = min(candidates)
        event = 1
    else:
        end, reason, event = censor, "censored", 0
    if end is None or start is None:
        return {"excluded": "нет даты начала или окончания"}
    if end < start:
        return {"excluded": "окончание раньше начала отсчёта"}
    return {"start": start, "end": end, "days": (end - start).days, "event": event, "reason": reason}


def km_estimate(durations: Iterable[float], events: Iterable[int], z: float = 1.96) -> dict:
    """Каплан–Майер с ошибкой Гринвуда и 95 % ДИ (log-log), медиана с ДИ, число под риском.
    Без p-value: сравнение групп (log-rank, Cox) — на выгрузке."""
    import math
    durations, events = list(durations), list(events)
    base = kaplan_meier(durations, events)        # те же проверки входа
    data = sorted(zip(durations, events))
    n = len(data)
    steps, censored_at = [], []
    surv, greenwood, at_risk, i = 1.0, 0.0, n, 0
    while i < n:
        t = data[i][0]
        d = c = 0
        while i < n and data[i][0] == t:
            d += data[i][1]
            c += 1 - data[i][1]
            i += 1
        if d:
            surv *= 1 - d / at_risk
            if at_risk > d:
                greenwood += d / (at_risk * (at_risk - d))
            se = surv * math.sqrt(greenwood)
            if 0 < surv < 1:
                s_theta = math.sqrt(greenwood) / abs(math.log(surv))
                low, high = surv ** math.exp(z * s_theta), surv ** math.exp(-z * s_theta)
            else:
                low = high = surv
            steps.append({"t": t, "survival": round(surv, 6), "se": round(se, 6),
                          "ci_low": round(low, 6), "ci_high": round(high, 6), "at_risk": at_risk, "events": d})
        if c:
            censored_at += [(t, surv)] * c
        at_risk -= d + c

    def first_below(key):
        return next((s["t"] for s in steps if s[key] <= 0.5), None)

    return {"n": n, "events": sum(events), "steps": steps,
            "censored": [{"t": t, "survival": round(s, 6)} for t, s in censored_at],
            "median": base["median"], "median_ci": (first_below("ci_low"), first_below("ci_high"))}


def at_risk_table(durations: Iterable[float], times: Iterable[float]) -> list[int]:
    """Число под риском (длительность ≥ t) в заданные моменты."""
    durations = list(durations)
    return [sum(1 for d in durations if d >= t) for t in times]


_DOHNER_ORDER = ("del17p", "del11q", "tri12", "del13q")
_NEGATIVE = {"not_detected", "normal_negative"}


def dohner_group(markers: dict) -> str:
    """Иерархия FISH по Döhner (2000): del(17p) > del(11q) > +12 > нормальный кариотип > del(13q) одна.
    Если вышестоящая проба не исследована — группа не определена (unknown)."""
    for key in _DOHNER_ORDER[:3]:
        v = markers.get(key)
        if v == "detected":
            return key
        if v not in _NEGATIVE:
            return "unknown"
    v = markers.get("del13q")
    if v == "detected":
        return "del13q_sole"
    return "normal" if v in _NEGATIVE else "unknown"


def cll_ipi(age: Optional[float], stage_system: Optional[str], stage_value: Optional[str],
            b2m_mg_l: Optional[float], ighv: Optional[str],
            tp53_abnormal: Optional[bool]) -> Optional[dict]:
    """CLL-IPI (International CLL-IPI working group, 2016):
    TP53 (del(17p) и/или мутация) 4, IGHV немутирован 2, β2-микроглобулин > 3.5 мг/л 2,
    стадия Binet B–C или Rai I–IV 1, возраст > 65 лет 1.
    Группы: 0–1 низкий, 2–3 промежуточный, 4–6 высокий, 7–10 очень высокий.
    Возвращает None, если хоть одного компонента нет: «норму» молча не подставляем."""
    if stage_system == "Binet":
        stage_pts = None if stage_value is None else int(stage_value in ("B", "C"))
    elif stage_system == "Rai":
        stage_pts = None if stage_value is None else int(stage_value != "0")
    else:
        stage_pts = None
    ighv_pts = {"unmutated": 2, "mutated": 0}.get(ighv or "")
    parts = {
        "TP53": None if tp53_abnormal is None else (4 if tp53_abnormal else 0),
        "IGHV": ighv_pts,
        "b2m": None if b2m_mg_l is None else (2 if b2m_mg_l > 3.5 else 0),
        "stage": stage_pts,
        "age": None if age is None else (1 if age > 65 else 0),
    }
    if any(v is None for v in parts.values()):
        return None
    score = sum(parts.values())
    group = ("low" if score <= 1 else "intermediate" if score <= 3
             else "high" if score <= 6 else "very_high")
    return {"score": score, "group": group, "components": parts}


# ---------------------------------------------------------------------------
# 7. Прогностические индексы ДВКЛ и критерии ответа Lugano 2014
# ---------------------------------------------------------------------------
# Балл -> группа: (верхняя граница балла, группа) по возрастанию.
INDEXES = {
    "CLL-IPI": {"max": 10, "groups": ((1, "low"), (3, "intermediate"), (6, "high"), (10, "very_high"))},
    "IPI": {"max": 5, "groups": ((1, "low"), (2, "low_intermediate"), (3, "high_intermediate"), (5, "high"))},
    "R-IPI": {"max": 5, "groups": ((0, "very_good"), (2, "good"), (5, "poor"))},
    "NCCN-IPI": {"max": 8, "groups": ((1, "low"), (3, "low_intermediate"), (5, "high_intermediate"), (8, "high"))},
    "FLIPI": {"max": 5, "groups": ((1, "low"), (2, "intermediate"), (5, "high"))},
    "FLIPI-2": {"max": 5, "groups": ((0, "low"), (2, "intermediate"), (5, "high"))},
    "PRIMA-PI": {"max": 2, "groups": ((0, "low"), (1, "intermediate"), (2, "high"))},
    "sMIPI": {"max": 11, "groups": ((3, "low"), (5, "intermediate"), (11, "high"))},
    "MIPI-c": {"max": 3, "groups": ((0, "low"), (1, "low_intermediate"), (2, "high_intermediate"), (3, "high"))},
    "IPS": {"max": 7, "groups": ((1, "low"), (3, "intermediate"), (7, "high"))},
    "GHSG": {"max": 2, "groups": ((0, "early_favorable"), (1, "early_unfavorable"), (2, "advanced"))},
    # IPSS-WM: при одном факторе группа зависит от того, какой он (возраст — промежуточный).
    "IPSS-WM": {"max": 5, "groups": ((0, "low"), (2, "intermediate"), (5, "high")),
                "allowed": {1: ("low", "intermediate")}},
    "rIPSS-WM": {"max": 5, "groups": ((0, "very_low"), (1, "low"), (2, "intermediate"), (3, "high"), (5, "very_high"))},
    "MALT-IPI": {"max": 3, "groups": ((0, "low"), (1, "intermediate"), (3, "high"))},
    "PIT": {"max": 4, "groups": ((0, "low"), (1, "low_intermediate"), (2, "high_intermediate"), (4, "high"))},
    "PINK": {"max": 4, "groups": ((0, "low"), (1, "intermediate"), (4, "high"))},
    "CNS-IPI": {"max": 6, "groups": ((1, "low"), (3, "intermediate"), (6, "high"))},
    "aaIPI": {"max": 3, "groups": ((0, "low"), (1, "low_intermediate"), (2, "high_intermediate"), (3, "high"))},
    "BL-IPI": {"max": 4, "groups": ((0, "low"), (1, "intermediate"), (4, "high"))},
    "IELSG": {"max": 5, "groups": ((1, "low"), (3, "intermediate"), (5, "high"))},
    "MSKCC": {"max": 2, "groups": ((0, "low"), (1, "intermediate"), (2, "high"))},
    "CLIPi": {"max": 5, "groups": ((1, "low"), (2, "intermediate"), (5, "high"))},
}
LDH_CATEGORIES = ("le_uln", "uln_1_3", "gt_3_uln")   # ЛДГ: ≤ ВГН, > 1–3 × ВГН, > 3 × ВГН
RESPONSES = {
    "iwCLL 2018": ("CR", "CRi", "nPR", "PR", "PR-L", "SD", "PD"),
    "Lugano 2014": ("CMR", "PMR", "NMR", "PMD", "CR", "PR", "SD", "PD"),
    "IWWM-11": ("CR", "VGPR", "PR", "MR", "SD", "PD"),
    "IPCG": ("CR", "CRu", "PR", "SD", "PD"),
    "HCL 2017": ("CR", "PR", "SD", "PD"),          # консенсус по ВКЛ (Grever, 2017): кровь и костный мозг
    "ISCL 2011": ("CR", "PR", "SD", "PD"),         # глобальный ответ при ГМ/СС (Olsen, 2011)
}
LUGANO_METABOLIC = ("CMR", "PMR", "NMR", "PMD")      # ответ по ПЭТ-КТ (Deauville обязателен)


def index_groups(name: str, score) -> tuple:
    """Допустимые группы для балла: обычно одна; у IPSS-WM при балле 1 — две."""
    spec = INDEXES.get(name)
    if spec is None or score is None or not 0 <= score <= spec["max"]:
        return ()
    allowed = spec.get("allowed", {}).get(score)
    return allowed or (next(g for upper, g in spec["groups"] if score <= upper),)


def index_group(name: str, score) -> Optional[str]:
    """Группа риска по баллу индекса; None — неизвестный индекс, балл вне шкалы
    или группа по баллу не однозначна (см. index_groups)."""
    groups = index_groups(name, score)
    return groups[0] if len(groups) == 1 else None


def advanced_stage(stage_system: Optional[str], stage_value: Optional[str]) -> Optional[bool]:
    """Стадия III–IV по Lugano / Ann Arbor (компонент IPI и NCCN-IPI)."""
    if stage_system not in ("Lugano", "Ann Arbor") or not stage_value:
        return None
    return stage_value in ("III", "IV")


def ipi(age: Optional[float], ecog: Optional[int], advanced: Optional[bool],
        ldh: Optional[str], extranodal_gt1: Optional[bool]) -> Optional[dict]:
    """IPI (International NHL Prognostic Factors Project, 1993), по 1 баллу:
    возраст > 60, ЛДГ > ВГН, ECOG ≥ 2, стадия III–IV, > 1 экстранодального очага.
    Группы: 0–1 низкий, 2 низкий-промежуточный, 3 высокий-промежуточный, 4–5 высокий.
    None, если хоть одного компонента нет."""
    parts = {
        "age": None if age is None else int(age > 60),
        "LDH": None if ldh not in LDH_CATEGORIES else int(ldh != "le_uln"),
        "ECOG": None if ecog is None else int(ecog >= 2),
        "stage": None if advanced is None else int(advanced),
        "extranodal": None if extranodal_gt1 is None else int(extranodal_gt1),
    }
    if any(v is None for v in parts.values()):
        return None
    score = sum(parts.values())
    return {"score": score, "group": index_group("IPI", score), "components": parts}


def r_ipi(ipi_score: Optional[int]) -> Optional[dict]:
    """R-IPI (Sehn, 2007) — перегруппировка балла IPI: 0 очень хороший, 1–2 хороший, 3–5 плохой."""
    if ipi_score is None:
        return None
    return {"score": ipi_score, "group": index_group("R-IPI", ipi_score), "components": {"IPI": ipi_score}}


def nccn_ipi(age: Optional[float], ecog: Optional[int], advanced: Optional[bool],
             ldh: Optional[str], extranodal_key_site: Optional[bool]) -> Optional[dict]:
    """NCCN-IPI (Zhou, 2014): возраст > 40–60 1, > 60–75 2, > 75 3; ЛДГ > 1–3 × ВГН 1, > 3 × ВГН 2;
    стадия III–IV 1; поражение КМ, ЦНС, печени/ЖКТ или лёгкого 1; ECOG ≥ 2 1.
    Группы: 0–1 низкий, 2–3 низкий-промежуточный, 4–5 высокий-промежуточный, 6–8 высокий."""
    if age is None:
        age_pts = None
    else:
        age_pts = 0 if age <= 40 else 1 if age <= 60 else 2 if age <= 75 else 3
    parts = {
        "age": age_pts,
        "LDH": None if ldh not in LDH_CATEGORIES else LDH_CATEGORIES.index(ldh),
        "stage": None if advanced is None else int(advanced),
        "extranodal": None if extranodal_key_site is None else int(extranodal_key_site),
        "ECOG": None if ecog is None else int(ecog >= 2),
    }
    if any(v is None for v in parts.values()):
        return None
    score = sum(parts.values())
    return {"score": score, "group": index_group("NCCN-IPI", score), "components": parts}


def check_response(criteria: Optional[str], value: Optional[str], basis: Optional[str],
                   deauville: Optional[int]) -> list[str]:
    """Согласованность оценки ответа. Lugano 2014: по ПЭТ-КТ — метаболический ответ
    (CMR/PMR/NMR/PMD) с баллом Deauville; CMR — Deauville 1–3, остальные — 4–5; по КТ — CR/PR/SD/PD."""
    if not value:
        return []
    if value not in RESPONSES.get(criteria or "", ()):
        return ["response_not_in_criteria"]
    flags = []
    if criteria == "Lugano 2014":
        metabolic = value in LUGANO_METABOLIC
        if metabolic != (basis == "pet_ct"):
            flags.append("lugano_basis_mismatch")
        if metabolic and deauville is None:
            flags.append("deauville_required")
        elif metabolic and (value == "CMR") != (deauville <= 3):
            flags.append("deauville_mismatch")
    return flags


# ---------------------------------------------------------------------------
# 8. Фолликулярная лимфома: FLIPI, FLIPI-2, PRIMA-PI, POD24
# ---------------------------------------------------------------------------
def flipi(age: Optional[float], advanced: Optional[bool], hb_g_l: Optional[float],
          ldh: Optional[str], nodal_areas_gt4: Optional[bool]) -> Optional[dict]:
    """FLIPI (Solal-Céligny, 2004), по 1 баллу: возраст > 60, стадия III–IV, Hb < 120 г/л,
    ЛДГ > ВГН, > 4 зон поражения лимфоузлов. Группы: 0–1 низкий, 2 промежуточный, 3–5 высокий."""
    parts = {
        "age": None if age is None else int(age > 60),
        "stage": None if advanced is None else int(advanced),
        "Hb": None if hb_g_l is None else int(hb_g_l < 120),
        "LDH": None if ldh not in LDH_CATEGORIES else int(ldh != "le_uln"),
        "nodal": None if nodal_areas_gt4 is None else int(nodal_areas_gt4),
    }
    if any(v is None for v in parts.values()):
        return None
    score = sum(parts.values())
    return {"score": score, "group": index_group("FLIPI", score), "components": parts}


def flipi2(age: Optional[float], hb_g_l: Optional[float], b2m_above_uln: Optional[bool],
           largest_node_gt6: Optional[bool], bm_involved: Optional[bool]) -> Optional[dict]:
    """FLIPI-2 (Federico, 2009), по 1 баллу: возраст > 60, Hb < 120 г/л, β2-микроглобулин > ВГН,
    наибольший лимфоузел > 6 см, поражение костного мозга. Группы: 0 низкий, 1–2 промежуточный, 3–5 высокий."""
    parts = {
        "age": None if age is None else int(age > 60),
        "Hb": None if hb_g_l is None else int(hb_g_l < 120),
        "b2m": None if b2m_above_uln is None else int(b2m_above_uln),
        "node": None if largest_node_gt6 is None else int(largest_node_gt6),
        "BM": None if bm_involved is None else int(bm_involved),
    }
    if any(v is None for v in parts.values()):
        return None
    score = sum(parts.values())
    return {"score": score, "group": index_group("FLIPI-2", score), "components": parts}


def prima_pi(b2m_mg_l: Optional[float], bm_involved: Optional[bool]) -> Optional[dict]:
    """PRIMA-PI (Bachy, 2018): β2-микроглобулин > 3 мг/л — высокий риск; иначе поражение
    костного мозга — промежуточный, без него — низкий. Балл: 2 / 1 / 0 (для единого вида)."""
    if b2m_mg_l is None:
        return None
    if b2m_mg_l > 3:                       # костный мозг при высоком β2M не влияет на группу
        return {"score": 2, "group": index_group("PRIMA-PI", 2), "components": {"b2m": 1}}
    if bm_involved is None:
        return None
    score = int(bm_involved)
    return {"score": score, "group": index_group("PRIMA-PI", score), "components": {"b2m": 0, "BM": score}}


POD24_DAYS = 730


def pod24(patient: dict, lines: list[dict], observations: list[dict],
          chemoimmunotherapy: Iterable[str]) -> dict:
    """POD24 (Casulo, 2015): прогрессия или рецидив ≤ 24 мес. от начала 1-й линии иммунохимиотерапии.
    status: pod24 | no_pod24 | not_applicable (1-я линия не иммунохимиотерапия или её нет) |
    undetermined (наблюдение < 24 мес. без прогрессии или смерть без прогрессии до 24 мес.).
    Режимы сравниваются по названию без учёта регистра."""
    allowed = {r.casefold() for r in chemoimmunotherapy}
    line = next((l for l in lines if l["line_no"] == 1), None)
    if line is None or not line.get("start_date") or (line.get("regimen") or "").strip().casefold() not in allowed:
        return {"status": "not_applicable"}
    start = impute_date(line["start_date"], line.get("start_precision"))
    limit = start + timedelta(days=POD24_DAYS)
    events = sorted(impute_date(o["event_date"], "day") for o in observations
                    if o.get("event_type") in ("progression", "relapse") and o.get("event_date"))
    early = [d for d in events if start < d <= limit]
    if early:
        return {"status": "pod24", "date": early[0], "landmark": limit, "months": round((early[0] - start).days / 30.44, 1)}
    if any(o.get("event_type") == "death" and o.get("event_date") and impute_date(o["event_date"], "day") <= limit
           for o in observations):
        return {"status": "undetermined", "reason": "смерть без прогрессии до 24 мес.", "landmark": limit}
    censor = impute_date(patient["last_contact_date"], patient.get("last_contact_precision"))
    if censor is None or censor < limit:
        return {"status": "undetermined", "reason": "наблюдение меньше 24 мес.", "landmark": limit}
    return {"status": "no_pod24", "landmark": limit}


# ---------------------------------------------------------------------------
# 9. Мантийноклеточная лимфома: sMIPI, MIPI-c
# ---------------------------------------------------------------------------
LDH_RATIO_CATEGORIES = ("lt_067", "067_099", "100_149", "ge_150")   # ЛДГ / ВГН: < 0,67 … ≥ 1,5


def smipi(age: Optional[float], ecog: Optional[int], ldh_ratio: Optional[str],
          wbc_10e9_l: Optional[float]) -> Optional[dict]:
    """Упрощённый MIPI (Hoster, 2008): возраст < 50 — 0, 50–59 — 1, 60–69 — 2, ≥ 70 — 3;
    ECOG 2–4 — 2; ЛДГ/ВГН < 0,67 — 0, 0,67–0,99 — 1, 1,00–1,49 — 2, ≥ 1,5 — 3;
    лейкоциты < 6,7 — 0, 6,7–9,99 — 1, 10–14,99 — 2, ≥ 15 ×10⁹/л — 3.
    Группы: 0–3 низкий, 4–5 промежуточный, 6–11 высокий."""
    if age is None:
        age_pts = None
    else:
        age_pts = 0 if age < 50 else 1 if age < 60 else 2 if age < 70 else 3
    if wbc_10e9_l is None:
        wbc_pts = None
    else:
        wbc_pts = 0 if wbc_10e9_l < 6.7 else 1 if wbc_10e9_l < 10 else 2 if wbc_10e9_l < 15 else 3
    parts = {
        "age": age_pts,
        "ECOG": None if ecog is None else (2 if ecog >= 2 else 0),
        "LDH": None if ldh_ratio not in LDH_RATIO_CATEGORIES else LDH_RATIO_CATEGORIES.index(ldh_ratio),
        "WBC": wbc_pts,
    }
    if any(v is None for v in parts.values()):
        return None
    score = sum(parts.values())
    return {"score": score, "group": index_group("sMIPI", score), "components": parts}


def mipi_c(mipi_group: Optional[str], ki67_ge30: Optional[bool]) -> Optional[dict]:
    """MIPI-c (Hoster, 2016): группа MIPI (0 низкий / 1 промежуточный / 2 высокий) + Ki-67 ≥ 30 % (+1).
    0 низкий, 1 низкий-промежуточный, 2 высокий-промежуточный, 3 высокий.
    Здесь от sMIPI — в исследовании использован MIPI; группы sMIPI с ним в основном совпадают."""
    order = {"low": 0, "intermediate": 1, "high": 2}
    if mipi_group not in order or ki67_ge30 is None:
        return None
    score = order[mipi_group] + int(ki67_ge30)
    return {"score": score, "group": index_group("MIPI-c", score),
            "components": {"MIPI": order[mipi_group], "Ki67": int(ki67_ge30)}}


# ---------------------------------------------------------------------------
# 10. Классическая лимфома Ходжкина: группа GHSG, IPS
# ---------------------------------------------------------------------------
def ghsg_group(stage: Optional[str], b_symptoms: Optional[bool], bulky_mediastinum: Optional[bool],
               extranodal: Optional[bool], esr_raised: Optional[bool], ge3_areas: Optional[bool]) -> Optional[dict]:
    """Группа GHSG (стадия по Ann Arbor/Lugano + факторы риска: a — массивное средостение,
    b — экстранодальное поражение, c — повышенная СОЭ, d — ≥ 3 зон лимфоузлов).
    III–IV — распространённые; IIB с a или b — распространённые; I–II с любым фактором —
    ранние неблагоприятные; без факторов — ранние благоприятные. Балл: 0 / 1 / 2."""
    if not stage:
        return None
    roman = stage.rstrip("E")
    extranodal = True if stage.endswith("E") else extranodal
    parts = {"bulky": bulky_mediastinum, "E": extranodal, "ESR": esr_raised, "areas": ge3_areas}

    def result(score):
        return {"score": score, "group": index_group("GHSG", score),
                "components": {k: int(v) for k, v in parts.items() if v is not None}}
    if roman in ("III", "IV"):
        return result(2)
    if any(v is None for v in parts.values()):
        return None
    if roman == "II" and (bulky_mediastinum or extranodal):
        if b_symptoms is None:
            return None
        if b_symptoms:
            return result(2)
    return result(1 if any(parts.values()) else 0)


def ips(albumin_g_l: Optional[float], hb_g_l: Optional[float], male: Optional[bool], age: Optional[float],
        stage_iv: Optional[bool], wbc_10e9_l: Optional[float], lymph_10e9_l: Optional[float]) -> Optional[dict]:
    """IPS (Hasenclever, 1998), по 1 баллу: альбумин < 40 г/л, Hb < 105 г/л, мужской пол, возраст ≥ 45,
    стадия IV, лейкоциты ≥ 15 ×10⁹/л, лимфоциты < 0,6 ×10⁹/л или < 8 % лейкоцитов.
    Группы: 0–1 низкий, 2–3 промежуточный, 4–7 высокий."""
    if lymph_10e9_l is None or wbc_10e9_l is None:
        lymphopenia = None
    else:
        lymphopenia = lymph_10e9_l < 0.6 or (wbc_10e9_l > 0 and lymph_10e9_l / wbc_10e9_l * 100 < 8)
    parts = {
        "albumin": None if albumin_g_l is None else int(albumin_g_l < 40),
        "Hb": None if hb_g_l is None else int(hb_g_l < 105),
        "male": None if male is None else int(male),
        "age": None if age is None else int(age >= 45),
        "stage": None if stage_iv is None else int(stage_iv),
        "WBC": None if wbc_10e9_l is None else int(wbc_10e9_l >= 15),
        "lymph": None if lymphopenia is None else int(lymphopenia),
    }
    if any(v is None for v in parts.values()):
        return None
    score = sum(parts.values())
    return {"score": score, "group": index_group("IPS", score), "components": parts}


# ---------------------------------------------------------------------------
# 11. Макроглобулинемия Вальденстрёма: IPSS-WM, rIPSS-WM
# ---------------------------------------------------------------------------
def ipss_wm(age: Optional[float], hb_g_l: Optional[float], plt_10e9_l: Optional[float],
            b2m_mg_l: Optional[float], igm_g_l: Optional[float]) -> Optional[dict]:
    """IPSS-WM (Morel, 2009): возраст > 65, Hb ≤ 115 г/л, тромбоциты ≤ 100 ×10⁹/л, β2M > 3 мг/л,
    IgM > 70 г/л. Низкий — ≤ 1 фактора, кроме возраста; промежуточный — 2 фактора или возраст > 65;
    высокий — > 2 факторов. Балл — число факторов."""
    parts = {
        "age": None if age is None else int(age > 65),
        "Hb": None if hb_g_l is None else int(hb_g_l <= 115),
        "PLT": None if plt_10e9_l is None else int(plt_10e9_l <= 100),
        "b2m": None if b2m_mg_l is None else int(b2m_mg_l > 3),
        "IgM": None if igm_g_l is None else int(igm_g_l > 70),
    }
    if any(v is None for v in parts.values()):
        return None
    score = sum(parts.values())
    group = "high" if score > 2 else "intermediate" if score == 2 or parts["age"] else "low"
    return {"score": score, "group": group, "components": parts}


def ripss_wm(age: Optional[float], b2m_mg_l: Optional[float], ldh_u_l: Optional[float],
             albumin_g_l: Optional[float]) -> Optional[dict]:
    """rIPSS-WM (Kastritis, 2019): возраст 66–75 — 1, ≥ 76 — 2; β2M ≥ 4 мг/л — 1; ЛДГ ≥ 250 Ед/л — 1;
    альбумин < 35 г/л — 1. Группы: 0 очень низкий, 1 низкий, 2 промежуточный, 3 высокий, 4–5 очень высокий."""
    parts = {
        "age": None if age is None else (0 if age <= 65 else 1 if age < 76 else 2),
        "b2m": None if b2m_mg_l is None else int(b2m_mg_l >= 4),
        "LDH": None if ldh_u_l is None else int(ldh_u_l >= 250),
        "albumin": None if albumin_g_l is None else int(albumin_g_l < 35),
    }
    if any(v is None for v in parts.values()):
        return None
    score = sum(parts.values())
    return {"score": score, "group": index_group("rIPSS-WM", score), "components": parts}


# ---------------------------------------------------------------------------
# 12. Лимфомы маргинальной зоны: MALT-IPI
# ---------------------------------------------------------------------------
def malt_ipi(age: Optional[float], advanced: Optional[bool], ldh: Optional[str]) -> Optional[dict]:
    """MALT-IPI (Thieblemont, 2017), по 1 баллу: возраст ≥ 70, стадия III–IV, ЛДГ > ВГН.
    Группы: 0 низкий, 1 промежуточный, 2–3 высокий. Только для экстранодальной ЛМЗ (MALT)."""
    parts = {
        "age": None if age is None else int(age >= 70),
        "stage": None if advanced is None else int(advanced),
        "LDH": None if ldh not in LDH_CATEGORIES else int(ldh != "le_uln"),
    }
    if any(v is None for v in parts.values()):
        return None
    score = sum(parts.values())
    return {"score": score, "group": index_group("MALT-IPI", score), "components": parts}


# ---------------------------------------------------------------------------
# 13. Периферические T-клеточные лимфомы: PIT, PINK
# ---------------------------------------------------------------------------
def pit(age: Optional[float], ecog: Optional[int], ldh: Optional[str], bm_involved: Optional[bool]) -> Optional[dict]:
    """PIT (Gallamini, 2004), по 1 баллу: возраст > 60, ECOG ≥ 2, ЛДГ > ВГН, поражение костного мозга.
    Группы 1–4: 0, 1, 2, 3–4 балла (низкий … высокий)."""
    parts = {
        "age": None if age is None else int(age > 60),
        "ECOG": None if ecog is None else int(ecog >= 2),
        "LDH": None if ldh not in LDH_CATEGORIES else int(ldh != "le_uln"),
        "BM": None if bm_involved is None else int(bm_involved),
    }
    if any(v is None for v in parts.values()):
        return None
    score = sum(parts.values())
    return {"score": score, "group": index_group("PIT", score), "components": parts}


def pink(age: Optional[float], advanced: Optional[bool], distant_ln: Optional[bool],
         non_nasal: Optional[bool]) -> Optional[dict]:
    """PINK (Kim, 2016) для ENKTL, по 1 баллу: возраст > 60, стадия III–IV, поражение отдалённых
    лимфоузлов, не-назальный тип. Группы: 0 низкий, 1 промежуточный, ≥ 2 высокий."""
    parts = {
        "age": None if age is None else int(age > 60),
        "stage": None if advanced is None else int(advanced),
        "distant": None if distant_ln is None else int(distant_ln),
        "non_nasal": None if non_nasal is None else int(non_nasal),
    }
    if any(v is None for v in parts.values()):
        return None
    score = sum(parts.values())
    return {"score": score, "group": index_group("PINK", score), "components": parts}


# ---------------------------------------------------------------------------
# 14. Агрессивные B-клеточные лимфомы: CNS-IPI
# ---------------------------------------------------------------------------
def cns_ipi(age: Optional[float], ecog: Optional[int], advanced: Optional[bool], ldh: Optional[str],
            extranodal_gt1: Optional[bool], kidney_adrenal: Optional[bool]) -> Optional[dict]:
    """CNS-IPI (Schmitz, 2016): пять факторов IPI + поражение почек и/или надпочечников, по 1 баллу.
    Риск рецидива в ЦНС: 0–1 низкий, 2–3 промежуточный, 4–6 высокий."""
    base = ipi(age, ecog, advanced, ldh, extranodal_gt1)
    if base is None or kidney_adrenal is None:
        return None
    parts = {**base["components"], "kidney": int(kidney_adrenal)}
    score = sum(parts.values())
    return {"score": score, "group": index_group("CNS-IPI", score), "components": parts}


def aa_ipi(ecog: Optional[int], advanced: Optional[bool], ldh: Optional[str]) -> Optional[dict]:
    """IPI с поправкой на возраст (aaIPI, 1993), по 1 баллу: стадия III–IV, ЛДГ > ВГН, ECOG ≥ 2.
    0 низкий, 1 низкий-промежуточный, 2 высокий-промежуточный, 3 высокий. Определён для ≤ 60 лет."""
    parts = {
        "stage": None if advanced is None else int(advanced),
        "LDH": None if ldh not in LDH_CATEGORIES else int(ldh != "le_uln"),
        "ECOG": None if ecog is None else int(ecog >= 2),
    }
    if any(v is None for v in parts.values()):
        return None
    score = sum(parts.values())
    return {"score": score, "group": index_group("aaIPI", score), "components": parts}


def bl_ipi(age: Optional[float], ecog: Optional[int], ldh: Optional[str], cns: Optional[bool]) -> Optional[dict]:
    """BL-IPI (Olszewski, 2021), по 1 баллу: возраст ≥ 40, ECOG ≥ 2, ЛДГ > 3 × ВГН, поражение ЦНС.
    0 низкий, 1 промежуточный, ≥ 2 высокий."""
    parts = {
        "age": None if age is None else int(age >= 40),
        "ECOG": None if ecog is None else int(ecog >= 2),
        "LDH": None if ldh not in LDH_CATEGORIES else int(ldh == "gt_3_uln"),
        "CNS": None if cns is None else int(cns),
    }
    if any(v is None for v in parts.values()):
        return None
    score = sum(parts.values())
    return {"score": score, "group": index_group("BL-IPI", score), "components": parts}


# ---------------------------------------------------------------------------
# 15. Первичная лимфома ЦНС: IELSG, MSKCC
# ---------------------------------------------------------------------------
def ielsg(age: Optional[float], ecog: Optional[int], ldh: Optional[str], csf_protein_high: Optional[bool],
          deep_brain: Optional[bool]) -> Optional[dict]:
    """IELSG (Ferreri, 2003), по 1 баллу: возраст > 60, ECOG > 1, ЛДГ > ВГН, повышенный белок ликвора,
    поражение глубоких структур мозга. 0–1 низкий, 2–3 промежуточный, 4–5 высокий."""
    parts = {
        "age": None if age is None else int(age > 60),
        "ECOG": None if ecog is None else int(ecog > 1),
        "LDH": None if ldh not in LDH_CATEGORIES else int(ldh != "le_uln"),
        "CSF": None if csf_protein_high is None else int(csf_protein_high),
        "deep": None if deep_brain is None else int(deep_brain),
    }
    if any(v is None for v in parts.values()):
        return None
    score = sum(parts.values())
    return {"score": score, "group": index_group("IELSG", score), "components": parts}


def mskcc_pcnsl(age: Optional[float], kps_ge70: Optional[bool]) -> Optional[dict]:
    """Прогностические классы MSKCC (Abrey, 2006): 1 — возраст ≤ 50; 2 — > 50 и KPS ≥ 70;
    3 — > 50 и KPS < 70. Балл 0 / 1 / 2."""
    if age is None:
        return None
    if age <= 50:
        score = 0
    elif kps_ge70 is None:
        return None
    else:
        score = 1 if kps_ge70 else 2
    return {"score": score, "group": index_group("MSKCC", score), "components": {"class": score + 1}}


# ---------------------------------------------------------------------------
# 16. Грибовидный микоз / синдром Сезари: стадия ISCL/EORTC по TNMB, CLIPi
# ---------------------------------------------------------------------------
ISCL_STAGES = ("IA", "IB", "IIA", "IIB", "IIIA", "IIIB", "IVA1", "IVA2", "IVB")


def iscl_stage(t: Optional[str], n: Optional[str], m: Optional[str], b: Optional[str]) -> Optional[str]:
    """Стадия ISCL/EORTC (Olsen, 2007) по категориям TNMB; None — категории неполны или Nx."""
    if not all((t, n, m, b)) or n == "Nx":
        return None
    t1, nn, bb = t[:2], int(n[1]), int(b[1])
    if m == "M1":
        return "IVB"
    if nn == 3:
        return "IVA2"
    if bb == 2:
        return "IVA1"
    if t1 == "T4":
        return "IIIB" if bb == 1 else "IIIA"
    if t1 == "T3":
        return "IIB"
    if nn in (1, 2):
        return "IIA"
    return "IA" if t1 == "T1" else "IB"


def clipi(stage: Optional[str], male: Optional[bool], age: Optional[float], t: Optional[str], n: Optional[str],
          m: Optional[str], b: Optional[str], folliculotropic: Optional[bool]) -> Optional[dict]:
    """CLIPi (Benton, 2013), по 1 баллу. Ранние стадии (IA–IIA): мужской пол, возраст > 60, бляшки
    (T1b/T2b), фолликулотропный вариант, N1/Nx. Поздние (IIB–IVB): мужской пол, возраст > 60,
    B1/B2, N2/N3, висцеральное поражение. 0–1 низкий, 2 промежуточный, 3–5 высокий."""
    if stage not in ISCL_STAGES or male is None or age is None:
        return None
    early = ISCL_STAGES.index(stage) <= ISCL_STAGES.index("IIA")
    if early:
        if t is None or n is None or folliculotropic is None:
            return None
        parts = {"male": int(male), "age": int(age > 60), "plaque": int(t in ("T1b", "T2b")),
                 "follicular": int(folliculotropic), "N1/Nx": int(n in ("N1", "Nx"))}
    else:
        if b is None or n is None or m is None:
            return None
        parts = {"male": int(male), "age": int(age > 60), "B1/B2": int(b in ("B1", "B2")),
                 "N2/N3": int(n in ("N2", "N3")), "visceral": int(m == "M1")}
    score = sum(parts.values())
    return {"score": score, "group": index_group("CLIPi", score), "components": parts}


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
    assert ipi(70, 2, True, "uln_1_3", True)["group"] == "high"                 # 5 баллов
    assert ipi(50, 0, False, "le_uln", False)["score"] == 0 and r_ipi(0)["group"] == "very_good"
    assert nccn_ipi(80, 2, True, "gt_3_uln", True)["score"] == 8
    assert check_response("Lugano 2014", "CMR", "pet_ct", 4) == ["deauville_mismatch"]
    assert flipi(65, True, 110, "uln_1_3", True)["group"] == "high" and prima_pi(3.5, False)["group"] == "high"
    assert smipi(72, 2, "ge_150", 16)["score"] == 11 and mipi_c("high", True)["group"] == "high"
    assert ghsg_group("IV", None, None, None, None, None)["group"] == "advanced"
    assert ips(35, 100, True, 50, True, 16, 0.5)["score"] == 7
    assert ipss_wm(70, 120, 150, 2, 30)["group"] == "intermediate" and index_group("IPSS-WM", 1) is None
    assert malt_ipi(70, True, "uln_1_3")["group"] == "high"
    assert pit(65, 2, "uln_1_3", True)["group"] == "high" and pink(50, False, False, False)["group"] == "low"
    assert cns_ipi(70, 2, True, "uln_1_3", True, True)["score"] == 6
    assert aa_ipi(2, True, "uln_1_3")["group"] == "high"
    assert bl_ipi(45, 2, "gt_3_uln", True)["score"] == 4 and bl_ipi(45, 0, "uln_1_3", False)["score"] == 1
    assert ielsg(65, 2, "uln_1_3", True, True)["group"] == "high" and mskcc_pcnsl(40, None)["group"] == "low"
    assert iscl_stage("T2b", "N0", "M0", "B0") == "IB" and iscl_stage("T4", "N3", "M0", "B1") == "IVA2"
    try:
        kaplan_meier([-1], [1])
        raise AssertionError("отрицательное время должно отвергаться")
    except ValueError:
        pass
    print("registry_core self-test OK")
