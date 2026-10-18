"""Граница деидентификации: ИИН, HMAC, идентификаторы в тексте (без БД)."""
import random
from datetime import date

import pytest

from kalypso.deid import IinHasher, InvalidIIN, find_identifiers, iin_check_digit, normalize_iin
from kalypso.synthetic import synthetic_iin

KEY = b"k" * 32


@pytest.fixture
def iin():
    return synthetic_iin(random.Random(7), date(1961, 4, 12), "F")


def test_synthetic_iin_is_valid(iin):
    assert normalize_iin(iin) == iin
    assert iin[:6] == "610412" and iin[6] == "4"  # дата рождения + век/пол
    assert normalize_iin(f"{iin[:6]} {iin[6:]}") == iin  # пробелы при вводе допустимы


def test_wrong_check_digit_rejected_without_echo(iin):
    bad = iin[:11] + str((int(iin[11]) + 1) % 10)
    with pytest.raises(InvalidIIN) as e:
        normalize_iin(bad)
    assert bad not in str(e.value) and "контрольная" in str(e.value)


@pytest.mark.parametrize("raw", ["", "12345", "1234567890123", "61041240000a"])
def test_bad_format_rejected(raw):
    with pytest.raises(InvalidIIN):
        normalize_iin(raw)


def test_impossible_birth_date_rejected():
    head = "611332" + "3" + "0001"  # 32-е число 13-го месяца
    check = iin_check_digit(head)
    if check is None:
        pytest.skip("для этих цифр контрольный разряд не существует")
    with pytest.raises(InvalidIIN, match="дату"):
        normalize_iin(head + str(check))


def test_hash_deterministic_and_keyed(iin):
    h = IinHasher(KEY).hash(iin)
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    assert IinHasher(KEY).hash(iin) == h
    assert IinHasher(b"other-key" * 4).hash(iin) != h
    assert iin not in h


def test_short_key_rejected_and_key_hidden():
    with pytest.raises(ValueError):
        IinHasher(b"short")
    assert "kkkk" not in repr(IinHasher(KEY))


@pytest.mark.parametrize("text, kinds", [
    ("ХЛЛ, CD5+ CD23+, Ki-67 10%", []),
    ("пациент 610412412345 направлен", ["iin"]),
    ("тел. +7 701 123 45 67", ["phone"]),
    ("звонить 8(777)123-45-67", ["phone"]),
    ("почта doctor@clinic.kz", ["email"]),
    ("номер блока 2024-1234567", []),
])
def test_find_identifiers(text, kinds):
    assert find_identifiers(text) == kinds
