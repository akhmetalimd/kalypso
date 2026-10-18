"""Общий интерфейс извлечения: на вход — тип и деидентифицированный текст документа."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Finding:
    target: str
    value: str
    evidence: str | None
    confidence: str          # high | medium | low


@dataclass
class Extraction:
    findings: list[Finding]
    extractor: str
    prompt_version: str
    model: str | None = None
    request_id: str | None = None
    usage: dict = field(default_factory=dict)


class ExtractionFailed(Exception):
    """Извлечение не состоялось; документ уходит в ручную очередь (изящная деградация)."""

    def __init__(self, status: str, message: str, request_id: str | None = None, usage: dict | None = None):
        super().__init__(message)
        self.status = status          # error | refusal | invalid | timeout
        self.request_id = request_id
        self.usage = usage or {}


class Extractor(ABC):
    name: str = "base"
    prompt_version: str = "-"
    model: str | None = None

    @abstractmethod
    def extract(self, doc_type: str, text: str) -> Extraction:
        ...


class FakeExtractor(Extractor):
    """Для тестов: возвращает заданные находки или бросает заданную ошибку."""
    name = "fake"
    prompt_version = "fake-1"

    def __init__(self, findings: list[Finding] | None = None, error: ExtractionFailed | None = None):
        self.findings = findings or []
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def extract(self, doc_type: str, text: str) -> Extraction:
        self.calls.append((doc_type, text))
        if self.error:
            raise self.error
        return Extraction(list(self.findings), self.name, self.prompt_version)
