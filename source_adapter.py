"""
KALYPSO — слой адаптеров источника данных.

Платформа общается с источником через интерфейс SourceAdapter, а не напрямую
с конкретной МИС. Это задел под his.kz: когда появится спецификация, добавляется
HisKzAdapter с той же сигнатурой — подключение становится подстановкой реализации,
а не переработкой платформы (PRD §6.1).

Пилот использует ManualAdapter (ручной ввод + загрузка файла, синтетика).
HisKzAdapter — заглушка, заполняется по спецификации поставщика (этап П5).
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import date
from typing import Optional


class SourceAdapter(ABC):
    """Единый интерфейс источника данных для конвейера приёма KALYPSO."""

    @abstractmethod
    def get_patient(self, source_id: str) -> dict:
        """Демография, диагноз, коды — структурно. source_id — ключ в источнике
        (например ИИН); деидентификация (хэш) выполняется ПОСЛЕ, до AI-слоя."""
        ...

    @abstractmethod
    def get_labs(self, source_id: str, since: Optional[date] = None) -> list[dict]:
        """Лабораторные наблюдения [{loinc, value, unit, obs_date}, ...].
        Нормализуются через registry_core.canonicalize_lab."""
        ...

    @abstractmethod
    def get_documents(self, source_id: str) -> list[dict]:
        """Свободный текст [{type, text, doc_date}, ...]: гистология, ПЭТ-КТ,
        выписки. Вход для AI-структурирования (предложения, не прямая запись)."""
        ...

    @abstractmethod
    def list_changes(self, since: date) -> list[str]:
        """ID новых/изменённых записей с даты since — инкрементальная подгрузка."""
        ...


class ManualAdapter(SourceAdapter):
    """Пилотная реализация: данные вводятся вручную / грузятся из файла.
    Работает на синтетике, не требует внешних систем."""

    def __init__(self, store: Optional[dict] = None):
        # store: {source_id: {"patient": {...}, "labs": [...], "documents": [...]}}
        self._store = store or {}

    def get_patient(self, source_id: str) -> dict:
        return self._store.get(source_id, {}).get("patient", {})

    def get_labs(self, source_id, since=None) -> list[dict]:
        labs = self._store.get(source_id, {}).get("labs", [])
        if since is None:
            return labs
        return [l for l in labs if l.get("obs_date") and l["obs_date"] >= since]

    def get_documents(self, source_id: str) -> list[dict]:
        return self._store.get(source_id, {}).get("documents", [])

    def list_changes(self, since: date) -> list[str]:
        return list(self._store.keys())


class HisKzAdapter(SourceAdapter):
    """ЗАГЛУШКА. Заполняется по спецификации his.kz (протокол, auth, эндпоинты).
    Исполняется на сервере ВНУТРИ сети клиники, не в облаке (PRD §6.4, §11).
    До появления спецификации методы намеренно не реализованы."""

    def __init__(self, base_url: str, auth=None):
        self.base_url = base_url
        self.auth = auth

    def get_patient(self, source_id: str) -> dict:
        raise NotImplementedError("Ожидает спецификацию API his.kz (этап П5)")

    def get_labs(self, source_id, since=None) -> list[dict]:
        raise NotImplementedError("Ожидает спецификацию API his.kz (этап П5)")

    def get_documents(self, source_id: str) -> list[dict]:
        raise NotImplementedError("Ожидает спецификацию API his.kz (этап П5)")

    def list_changes(self, since: date) -> list[str]:
        raise NotImplementedError("Ожидает спецификацию API his.kz (этап П5)")


if __name__ == "__main__":
    demo = ManualAdapter({
        "SYNTH-001": {
            "patient": {"sex": "М", "who_haem5": "cll_sll"},
            "labs": [{"loinc": "8302-2", "value": 14, "unit": "g/dL", "obs_date": date(2024, 1, 10)}],
            "documents": [{"type": "histology", "text": "ХЛЛ, CD5+CD23+", "doc_date": date(2024, 1, 5)}],
        }
    })
    assert demo.get_patient("SYNTH-001")["who_haem5"] == "cll_sll"
    assert demo.get_labs("SYNTH-001")[0]["unit"] == "g/dL"
    assert demo.list_changes(date(2020, 1, 1)) == ["SYNTH-001"]
    print("source_adapter self-test OK")
