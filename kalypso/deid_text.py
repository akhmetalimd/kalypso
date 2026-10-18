"""Деидентификация свободного текста документов (стадия 2 конвейера, PRD §6.2).

Идентификаторы заменяются метками ([ФИО], [ИИН], …). Отчёт содержит только виды
и число замен — сами значения нигде не сохраняются и не логируются.

Регулярные выражения не дают гарантии для ФИО: поэтому после очистки
пользователь видит текст и подтверждает, что идентификаторов не осталось,
а повторная автопроверка (kalypso.deid.find_identifiers) — жёсткий шлюз перед AI.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from kalypso.deid import find_identifiers

_UP = "А-ЯЁӘҒҚҢӨҰҮҺІ"
_LO = "а-яёәғқңөұүһі"
_NAME = rf"[{_UP}][{_LO}]+(?:-[{_UP}][{_LO}]+)?"
_INIT = rf"[{_UP}]\.\s?(?:[{_UP}]\.)?"
_PATRONYMIC = rf"[{_UP}][{_LO}]+(?:вич|вна|ична|чна|ьич|улы|ұлы|кызы|қызы)"

# Порядок важен: сначала однозначные шаблоны, затем эвристики ФИО.
_RULES: list[tuple[str, str, re.Pattern]] = [
    ("email", "[EMAIL]", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("phone", "[ТЕЛ]", re.compile(r"(?<!\d)(?:\+7|8)[\s(-]*7\d{2}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")),
    ("iin", "[ИИН]", re.compile(r"(?<!\d)\d{12}(?!\d)")),
    ("birth_date", "[ДАТА РОЖДЕНИЯ]", re.compile(
        r"(?:дата\s+рождения|д\.\s?р\.|г\.\s?р\.)\s*[:\-–]?\s*\d{1,2}[./]\d{1,2}[./]\d{2,4}"
        r"|\d{1,2}[./]\d{1,2}[./]\d{4}\s*г\.\s?р\.?|\b\d{4}\s*г\.\s?р\.?", re.I)),
    ("record_no", "[№ ДОКУМЕНТА]", re.compile(
        r"(?:истори[яи]\s+болезни|медицинск\w+\s+карт\w*|амбулаторн\w+\s+карт\w*|медкарт\w*|\bИБ|\bамб\.?\s*карт\w*)"
        r"\s*№?\s*[:\-]?\s*[\w/-]*\d[\w/-]*", re.I)),
    ("number", "[НОМЕР]", re.compile(r"№\s*\d[\d/-]{3,}")),
    ("address", "[АДРЕС]", re.compile(
        r"(?:\bул\.|\bулица|\bпр-т|\bпроспект|\bмкр\.?|\bмикрорайон|\bпер\.|\bпереулок)\s+[^\n,;]{1,60}"
        r"(?:,\s*(?:д\.|дом)\s*\d+[а-я]?)?(?:,\s*(?:кв\.|квартира)\s*\d+)?", re.I)),
    # «ФИО: …», «Пациент: …», «Врач: …» — имя после метки.
    ("name", r"\g<1>[ФИО]", re.compile(
        rf"((?:ФИО|Ф\.И\.О\.|Пациент|Пациентка|Больной|Больная|Врач|Исследование провел[аи]?|"
        rf"Заведующ\w+(?:\s+отделением)?|Патологоанатом|Лаборант)\s?[:\-–]?\s)"
        rf"(?:{_NAME}(?:\s+{_NAME}){{0,2}}(?:\s+{_INIT})?|{_INIT}\s?{_NAME})")),
    ("name", "[ФИО]", re.compile(rf"\b{_NAME}\s+{_NAME}\s+{_PATRONYMIC}\b")),   # Фамилия Имя Отчество
    ("name", "[ФИО]", re.compile(rf"\b{_NAME}\s+{_INIT}(?=[\s,.;:)]|$)")),       # Иванов И.И.
    ("name", "[ФИО]", re.compile(rf"(?<![{_UP}{_LO}]){_INIT}\s?{_NAME}\b")),      # И.И. Иванов
]


@dataclass
class DeidResult:
    text: str
    report: dict[str, int] = field(default_factory=dict)
    residual: list[str] = field(default_factory=list)   # что осталось после очистки (виды)

    @property
    def clean(self) -> bool:
        return not self.residual


def deidentify(raw: str) -> DeidResult:
    text = (raw or "").replace("\r\n", "\n").strip()
    report: dict[str, int] = {}
    for kind, label, rx in _RULES:
        text, n = rx.subn(label, text)
        if n:
            report[kind] = report.get(kind, 0) + n
    return DeidResult(text=text, report=report, residual=find_identifiers(text))
