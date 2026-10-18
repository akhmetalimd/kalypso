"""Синтетический «МИС-подобный» источник для проверки конвейера приёма.

Записи нарочно похожи на то, что, вероятно, отдаст МИС: ИИН и ФИО прямо в записи,
адрес и телефон, локальные коды анализов, единицы по-русски, даты «дд.мм.гггг»,
тексты заключений с идентификаторами. Формат — предположение, а не API his.kz.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

from kalypso.ai.synthetic_docs import generate as generate_docs
from kalypso.synthetic import synthetic_iin
from source_adapter import ManualAdapter

FACILITIES = ["F-ALM-01", "F-AST-02", "F-SHY-03"]
SURNAMES = ["Ахметов", "Иванов", "Сейткали", "Нурланов", "Петров", "Жумабаев"]
NAMES = ["Ерлан", "Сергей", "Асхат", "Олег", "Данияр"]
LABS = [("HGB", "г/л", 90, 150), ("WBC", "10^9/л", 8, 120), ("LYM#", "10^9/л", 5, 100),
        ("PLT", "10^9/л", 60, 320), ("LDH", "Ед/л", 150, 600), ("B2M", "мг/л", 1.5, 7.5)]
DOC_CODES = {"histology": "HIST", "cytogenetics": "FISH", "molecular": "MOLEC", "pet_ct": "PET"}


def _d(x: date) -> str:
    return f"{x:%d.%m.%Y}"


def build_store(n: int = 12, seed: int = 1, known_iins: list[str] = (), excluded: int = 2,
                updated_at: datetime | None = None) -> dict:
    """n пациентов с ХЛЛ/ЛМЛ (из них known_iins — уже в регистре) + excluded вне критериев."""
    rng = random.Random(seed)
    stamp = (updated_at or datetime(2026, 9, 1, tzinfo=timezone.utc)).isoformat()
    docs = generate_docs(n, seed=seed)
    store: dict = {}
    for i in range(n + excluded):
        birth = date(rng.randint(1940, 1972), rng.randint(1, 12), rng.randint(1, 28))
        iin = known_iins[i] if i < len(known_iins) else synthetic_iin(rng, birth, "M")
        dx = date(rng.randint(2016, 2024), rng.randint(1, 12), rng.randint(1, 28))
        code = "C83.3" if i >= n else rng.choice(["C91.1", "C91.1", "C83.0"])
        rec = {
            "patient": {"iin": iin, "lastName": rng.choice(SURNAMES), "firstName": rng.choice(NAMES),
                        "middleName": "Серикович", "gender": rng.choice(["М", "1"]), "birthDate": _d(birth),
                        "address": "г. Астана, ул. Абая 12, кв. 5", "phone": "+7 701 555 12 34",
                        "facilityCode": rng.choice(FACILITIES), "lastVisitDate": _d(dx + timedelta(days=400))},
            "diagnoses": [{"id": f"dx-{i}-1", "icd10": code, "date": _d(dx), "text": "диагноз"},
                          {"id": f"dx-{i}-2", "icd10": "I10", "date": _d(dx - timedelta(days=900))}],
            "labs": [{"id": f"lab-{i}-{j}", "code": c, "name": c, "unit": u,
                      "value": f"{rng.uniform(lo, hi):.1f}".replace(".", ","), "date": _d(dx + timedelta(days=j))}
                     for j, (c, u, lo, hi) in enumerate(LABS)],
            "documents": [],
            "updated_at": stamp,
        }
        if i < n:
            d = docs[i]
            rec["documents"].append({"id": f"doc-{i}", "type": DOC_CODES[d.doc_type], "date": _d(dx + timedelta(days=10)),
                                     "text": d.raw_text})
        store[iin] = rec
    return store


class SyntheticHisAdapter(ManualAdapter):
    name = "synthetic-his"

    def __init__(self, store: dict | None = None, **kwargs):
        super().__init__(store if store is not None else build_store(**kwargs))

    def touch_lab(self, source_id: str, lab_index: int, value: str, at: datetime) -> None:
        """Изменить значение анализа в источнике (для проверки инкремента и «обновлений»)."""
        lab = self._store[source_id]["labs"][lab_index]
        lab["value"], lab["updated_at"] = value, at.isoformat()
