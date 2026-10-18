"""Словарь целевых полей AI-предложений: что можно извлечь из какого документа.

Значения — те же коды, что принимает регистр (config/nosology_tree.json и
ограничения БД), чтобы принятое предложение записывалось без перекодировки.
"""
from __future__ import annotations

from kalypso.registry import core_fields

# Вариант нозологии -> (узел WHO-HAEM5, ICD-O-3.2, МКБ-10). Коды — из дерева нозологий.
DIAGNOSIS_CODES = {
    "cll": ("cll_sll", "9823/3", "C91.1"),
    "sll": ("cll_sll", "9670/3", "C83.0"),
}

_MARKERS = {f["key"]: f for f in core_fields()}

TARGETS: dict[str, dict] = {
    "diagnosis": {
        "label": "Нозология (ХЛЛ / ЛМЛ)",
        "values": {"cll": "ХЛЛ (9823/3, C91.1)", "sll": "ЛМЛ (9670/3, C83.0)"},
        "hint": "cll — хронический лимфоцитарный лейкоз; sll — лимфоцитарная лимфома из малых "
                "лимфоцитов (поражение лимфоузлов без лейкемизации).",
    },
    **{key: {
        "label": spec["label_ru"],
        "values": {v: v for v in spec["values"]},
        "hint": {
            "IGHV": "mutated — идентичность с зародышевой линией < 98 %; unmutated — ≥ 98 %.",
            "TP53": "mutated — найдена мутация гена TP53 (секвенирование); wild_type — мутаций нет.",
        }.get(key, "FISH: detected — аберрация выявлена; not_detected — не выявлена."),
    } for key, spec in _MARKERS.items()},
    "deauville": {
        "label": "Шкала Deauville (ПЭТ-КТ)",
        "values": {str(i): str(i) for i in range(1, 6)},
        "hint": "Балл 1–5, только если прямо указан в заключении.",
    },
    "response": {
        "label": "Ответ на терапию (iwCLL 2018)",
        "values": {"CR": "CR", "PR": "PR", "SD": "SD", "PD": "PD"},
        "hint": "Только если заключение прямо формулирует ответ: полная / частичная ремиссия, "
                "стабилизация, прогрессирование.",
    },
}

# Текст гистологии предлагается системой (не моделью): «сохранить заключение в карточку».
HISTOLOGY_TEXT = "histology_report"
TARGET_LABELS = {**{k: v["label"] for k, v in TARGETS.items()}, HISTOLOGY_TEXT: "Текст заключения гистологии"}

_BY_DOC_TYPE = {
    "histology": ["diagnosis", "IGHV", "TP53", "del17p", "del11q", "tri12", "del13q"],
    "cytogenetics": ["del17p", "del11q", "tri12", "del13q"],
    "molecular": ["IGHV", "TP53"],
    "pet_ct": ["deauville", "response"],
    "other": list(TARGETS),
}
MARKER_TARGETS = set(_MARKERS)
OBSERVATION_TARGETS = {"deauville", "response"}


def targets_for(doc_type: str) -> list[str]:
    return list(_BY_DOC_TYPE.get(doc_type, []))


def allowed_values(target: str) -> list[str]:
    return list(TARGETS[target]["values"]) if target in TARGETS else []


def value_label(target: str, value) -> str:
    from kalypso import i18n
    if value is None:
        return "—"
    if target == HISTOLOGY_TEXT:
        return "текст заключения"
    if target in MARKER_TARGETS:   # результат маркера или код пропуска — подписи из каталога
        group = "marker_value" if str(value) in TARGETS[target]["values"] else "missing"
        return i18n.label(group, value)
    return TARGETS.get(target, {}).get("values", {}).get(str(value), str(value))
