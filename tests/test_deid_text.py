"""Деидентификация свободного текста (без БД): идентификаторы удаляются, клиника остаётся."""
import re

import pytest

from kalypso.ai.synthetic_docs import NAMES_F, NAMES_M, SURNAMES_F, SURNAMES_M, generate
from kalypso.deid_text import deidentify

CLINICAL = ["CD5+", "CD23+", "Ki-67", "del(17p)", "IGHV", "TP53", "SUVmax", "Заключение"]


@pytest.fixture(scope="module")
def docs():
    return generate(120, seed=11)


def test_synthetic_docs_lose_all_identifiers(docs):
    names = SURNAMES_M + SURNAMES_F + NAMES_M + NAMES_F
    for d in docs:
        r = deidentify(d.raw_text)
        assert r.clean, (r.residual, r.text)
        assert not re.search(r"(?<!\d)\d{12}(?!\d)", r.text)
        assert not re.search(r"\+7\s?7\d\d", r.text)
        leaked = [n for n in names if re.search(rf"(?<![А-Яа-яЁё]){n}(?![а-яё])", r.text)]
        assert leaked == [], (leaked, r.text)


def test_clinical_content_preserved(docs):
    for d in docs:
        raw, clean = d.raw_text, deidentify(d.raw_text).text
        for token in CLINICAL:
            assert (token in raw) == (token in clean), token
        for m in re.finditer(r"\d+(?:[.,]\d+)?\s*%", raw):   # проценты (Ki-67, ядра, VAF, IGHV) целы
            assert m.group(0) in clean


@pytest.mark.parametrize("raw, kinds, must_keep", [
    ("Пациент: Сапаров Ерлан Болатович, обследован.", {"name"}, "обследован"),
    ("Врач: Иванова А.К.", {"name"}, "Врач:"),
    ("Заведующий отделением К.Т. Нурланов", {"name"}, "Заведующий"),
    ("Нурланова Айгуль Ерланқызы направлена", {"name"}, "направлена"),
    ("15.03.1952 г.р., ИИН 520315400123", {"birth_date", "iin"}, ","),
    ("тел. 8 (701) 123-45-67", {"phone"}, "тел."),
    ("История болезни № 2024/1534.", {"record_no"}, "."),
    ("Адрес: ул. Абая 12, кв. 5.", {"address"}, "Адрес:"),
    ("Блок № 24-8841, Ki-67 15%.", {"number"}, "Ki-67 15%"),
])
def test_patterns(raw, kinds, must_keep):
    r = deidentify(raw)
    assert set(r.report) == kinds and must_keep in r.text, (r.report, r.text)


def test_idempotent_and_report_has_no_values(docs):
    for d in docs[:20]:
        once = deidentify(d.raw_text)
        assert deidentify(once.text).text == once.text
        assert all(isinstance(v, int) for v in once.report.values())


def test_clinical_capitalized_words_untouched():
    text = "Заключение: Хронический лимфоцитарный лейкоз. Стадия Binet B. Морфология соответствует ХЛЛ."
    assert deidentify(text).text == text
