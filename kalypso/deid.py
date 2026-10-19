"""ИИН и идентификаторы в тексте: проверка формата и контрольной цифры ИИН, поиск
идентификаторов в свободном тексте заключений.

На раннем этапе ИИН хранится в карточке открыто, без хэширования (решение владельца
проекта). Тексты ошибок модуля никогда не содержат сам ИИН.
"""
from __future__ import annotations

import re
from datetime import date

_W1 = list(range(1, 12))
_W2 = [3, 4, 5, 6, 7, 8, 9, 10, 11, 1, 2]
_CENTURY = {1: 1800, 2: 1800, 3: 1900, 4: 1900, 5: 2000, 6: 2000}


class InvalidIIN(ValueError):
    """Ошибка формата ИИН. Текст ошибки никогда не содержит сам ИИН."""


def iin_check_digit(first11: str) -> int | None:
    """Контрольный разряд ИИН (алгоритм РК: веса 1..11, при остатке 10 — 3..11,1,2).
    None — номер с такими цифрами недопустим."""
    digits = [int(c) for c in first11]
    for weights in (_W1, _W2):
        rest = sum(d * w for d, w in zip(digits, weights)) % 11
        if rest != 10:
            return rest
    return None


def normalize_iin(raw: str) -> str:
    """Проверяет ИИН и возвращает 12 цифр. Пробелы и дефисы допускаются при вводе."""
    iin = re.sub(r"[\s-]", "", raw or "")
    if not re.fullmatch(r"\d{12}", iin):
        raise InvalidIIN("ИИН должен состоять из 12 цифр")
    if iin_check_digit(iin[:11]) != int(iin[11]):
        raise InvalidIIN("Неверная контрольная цифра ИИН — проверьте номер")
    century = _CENTURY.get(int(iin[6]))
    if century is not None:  # 0 — особые случаи (иностранцы), дату не проверяем
        try:
            date(century + int(iin[0:2]), int(iin[2:4]), int(iin[4:6]))
        except ValueError:
            raise InvalidIIN("Первые 6 цифр ИИН не образуют дату рождения") from None
    return iin


# ---------------------------------------------------------------------------
# Идентификаторы в свободном тексте (гистология, ПЭТ-КТ)
# ---------------------------------------------------------------------------
_PATTERNS = {
    "iin": re.compile(r"(?<!\d)\d{12}(?!\d)"),
    "phone": re.compile(r"(?<!\d)(?:\+7|8)[\s(-]*7\d{2}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
}


def find_identifiers(text: str | None) -> list[str]:
    """Типы найденных идентификаторов ('iin', 'phone', 'email') — без самих значений.
    ФИО регулярками надёжно не ловятся: за них отвечает пользователь (предупреждение в форме)."""
    if not text:
        return []
    return [kind for kind, rx in _PATTERNS.items() if rx.search(text)]
