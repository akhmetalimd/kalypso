"""
KALYPSO — слой адаптеров источника данных.

Платформа общается с источником через интерфейс SourceAdapter, а не напрямую
с конкретной МИС. Это задел под his.kz: когда появится спецификация, добавляется
HisKzAdapter с той же сигнатурой — подключение становится подстановкой реализации,
а не переработкой платформы (PRD §6.1).

Формат записей (общий для всех адаптеров; сопоставление с регистром — в
config/field_mapping/*.json, не в коде):
    get_patient(id)   -> {"patient": {...}, "diagnoses": [{...}, ...]}
    get_labs(id, s)   -> [{"id", ...поля анализа...}, ...]
    get_documents(id) -> [{"id", ...поля документа...}, ...]
    list_changes(s)   -> [id, ...]   (изменённые с момента s)
Записи источника содержат персональные данные: адаптер исполняется только внутри
коннектора (сеть клиники); деидентификация — сразу после него (kalypso.ingest).

Пилот: ManualAdapter (ручная выгрузка в файл, синтетика).
HisKzAdapter — заглушка, заполняется по спецификации поставщика (этап П5).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional


def _ts(value) -> Optional[datetime]:
    """Метка времени -> aware datetime (наивные считаем UTC)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        dt = datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class SourceAdapter(ABC):
    """Единый интерфейс источника данных для конвейера приёма KALYPSO."""
    name = "base"

    @abstractmethod
    def get_patient(self, source_id: str) -> dict:
        """Демография, диагнозы, коды — структурно. source_id — ключ в источнике
        (например ИИН); деидентификация (хэш) выполняется ПОСЛЕ, до AI-слоя."""
        ...

    @abstractmethod
    def get_labs(self, source_id: str, since=None) -> list[dict]:
        """Лабораторные наблюдения, изменённые с момента since (None — все)."""
        ...

    @abstractmethod
    def get_documents(self, source_id: str) -> list[dict]:
        """Свободный текст: гистология, ПЭТ-КТ, выписки. Вход для AI (только предложения)."""
        ...

    @abstractmethod
    def list_changes(self, since=None) -> list[str]:
        """ID записей, новых или изменённых с момента since — инкрементальная подгрузка."""
        ...


class ManualAdapter(SourceAdapter):
    """Пилотная реализация: выгрузка в файл (JSON) или словарь в памяти. Синтетика.

    store: {source_id: {"patient": {...}, "diagnoses": [...], "labs": [...],
                        "documents": [...], "updated_at": ISO}}
    У анализов и документов может быть свой updated_at (иначе — как у пациента)."""
    name = "manual"

    def __init__(self, store: Optional[dict] = None):
        self._store = store or {}

    @classmethod
    def from_file(cls, path: str | Path) -> "ManualAdapter":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def _rec(self, source_id: str) -> dict:
        if source_id not in self._store:
            raise KeyError("запись не найдена в источнике")   # без ID: это может быть ИИН
        return self._store[source_id]

    def _updated(self, item: dict, rec: dict) -> Optional[datetime]:
        return _ts(item.get("updated_at") or rec.get("updated_at"))

    def get_patient(self, source_id: str) -> dict:
        rec = self._rec(source_id)
        return {"patient": dict(rec.get("patient", {})), "diagnoses": list(rec.get("diagnoses", []))}

    def get_labs(self, source_id, since=None) -> list[dict]:
        rec = self._rec(source_id)
        since = _ts(since)
        return [dict(l) for l in rec.get("labs", [])
                if since is None or (self._updated(l, rec) or since) >= since]

    def get_documents(self, source_id: str) -> list[dict]:
        return [dict(d) for d in self._rec(source_id).get("documents", [])]

    def list_changes(self, since=None) -> list[str]:
        since = _ts(since)
        out = []
        for sid, rec in self._store.items():
            stamps = [self._updated(x, rec) for x in [rec, *rec.get("labs", []), *rec.get("documents", [])]]
            if since is None or any(s is not None and s >= since for s in stamps):
                out.append(sid)
        return out


class HisKzAdapter(SourceAdapter):
    """ЗАГЛУШКА. Заполняется по спецификации his.kz (этап П5): протокол (REST / FHIR /
    SOAP / HL7), авторизация (OAuth2 или сертификаты), эндпоинты пациента, диагнозов,
    анализов, документов и ленты изменений, постраничная выдача, лимиты, повторы.
    Исполняется на сервере ВНУТРИ сети клиники, не в облаке (PRD §6.4, §11).
    Должна пройти тот же набор контрактных тестов, что ManualAdapter (tests/test_adapters.py)."""
    name = "his.kz"

    def __init__(self, base_url: str, auth=None):
        self.base_url = base_url
        self.auth = auth

    def get_patient(self, source_id: str) -> dict:
        raise NotImplementedError("Ожидает спецификацию API his.kz (этап П5)")

    def get_labs(self, source_id, since=None) -> list[dict]:
        raise NotImplementedError("Ожидает спецификацию API his.kz (этап П5)")

    def get_documents(self, source_id: str) -> list[dict]:
        raise NotImplementedError("Ожидает спецификацию API his.kz (этап П5)")

    def list_changes(self, since=None) -> list[str]:
        raise NotImplementedError("Ожидает спецификацию API his.kz (этап П5)")


if __name__ == "__main__":
    demo = ManualAdapter({
        "SYNTH-001": {
            "patient": {"gender": "М"}, "diagnoses": [{"id": "d1", "icd10": "C91.1"}],
            "labs": [{"id": "l1", "code": "HGB", "value": "14", "unit": "г/дл", "updated_at": "2024-01-10T00:00:00"}],
            "documents": [{"id": "x1", "type": "HIST", "text": "ХЛЛ, CD5+CD23+"}],
            "updated_at": "2024-01-05T00:00:00",
        }
    })
    assert demo.get_patient("SYNTH-001")["diagnoses"][0]["icd10"] == "C91.1"
    assert demo.get_labs("SYNTH-001")[0]["unit"] == "г/дл"
    assert demo.get_labs("SYNTH-001", since=date(2024, 2, 1)) == []
    assert demo.list_changes(date(2020, 1, 1)) == ["SYNTH-001"]
    assert demo.list_changes(date(2025, 1, 1)) == []
    print("source_adapter self-test OK")
