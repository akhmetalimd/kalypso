"""Роли и права: матрица из config/roles.json (данные, не код)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

ROLES_FILE = Path(__file__).resolve().parent.parent / "config" / "roles.json"
MATRIX: dict[str, dict[str, str]] = {
    k: v for k, v in json.loads(ROLES_FILE.read_text(encoding="utf-8")).items()
    if not k.startswith("_")
}


class AccessDenied(Exception):
    pass


class NotFound(Exception):
    """Запись не найдена или не видна пользователю (не раскрываем, что она есть)."""


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    login: str
    display_name: str
    role: str
    branch_code: str | None
    must_change_password: bool = False

    def scope(self, perm: str) -> str | None:
        return MATRIX.get(self.role, {}).get(perm)

    def can(self, perm: str) -> bool:
        return self.scope(perm) is not None

    def require(self, perm: str) -> str:
        scope = self.scope(perm)
        if scope is None:
            raise AccessDenied(perm)
        return scope


def visible_patients_sql(principal: Principal, perm: str = "patient.read",
                         alias: str = "p") -> tuple[str, list]:
    """SQL-условие «пациент виден пользователю» для WHERE."""
    scope = principal.require(perm)
    if scope == "all":
        return "TRUE", []
    return (f"EXISTS (SELECT 1 FROM patient_branch pb WHERE pb.patient_id = {alias}.patient_id"
            f" AND pb.branch_code = %s)", [principal.branch_code])
