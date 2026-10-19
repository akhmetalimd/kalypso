"""Исследования карточки: ИФТ, генетика, вирусный статус, аутоиммунный скрининг.

Не у всех пациентов есть все исследования: раздел необязательный, запись добавляется,
когда исследование проводилось (нет записи — «не проводилось», полноту это не портит).
Результат — качественный (есть / нет / сомнительно и что именно) и/или количественный
(число + единица); для ИФТ — маркеры той панели, что использовалась, как в заключении.
Справочник — config/investigations.json. Генетика — сами проведённые исследования;
поле модуля (TP53, IGHV, MYC…) — вывод для индекса; расхождение — предупреждение.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import registry_core as core

CATALOG = json.loads((Path(core.CONFIG_DIR) / "investigations.json").read_text(encoding="utf-8"))
KINDS = tuple(CATALOG["kinds"])
MATERIALS = CATALOG["materials"]
QUAL = ("positive", "negative", "equivocal")
MARKER_RESULTS = {"pos": "+", "neg": "−", "dim": " слаб.", "bright": " ярк.", "partial": " частично"}
_MARKER = re.compile(r"([A-Za-zА-Яа-я][\w/\-]*?)\s*(\+|−|–|-|±|dim|слаб\w*\.?|weak|bright|ярк\w*\.?|partial|частич\w*)",
                     re.IGNORECASE)


def tests(kind: str) -> list[dict]:
    return CATALOG["kinds"][kind]["tests"]


def test_def(kind: str, code: str) -> dict | None:
    return next((t for t in tests(kind) if t["code"] == code), None) if kind in CATALOG["kinds"] else None


def kind_label(kind: str) -> str:
    return CATALOG["kinds"][kind]["label"]


def qual_label(kind: str, code: str, qual: str | None) -> str:
    """Подпись качественного результата: своя у исследования («мутирован») или общая («есть»)."""
    if not qual:
        return "—"
    t = test_def(kind, code) or {}
    return t.get("qual", {}).get(qual) or CATALOG["qual"][qual]


def parse_markers(text: str | None) -> list[dict] | None:
    """«CD5+, CD19+ CD20 dim; CD23+ CD10−» -> [{"m": "CD5", "r": "pos"}, …]. Нераспознанный
    фрагмент — ValueError с самим фрагментом (без данных пациента: только маркеры)."""
    if not text or not text.strip():
        return None
    out, rest = [], text
    for m in _MARKER.finditer(text):
        sign = m.group(2).lower()
        r = ("pos" if sign == "+" else "neg" if sign in ("-", "−", "–") else
             "bright" if sign.startswith(("bright", "ярк")) else
             "partial" if sign.startswith(("partial", "частич")) else "dim")
        out.append({"m": m.group(1), "r": r})
        rest = rest.replace(m.group(0), " ", 1)
    leftover = re.sub(r"[\s,;.]+", " ", rest).strip()
    if leftover:
        raise ValueError(f"Маркеры ИФТ: не распознано «{leftover[:40]}» — пишите как в заключении: CD5+, CD20 dim, CD10−")
    return out or None


def format_markers(markers: list[dict] | None) -> str:
    return " ".join(m["m"] + MARKER_RESULTS.get(m["r"], "?") for m in markers or [])


def row_from_form(f, kind: str) -> dict:
    """Поля записи из формы (Form из registry): справочник, материал, результат, маркеры."""
    if kind not in KINDS:
        f.errors.append("Неизвестный раздел исследований")
        return {}
    code = f.choice("test_code", "Исследование", [t["code"] for t in tests(kind)], required=True)
    t = test_def(kind, code) or {}
    test_date = f.date("test_date", "Дата исследования")
    value = f.num("value", "Количественный результат")
    unit = f.text("unit", "Единица", maxlen=30) or (t.get("unit") if value is not None else None)
    markers = None
    if kind == "flow":
        try:
            markers = parse_markers(f.raw("markers"))
        except ValueError as e:
            f.errors.append(str(e))
    row = {
        "kind": kind, "test_code": code, "test_date": test_date,
        "test_date_precision": f.precision("test_date_precision") if test_date else None,
        "material": f.choice("material", "Материал", list(MATERIALS)),
        "qual_result": f.choice("qual_result", "Качественный результат", QUAL),
        "qual_detail": f.free_text("qual_detail", "Что именно") if f.raw("qual_detail") else None,
        "value": value, "unit": unit if value is not None else None,
        "markers": json.dumps(markers, ensure_ascii=False) if markers else None,
        "conclusion": f.free_text("conclusion", "Заключение") if f.raw("conclusion") else None,
        "provenance": f.choice("provenance", "Ретро/проспектив", ("retrospective", "prospective")),
    }
    if row["qual_detail"] and len(row["qual_detail"]) > 300:
        f.errors.append("Что именно: не длиннее 300 символов")
    if not any(row[k] is not None for k in ("qual_result", "value", "markers", "conclusion")):
        f.errors.append("Укажите результат: качественный, количественный, маркеры или заключение")
    return row


def by_kind(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {k: [] for k in KINDS}
    for r in rows:
        out.setdefault(r["kind"], []).append(r)
    return out


def _latest(rows: list[dict]) -> dict[str, dict]:
    """Последний результат по каждому исследованию (по дате; без даты — раньше датированных)."""
    out: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: (r["test_date"] is not None, r["test_date"] or 0, r["created_at"])):
        if r["qual_result"]:
            out[r["test_code"]] = r
    return out


def virology(rows: list[dict]) -> dict[str, str]:
    """{код вирусного исследования: positive | negative | equivocal} — последний результат."""
    return {code: r["qual_result"] for code, r in _latest([r for r in rows if r["kind"] == "virology"]).items()}


def genetics_mismatches(mm: dict[str, str], rows: list[dict]) -> list[str]:
    """Флаги «результат генетического исследования расходится с полем модуля»: для каждого
    поля, о котором исследование даёт вывод (links), если поле заполнено значением."""
    flags = []
    for code, r in _latest([r for r in rows if r["kind"] == "genetics"]).items():
        for field, mapping in (test_def("genetics", code) or {}).get("links", {}).items():
            expected = mapping.get(r["qual_result"])
            have = mm.get(field)
            if expected and have and have not in core.MISSING_CODES and have != expected:
                flags.append(f"inv_mismatch_{field}")
    return sorted(set(flags))
