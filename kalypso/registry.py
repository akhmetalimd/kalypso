"""Доменные сервисы регистра (модули ХЛЛ и ДВКЛ): единственный путь записи в регистр.

Каждая функция принимает пользователя (Principal), проверяет права и видимость
пациента, приводит и проверяет ввод, пишет в одной транзакции и возвращает
предупреждения контроля качества. Последний рубеж — ограничения БД (0002, 0005).

Всё, что зависит от нозологии (стадирование, индекс, критерии ответа, поля модуля),
берётся из узла дерева WHO-HAEM5 (config/nosology_tree.json) — см. profile().
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from datetime import date
from typing import Any
from uuid import UUID

import psycopg

import registry_core as core
from kalypso.db import Database, db_error_message
from kalypso.deid import InvalidIIN, find_identifiers, normalize_iin
from kalypso.indices import (  # noqa: F401 — _age, _tp53_abnormal, pod24_status импортируют analytics/docgen
    _age, _tp53_abnormal, computed_indices, module_info, module_quality, pod24_status)
from kalypso.rbac import NotFound, Principal, visible_patients_sql

CLL = "cll_sll"
REGISTRY_START = date.fromisoformat(
    json.loads((Path(core.CONFIG_DIR) / "registry.json").read_text(encoding="utf-8"))["registry_start"])


def provenance_for(dx_date: date) -> str:
    """Ретро/проспектив — автоматически: диагноз до запуска регистра — ретроспективно."""
    return "prospective" if dx_date >= REGISTRY_START else "retrospective"
MISSING = sorted(core.MISSING_CODES)
PRECISIONS = ("day", "month", "year")
# «Условные» поля пациента: пусто без кода пропуска -> флаг полноты.
CONDITIONAL_FIELDS = {
    "birth_date": "Дата рождения", "ecog": "ECOG", "cirs_score": "CIRS-G",
    "b_symptoms": "B-симптомы", "stage_value": "Стадия", "index_score": "Прогностический индекс",
}
DX_FIELDS = ("who_haem5", "icd_o_3_2", "mkb", "dx_date", "dx_date_precision")


class ValidationError(Exception):
    def __init__(self, errors: list[str], fields: dict[str, str] | None = None):
        super().__init__("; ".join(errors))
        self.errors = errors
        self.fields = fields or {}             # ошибки по полям формы


@dataclass
class Result:
    status: str = "saved"
    record_id: UUID | None = None
    warnings: list[str] = field(default_factory=list)


def active_nodes() -> list[dict]:
    """Нозологии, доступные для ввода в пилоте (status = active)."""
    out = []

    def walk(n):
        if n.get("status") == "active":
            out.append(n)
        for ch in n.get("children", []):
            walk(ch)
    walk(core.NOSOLOGY_TREE)
    return out


# Группы для навигации (кнопки дашбордов, выбор нозологии). Не вошедшая сюда активная нозология
# попадает в «Прочие» — новую нозологию достаточно дописать в нужную группу.
NOSOLOGY_GROUPS = [
    ("Индолентные B-клеточные", ("cll_sll", "follicular", "mzl", "waldenstrom", "hcl")),
    ("Агрессивные B-клеточные", ("dlbcl", "hgbl", "pmbcl", "burkitt", "pcnsl", "mcl")),
    ("Лимфомы Ходжкина", ("hodgkin", "nlpbcl")),
    ("T- и NK-клеточные", ("ptcl", "mf_ss", "tlgll")),
]


def nosology_groups() -> list[tuple[str, list[dict]]]:
    nodes = {n["id"]: n for n in active_nodes()}
    out, used = [], set()
    for title, ids in NOSOLOGY_GROUPS:
        items = [nodes[i] for i in ids if i in nodes]
        used.update(ids)
        if items:
            out.append((title, items))
    rest = [n for i, n in nodes.items() if i not in used]
    if rest:
        out.append(("Прочие", rest))
    return out


def profile(who_haem5: str | None) -> dict:
    """Настройки модуля нозологии из её узла дерева: системы и значения стадий, индекс,
    критерии и варианты ответа, поля модуля (module_data). Неизвестный узел -> ХЛЛ."""
    node = core.find_node(core.NOSOLOGY_TREE, who_haem5 or CLL)
    if not node or not node.get("module"):
        node = core.find_node(core.NOSOLOGY_TREE, CLL)
    criteria = node.get("response_criteria")
    return {"id": node["id"], "name": node["name"], "module": node["module"],
            "stages": node.get("stage_values", {}), "index": node.get("index"),
            "criteria": criteria, "responses": core.RESPONSES.get(criteria, ()),
            "fields": node.get("core_fields", []), "regimens": node.get("regimens", []),
            "pod24_regimens": node.get("pod24_regimens", [])}


def core_fields(who_haem5: str = CLL) -> list[dict]:
    return profile(who_haem5)["fields"]


def all_fields() -> list[dict]:
    """Поля всех активных модулей (для выгрузки). Общий ключ (ldh_uln у ДВКЛ и ФЛ) —
    одно поле: смысл и значения у него одинаковые."""
    seen, out = set(), []
    for f in (f for n in active_nodes() for f in n.get("core_fields", [])):
        if f["key"] not in seen:
            seen.add(f["key"])
            out.append(f)
    return out


def _fields_of_module(module: str) -> list[dict]:
    node = next((n for n in active_nodes() if n.get("module") == module), {})
    return node.get("core_fields", [])


# ---------------------------------------------------------------------------
# Приведение ввода (строки форм или значения Python)
# ---------------------------------------------------------------------------
class Form:
    def __init__(self, data: dict):
        self.data = {k: (v.strip() if isinstance(v, str) else v) for k, v in data.items()}
        self.errors: list[str] = []
        self.fields: dict[str, str] = {}      # поле -> ошибка (показывается у поля в форме)

    def _err(self, name: str, message: str) -> None:
        self.errors.append(message)
        self.fields.setdefault(name, message)

    def raw(self, name: str):
        v = self.data.get(name)
        return None if v == "" else v

    def text(self, name: str, label: str, required=False, maxlen=20000) -> str | None:
        v = self.raw(name)
        if v is None:
            if required:
                self._err(name, f"{label}: обязательное поле")
            return None
        v = str(v)
        if len(v) > maxlen:
            self._err(name, f"{label}: не длиннее {maxlen} символов")
        return v

    def choice(self, name: str, label: str, allowed, required=False, default=None) -> str | None:
        v = self.raw(name)
        if v is None:
            if required and default is None:
                self._err(name, f"{label}: выберите значение")
            return default
        if v not in allowed:
            self._err(name, f"{label}: недопустимое значение")
            return None
        return v

    def date(self, name: str, label: str, required=False) -> date | None:
        v = self.raw(name)
        if v is None:
            if required:
                self._err(name, f"{label}: обязательное поле")
            return None
        try:
            return core._as_date(v)
        except ValueError:
            self._err(name, f"{label}: дата в формате ГГГГ-ММ-ДД")
            return None

    def precision(self, name: str) -> str:
        return self.choice(name, "Точность даты", PRECISIONS, default="day")

    def int(self, name: str, label: str, lo=None, hi=None) -> int | None:
        v = self.raw(name)
        if v is None:
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            self._err(name, f"{label}: целое число")
            return None
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            self._err(name, f"{label}: от {lo} до {hi}")
            return None
        return n

    def num(self, name: str, label: str, required=False) -> float | None:
        v = self.raw(name)
        if v is None:
            if required:
                self._err(name, f"{label}: обязательное поле")
            return None
        try:
            return float(str(v).replace(",", "."))
        except ValueError:
            self._err(name, f"{label}: число")
            return None

    def free_text(self, name: str, label: str) -> str | None:
        """Свободный текст документа: идентификаторы не допускаются (CLAUDE.md, п. 4)."""
        v = self.text(name, label)
        found = find_identifiers(v)
        if found:
            kinds = {"iin": "ИИН (12 цифр)", "phone": "номер телефона", "email": "e-mail"}
            self._err(name, f"{label}: удалите идентификаторы — " + ", ".join(kinds[k] for k in found))
        return v

    def check(self) -> None:
        if self.errors:
            raise ValidationError(self.errors, self.fields)


def _db_call(fn):
    """Ошибки БД -> ValidationError с понятным сообщением."""
    try:
        return fn()
    except psycopg.Error as e:
        raise ValidationError([db_error_message(e)]) from e


# ---------------------------------------------------------------------------
# Пациент
# ---------------------------------------------------------------------------
def _patient_fields(f: Form) -> dict:
    who = f.choice("who_haem5", "Нозология", [n["id"] for n in active_nodes()], default=CLL)
    codes = core.resolve_codes(who or CLL) or {}
    row = {
        "sex": f.choice("sex", "Пол", ("M", "F", "unknown"), required=True),
        "birth_date": f.date("birth_date", "Дата рождения"),
        "birth_date_precision": f.precision("birth_date_precision"),
        "who_haem5": who,
        "icd_o_3_2": f.text("icd_o_3_2", "ICD-O-3.2") or codes.get("icd_o_3_2"),
        "mkb": (f.text("mkb", "МКБ-10") or codes.get("mkb10") or "").upper() or None,
        "dx_date": f.date("dx_date", "Дата диагноза", required=True),
        "dx_date_precision": f.precision("dx_date_precision"),
        "first_visit_date": f.date("first_visit_date", "Первый визит"),
        "first_visit_precision": f.precision("first_visit_precision"),
        "last_contact_date": f.date("last_contact_date", "Последний контакт", required=True),
        "last_contact_precision": f.precision("last_contact_precision"),
        "provenance": f.choice("provenance", "Ретро/проспектив", ("retrospective", "prospective")),
        "status_last_contact": f.choice("status_last_contact", "Статус",
                                        ("alive", "dead", "lost_to_follow_up"), required=True),
        "ecog": f.int("ecog", "ECOG", 0, 4),
        "cirs_score": f.int("cirs_score", "CIRS-G", 0, 56),
        "cirs_fitness": f.choice("cirs_fitness", "Fitness", ("fit", "unfit", "frail")),
        "charlson_score": f.int("charlson_score", "Charlson", 0, 40),
        "b_symptoms": f.choice("b_symptoms", "B-симптомы", ("yes", "no", "unknown")),
        "histology_report": f.free_text("histology_report", "Заключение гистологии"),
        "histology_scan": f.text("histology_scan", "Ссылка на скан", maxlen=500),
    }
    row.update(_stage_and_index(f, profile(who)))
    if row["provenance"] is None and row["dx_date"]:
        row["provenance"] = provenance_for(row["dx_date"])
    for flag in core.validate_codes(row):
        f.errors.append({
            "icd_o_mismatch": "Код ICD-O-3.2 не соответствует выбранной нозологии",
            "mkb_mismatch": "Код МКБ-10 не соответствует выбранной нозологии",
            "code_pair_mismatch": "Коды ICD-O и МКБ относятся к разным вариантам нозологии",
        }.get(flag, flag))
    if row["dx_date"] and row["last_contact_date"] and row["last_contact_date"] < row["dx_date"]:
        f.errors.append("Последний контакт не может быть раньше даты диагноза")
    return row


def _stage_and_index(f: Form, prof: dict) -> dict:
    """Стадия и индекс — по настройкам нозологии; группа индекса выводится из балла."""
    stages, name = prof["stages"], prof["index"]
    system = f.choice("stage_system", "Система стадирования", list(stages))
    value = f.raw("stage_value")
    if (system is None) != (value is None):
        f.errors.append("Стадия: укажите и систему стадирования, и стадию")
    elif system and value not in stages[system]:
        f.errors.append(f"Стадия по {system}: {', '.join(stages[system])}")
    spec = core.INDEXES.get(name or "")
    score = f.int("index_score", f"Балл {name}", 0, spec["max"]) if spec else None
    group = f.choice("index_group", f"Группа {name}", [g for _, g in spec["groups"]]) if spec else None
    if score is None and group:
        f.errors.append(f"{name}: укажите балл (группа выводится из него)")
    elif score is not None:
        allowed = core.index_groups(name, score)
        if group and group not in allowed:
            f.errors.append(f"{name}: группа не соответствует баллу {score}")
        elif not group and len(allowed) == 1:
            group = allowed[0]
        elif not group:
            f.errors.append(f"{name}: при балле {score} группа зависит от факторов — укажите её")
    return {"stage_system": system, "stage_value": value if system else None,
            "index_name": name if score is not None else None, "index_score": score,
            "index_group": group if score is not None else None}


def _missing_codes(f: Form, row: dict) -> dict[str, str]:
    out = {}
    for name, label in CONDITIONAL_FIELDS.items():
        code = f.choice(f"{name}__missing", f"{label}: код пропуска", MISSING)
        if code and row.get(name) is not None:
            f.errors.append(f"{label}: укажите либо значение, либо код пропуска")
        elif code:
            out[name] = code
    return out


def _death_fields(f: Form, row: dict) -> dict:
    d = {"event_date": f.date("death_date", "Дата смерти", required=True),
         "death_cause": f.choice("death_cause", "Причина смерти",
                                 ("lpd_related", "not_related", "unknown"), required=True)}
    if d["event_date"] and row["dx_date"] and d["event_date"] < row["dx_date"]:
        f.errors.append("Дата смерти раньше даты диагноза")
    if d["event_date"] and row["last_contact_date"] and d["event_date"] > row["last_contact_date"]:
        f.errors.append("Дата смерти позже последнего контакта")
    return d


def _inherit(row: dict, patient: dict) -> dict:
    """Ретро/проспектив записи — как у пациента, если не указано явно."""
    if not row.get("provenance"):
        row["provenance"] = patient["provenance"]
    return row


def _insert(cur, table: str, row: dict, returning: str):
    cols = ", ".join(row)
    ph = ", ".join(["%s"] * len(row))
    cur.execute(f"INSERT INTO {table} ({cols}) VALUES ({ph}) RETURNING {returning}", list(row.values()))
    return cur.fetchone()[returning]


def _write_missing(cur, pid, codes: dict[str, str]) -> None:
    cur.execute("SELECT field, code FROM patient_missing WHERE patient_id = %s", (pid,))
    have = {r["field"]: r["code"] for r in cur.fetchall()}
    for name in CONDITIONAL_FIELDS:
        new = codes.get(name)
        if have.get(name) == new:
            continue
        if name in have:
            cur.execute("DELETE FROM patient_missing WHERE patient_id = %s AND field = %s", (pid, name))
        if new:
            cur.execute("INSERT INTO patient_missing (patient_id, field, code, source) "
                        "VALUES (%s, %s, %s, 'manual')", (pid, name, new))


def _insert_death(cur, pid, death: dict, provenance: str) -> None:
    _insert(cur, "observation", {
        "patient_id": pid, "obs_date": death["event_date"], "event_type": "death",
        "event_date": death["event_date"], "death_cause": death["death_cause"],
        "source": "manual", "provenance": provenance}, "obs_id")


def _quality(row: dict) -> list[str]:
    return core.validate_record(row)


def _iin(raw) -> str:
    try:
        return normalize_iin(str(raw or ""))
    except InvalidIIN as e:
        raise ValidationError([str(e)]) from None


def register_patient(db: Database, p: Principal, data: dict) -> Result:
    """Новый пациент. ИИН — ключ дедупликации: если пациент уже есть в чужом филиале,
    врач получает запрос доступа к куратору, а не данные пациента."""
    p.require("patient.write")
    f = Form(data)
    iin = _iin(f.raw("iin"))

    with db.tx(p.user_id) as cur:
        # Под RLS врач не видит пациентов чужого филиала; функция БД сообщает только факт и id.
        cur.execute("SELECT patient_id_by_iin(%s) AS pid", (iin,))
        pid = cur.fetchone()["pid"]
        if pid:
            cur.execute("SELECT 1 FROM patient_branch WHERE patient_id = %s AND branch_code = %s",
                        (pid, p.branch_code))
            if cur.fetchone():
                return Result("exists_visible", pid)
            cur.execute("""INSERT INTO branch_access_request (patient_id, branch_code)
                           VALUES (%s, %s) ON CONFLICT DO NOTHING""", (pid, p.branch_code))
            return Result("exists_requested", None)

    row = _patient_fields(f)
    missing = _missing_codes(f, row)
    death = _death_fields(f, row) if row["status_last_contact"] == "dead" else None
    f.check()
    row.update(iin=iin, branch=p.branch_code, source="manual")

    def write():
        with db.tx(p.user_id) as cur:
            pid = _insert(cur, "patient", row, "patient_id")
            _write_missing(cur, pid, missing)
            if death:
                _insert_death(cur, pid, death, row["provenance"])
            return pid
    pid = _db_call(write)
    return Result("created", pid, _quality({**row, **(death or {}),
                                            "event_type": "death" if death else None}))


def _load_visible(cur, p: Principal, pid, perm="patient.read") -> dict:
    cond, params = visible_patients_sql(p, perm)
    cur.execute(f"SELECT * FROM patient p WHERE p.patient_id = %s AND p.deleted_at IS NULL AND {cond}",
                [pid, *params])
    row = cur.fetchone()
    if row is None:
        raise NotFound()
    return row


def update_patient(db: Database, p: Principal, pid, data: dict) -> Result:
    p.require("patient.write")
    f = Form(data)
    row = _patient_fields(f)
    missing = _missing_codes(f, row)
    reason = f.text("change_reason", "Причина пересмотра диагноза", maxlen=500)

    with db.tx(p.user_id) as cur:
        old = _load_visible(cur, p, pid, "patient.write")
        cur.execute("SELECT obs_id FROM observation WHERE patient_id = %s AND event_type = 'death' "
                    "AND deleted_at IS NULL", (pid,))
        death_obs = cur.fetchone()
    dx_changed = any(row[k] != old[k] for k in DX_FIELDS)
    if dx_changed and not reason:
        f.errors.append("Диагноз изменён — укажите причину пересмотра (версия диагноза сохранится)")
    death = None
    if row["status_last_contact"] == "dead" and not death_obs:
        death = _death_fields(f, row)
    f.check()

    def write():
        with db.tx(p.user_id, reason=reason if dx_changed else None) as cur:
            sets = ", ".join(f"{k} = %s" for k in row)
            cur.execute(f"UPDATE patient SET {sets} WHERE patient_id = %s", [*row.values(), pid])
            _write_missing(cur, pid, missing)
            if death:
                _insert_death(cur, pid, death, row["provenance"])
            if death_obs and row["status_last_contact"] != "dead":
                cur.execute("UPDATE observation SET deleted_at = now(), deleted_by = %s "
                            "WHERE obs_id = %s", (p.user_id, death_obs["obs_id"]))
    _db_call(write)
    return Result("saved", pid, _quality(row))


def find_by_iin(db: Database, p: Principal, iin: str) -> UUID | None:
    value = _iin(iin)
    cond, params = visible_patients_sql(p)
    with db.tx(p.user_id) as cur:
        cur.execute(f"SELECT patient_id FROM patient p WHERE iin = %s AND deleted_at IS NULL AND {cond}",
                    [value, *params])
        row = cur.fetchone()
    return row["patient_id"] if row else None


# ---------------------------------------------------------------------------
# Чтение: список и карточка
# ---------------------------------------------------------------------------
def _marker_map(markers: list[dict]) -> dict[str, str]:
    """Последний результат по каждому маркеру: значение или код пропуска."""
    out: dict[str, str] = {}
    for m in sorted(markers, key=lambda m: (m["test_date"] or date.min)):
        out[m["field_key"]] = m["value"] or m["missing"]
    return out


def _flags_for(patient: dict, markers, lines, observations) -> dict[str, list[str]]:
    completeness = core.completeness_flags({
        "who_haem5": patient["who_haem5"], "status_last_contact": patient["status_last_contact"],
        "module_data": _marker_map(markers),
        "therapy_lines": lines, "observations": observations})
    return {"completeness": completeness}


def patient_filter_sql(f: dict) -> tuple[list[str], list]:
    """Условия фильтров списка/когорты: нозология, филиал, пол, данные, статус, годы диагноза,
    стадия («система:стадия»). Значения — только параметрами запроса."""
    where, params = [], []
    for col, key in (("p.who_haem5", "nosology"), ("p.branch", "branch"), ("p.sex", "sex"),
                     ("p.provenance", "provenance"), ("p.status_last_contact", "status")):
        if f.get(key):
            where.append(f"{col} = %s")
            params.append(f[key])
    if f.get("year_from"):
        where.append("extract(year FROM p.dx_date) >= %s")
        params.append(f["year_from"])
    if f.get("year_to"):
        where.append("extract(year FROM p.dx_date) <= %s")
        params.append(f["year_to"])
    if f.get("stage") and ":" in f["stage"]:
        system, value = f["stage"].split(":", 1)
        where.append("p.stage_system = %s AND p.stage_value = %s")
        params += [system, value]
    if f.get("has"):                     # есть хотя бы одно исследование раздела
        where.append("EXISTS (SELECT 1 FROM investigation i WHERE i.patient_id = p.patient_id "
                     "AND i.kind = %s AND i.deleted_at IS NULL)")
        params.append(f["has"])
    return where, params


def _by_patient(rows: list[dict]) -> dict:
    out: dict = {}
    for r in rows:
        out.setdefault(r["patient_id"], []).append(r)
    return out


def list_patients(db: Database, p: Principal, limit: int | None = None, filters: dict | None = None) -> list[dict]:
    """Пациенты в пределах прав, с флагами полноты. filters — как у когорты аналитики
    (patient_filter_sql) и ещё completeness: complete | gaps."""
    f = filters or {}
    cond, params = visible_patients_sql(p)
    extra, extra_params = patient_filter_sql(f)
    where = " AND ".join(["p.deleted_at IS NULL", cond, *extra])
    with db.tx(p.user_id) as cur:
        iin_col = "p.iin, " if p.can("patient.iin") else ""
        cur.execute(f"""SELECT {iin_col}p.patient_id, p.branch, p.sex, p.who_haem5, p.mkb, p.dx_date,
                               p.dx_version, p.status_last_contact, p.last_contact_date,
                               p.stage_system, p.stage_value
                        FROM patient p WHERE {where}
                        ORDER BY p.entry_date DESC""" + (" LIMIT %s" if limit else ""),
                    [*params, *extra_params, *([limit] if limit else [])])
        patients = cur.fetchall()
        ids = [r["patient_id"] for r in patients]
        q = lambda table: (cur.execute(f"SELECT * FROM {table} WHERE patient_id = ANY(%s) AND deleted_at IS NULL",  # noqa: E731
                                       (ids,)), _by_patient(cur.fetchall()))[1]
        markers, lines, obs = q("module_data"), q("therapy_line"), q("observation")
    for r in patients:
        pid = r["patient_id"]
        r["flags"] = _flags_for(r, markers.get(pid, []), lines.get(pid, []), obs.get(pid, []))["completeness"]
        r["n_lines"] = len(lines.get(pid, []))
    if f.get("completeness") == "complete":
        patients = [r for r in patients if not r["flags"]]
    elif f.get("completeness") == "gaps":
        patients = [r for r in patients if r["flags"]]
    return patients


def get_card(db: Database, p: Principal, pid) -> dict:
    with db.tx(p.user_id) as cur:
        patient = _load_visible(cur, p, pid)
        if not p.can("patient.iin"):   # ИИН — только врачу филиала и куратору
            patient.pop("iin")
        q = lambda sql: (cur.execute(sql, (pid,)), cur.fetchall())[1]  # noqa: E731
        card = {
            "patient": patient,
            "diagnoses": q("SELECT * FROM diagnosis_version WHERE patient_id = %s ORDER BY version DESC"),
            "missing": {r["field"]: r["code"] for r in q("SELECT * FROM patient_missing WHERE patient_id = %s")},
            "branches": [r["branch_code"] for r in q("SELECT * FROM patient_branch WHERE patient_id = %s ORDER BY added_at")],
            "markers": q("SELECT * FROM module_data WHERE patient_id = %s AND deleted_at IS NULL "
                         "ORDER BY field_key, test_date NULLS FIRST"),
            "lines": q("SELECT * FROM therapy_line WHERE patient_id = %s AND deleted_at IS NULL ORDER BY line_no"),
            "observations": q("SELECT * FROM observation WHERE patient_id = %s AND deleted_at IS NULL "
                              "ORDER BY obs_date, created_at"),
            "labs": q("SELECT * FROM lab WHERE patient_id = %s AND deleted_at IS NULL ORDER BY obs_date, loinc"),
            "investigations": q("SELECT * FROM investigation WHERE patient_id = %s AND deleted_at IS NULL "
                                "ORDER BY kind, test_date NULLS FIRST, created_at"),
        }
    mm = _marker_map(card["markers"])
    card["marker_map"] = mm
    card["flags"] = _flags_for(patient, card["markers"], card["lines"], card["observations"])
    quality = set(core.validate_codes(patient))
    for o in card["observations"] or [{}]:
        quality.update(_quality({**patient, "response_date": o.get("obs_date") if o.get("response_value") else None,
                                 "event_date": o.get("event_date"), "event_type": o.get("event_type"),
                                 "death_cause": o.get("death_cause")}))
    from kalypso import investigations as inv
    quality.update(module_quality(patient, mm))
    quality.update(inv.genetics_mismatches(mm, card["investigations"]))
    card["inv"] = inv.by_kind(card["investigations"])
    vir = inv.virology(card["investigations"])
    card["flags"]["quality"] = sorted(quality)
    prof = profile(patient["who_haem5"])
    not_applicable = ({"stage_value"} if not prof["stages"] else set()) |                      ({"index_score"} if prof["index"] not in core.INDEXES else set())
    for name in CONDITIONAL_FIELDS:
        if patient.get(name) is None and name not in card["missing"] and name not in not_applicable:
            card["flags"]["completeness"].append(f"empty_{name}")
    card["profile"] = prof
    card["indices"] = computed_indices(patient, mm, card["labs"])
    card["module_info"] = module_info(patient, mm, card["lines"], card["observations"], card["markers"], vir)
    return card


# ---------------------------------------------------------------------------
# Клинические записи: линии, наблюдения, лабораторные, маркеры
# Добавление и правка идут через одни и те же разборщики полей (_*_row).
# ---------------------------------------------------------------------------
PROVENANCES = ("retrospective", "prospective")
_KINDS = {"line": ("therapy_line", "line_id"), "observation": ("observation", "obs_id"),
          "lab": ("lab", "lab_id"), "marker": ("module_data", "md_id"),
          "investigation": ("investigation", "inv_id")}


def _reason(f: Form) -> str | None:
    """Необязательная причина правки — уходит в audit_log.reason."""
    return f.text("change_reason", "Причина правки", maxlen=500)


def _update(cur, table: str, pk: str, rid, pid, row: dict) -> None:
    sets = ", ".join(f"{k} = %s" for k in row)
    cur.execute(f"UPDATE {table} SET {sets} WHERE {pk} = %s AND patient_id = %s AND deleted_at IS NULL",
                [*row.values(), rid, pid])
    if cur.rowcount == 0:
        raise NotFound()


def get_record(db: Database, p: Principal, pid, kind: str, rid) -> dict:
    """Запись для формы правки (только действующая и только видимого пациента)."""
    perm = "patient.write"
    p.require(perm)
    if kind not in _KINDS:
        raise NotFound()
    table, pk = _KINDS[kind]
    with db.tx(p.user_id) as cur:
        _load_visible(cur, p, pid, perm)
        cur.execute(f"SELECT * FROM {table} WHERE {pk} = %s AND patient_id = %s AND deleted_at IS NULL",
                    (rid, pid))
        row = cur.fetchone()
    if row is None:
        raise NotFound()
    return row


# --- линии терапии ----------------------------------------------------------
def _line_row(f: Form) -> dict:
    row = {
        "regimen": f.text("regimen", "Режим", required=True, maxlen=200),
        "drug": f.text("drug", "Препараты", maxlen=500),
        "dose": f.text("dose", "Доза", maxlen=200),
        "cycles": f.int("cycles", "Циклов", 0, 100),
        "route": f.text("route", "Путь введения", maxlen=100),
        "start_date": f.date("start_date", "Начало", required=True),
        "start_precision": f.precision("start_precision"),
        "end_date": f.date("end_date", "Окончание"),
        "end_precision": f.precision("end_precision"),
        "stop_reason": f.text("stop_reason", "Причина окончания", maxlen=300),
        "provenance": f.choice("provenance", "Ретро/проспектив", PROVENANCES),
    }
    if row["start_date"] and row["end_date"] and row["end_date"] < row["start_date"]:
        f.errors.append("Окончание линии раньше начала")
    return row


def _check_line_start(row: dict, patient: dict) -> None:
    if row["start_date"] < patient["dx_date"]:
        raise ValidationError(["Начало линии раньше даты диагноза"])


def add_line(db: Database, p: Principal, pid, data: dict) -> Result:
    p.require("patient.write")
    f = Form(data)
    row = _line_row(f)
    f.check()

    def write():
        with db.tx(p.user_id) as cur:
            patient = _load_visible(cur, p, pid, "patient.write")
            _check_line_start(row, patient)
            _inherit(row, patient)
            cur.execute("SELECT coalesce(max(line_no), 0) + 1 AS n FROM therapy_line WHERE patient_id = %s", (pid,))
            row.update(patient_id=pid, line_no=cur.fetchone()["n"], source="manual")
            return _insert(cur, "therapy_line", row, "line_id")
    return Result("saved", _db_call(write))


def update_line(db: Database, p: Principal, pid, rid, data: dict) -> Result:
    p.require("patient.write")
    f = Form(data)
    row, reason = _line_row(f), _reason(f)
    f.check()

    def write():
        with db.tx(p.user_id, reason=reason) as cur:
            patient = _load_visible(cur, p, pid, "patient.write")
            _check_line_start(row, patient)
            _update(cur, "therapy_line", "line_id", rid, pid, _inherit(row, patient))
    _db_call(write)
    return Result("saved", rid)


# --- наблюдения -------------------------------------------------------------
def _obs_row(f: Form, prof: dict) -> dict:
    row = {
        "obs_date": f.date("obs_date", "Дата наблюдения", required=True),
        "obs_precision": f.precision("obs_precision"),
        "line_id": f.raw("line_id"),
        "response_value": f.choice("response_value", f"Ответ ({prof['criteria']})", prof["responses"]),
        "response_basis": f.choice("response_basis", "Основание оценки",
                                   ("clinical", "ct", "pet_ct", "morphology", "laboratory", "mri")),
        "event_type": f.choice("event_type", "Событие",
                               ("none", "progression", "relapse", "death", "new_line_start"), default="none"),
        "event_date": f.date("event_date", "Дата события"),
        "death_cause": f.choice("death_cause", "Причина смерти", ("lpd_related", "not_related", "unknown")),
        "pet_ct_report": f.free_text("pet_ct_report", "Заключение ПЭТ-КТ"),
        "deauville": f.int("deauville", "Deauville", 1, 5),
        "pacs_accession": f.text("pacs_accession", "PACS accession", maxlen=200),
        "provenance": f.choice("provenance", "Ретро/проспектив", PROVENANCES),
    }
    if row["line_id"]:
        try:
            row["line_id"] = UUID(str(row["line_id"]))
        except ValueError:
            f.errors.append("Неизвестная линия терапии")
    row["response_criteria"] = prof["criteria"] if row["response_value"] else None
    if row["response_value"] and not row["response_basis"]:
        f.errors.append("Для ответа укажите основание оценки")
    elif not f.errors:
        f.errors += [RESPONSE_ERRORS[e] for e in core.check_response(
            row["response_criteria"], row["response_value"], row["response_basis"], row["deauville"])]
    if not row["response_value"]:
        row["response_basis"] = None
    if row["event_type"] == "none":
        row["event_date"] = None
    elif not row["event_date"]:
        row["event_date"] = row["obs_date"]
    if row["event_type"] == "death" and not row["death_cause"]:
        f.errors.append("Для события «смерть» укажите причину (допустимо «неизвестно»)")
    if row["event_type"] != "death":
        row["death_cause"] = None
    if not row["response_value"] and row["event_type"] == "none" and not row["pet_ct_report"]:
        f.errors.append("Наблюдение пустое: укажите ответ, событие или заключение ПЭТ-КТ")
    return row


RESPONSE_ERRORS = {
    "response_not_in_criteria": "Ответ не из критериев этой нозологии",
    "lugano_basis_mismatch": "Lugano 2014: по ПЭТ-КТ — метаболический ответ (CMR/PMR/NMR/PMD), "
                             "по КТ и другим основаниям — CR/PR/SD/PD",
    "deauville_required": "Для метаболического ответа укажите балл Deauville",
    "deauville_mismatch": "CMR — Deauville 1–3; PMR, NMR и PMD — Deauville 4–5",
}


def _obs_effects(cur, pid, patient: dict, row: dict) -> None:
    """Смерть закрывает наблюдение: статус и последний контакт — в той же транзакции."""
    if row["event_type"] == "death":
        cur.execute("""UPDATE patient SET status_last_contact = 'dead',
                           last_contact_date = greatest(last_contact_date, %s)
                       WHERE patient_id = %s""", (row["event_date"], pid))
    elif row["obs_date"] > patient["last_contact_date"]:
        cur.execute("UPDATE patient SET last_contact_date = %s WHERE patient_id = %s", (row["obs_date"], pid))


def _obs_warnings(patient: dict, row: dict) -> list[str]:
    return _quality({**patient, "response_date": row["obs_date"] if row["response_value"] else None,
                     "event_date": row["event_date"], "event_type": row["event_type"],
                     "death_cause": row["death_cause"]})


def add_observation(db: Database, p: Principal, pid, data: dict) -> Result:
    perm = "patient.write"
    p.require(perm)
    f = Form(data)

    def write():
        with db.tx(p.user_id) as cur:
            patient = _load_visible(cur, p, pid, perm)
            row = _inherit(_obs_row(f, profile(patient["who_haem5"])), patient)
            f.check()
            oid = _insert(cur, "observation", {**row, "patient_id": pid, "source": "manual"}, "obs_id")
            _obs_effects(cur, pid, patient, row)
            return oid, patient, row
    oid, patient, row = _db_call(write)
    return Result("saved", oid, _obs_warnings(patient, row))


def update_observation(db: Database, p: Principal, pid, rid, data: dict) -> Result:
    perm = "patient.write"
    p.require(perm)
    f = Form(data)
    reason = _reason(f)

    def write():
        with db.tx(p.user_id, reason=reason) as cur:
            patient = _load_visible(cur, p, pid, perm)
            row = _inherit(_obs_row(f, profile(patient["who_haem5"])), patient)
            f.check()
            cur.execute("SELECT event_type FROM observation WHERE obs_id = %s AND patient_id = %s "
                        "AND deleted_at IS NULL", (rid, pid))
            old = cur.fetchone()
            if old is None:
                raise NotFound()
            if old["event_type"] == "death" and row["event_type"] != "death":
                raise ValidationError(["Смерть отменяется сменой статуса в карточке пациента (Правка карточки)"])
            _update(cur, "observation", "obs_id", rid, pid, row)
            _obs_effects(cur, pid, patient, row)
            return patient, row
    patient, row = _db_call(write)
    return Result("saved", rid, _obs_warnings(patient, row))


# --- лабораторные -----------------------------------------------------------
def _lab_row(f: Form) -> tuple[dict, list[str]]:
    loinc = f.choice("loinc", "Показатель", [k for k in core.LAB_UNITS if not k.startswith("_")], required=True)
    value = f.num("value", "Значение", required=True)
    unit = f.text("unit", "Единица", required=True, maxlen=30)
    row = {"obs_date": f.date("obs_date", "Дата анализа", required=True), "loinc": loinc,
           "value_raw": value, "unit_raw": unit,
           "provenance": f.choice("provenance", "Ретро/проспектив", PROVENANCES)}
    if f.errors:
        return row, []
    c = core.canonicalize_lab(loinc, value, unit)  # исходное значение сохраняется рядом
    row.update(value_canonical=c.value_canonical, unit_canonical=c.unit_canonical, flag=c.flag)
    return row, [f"lab_{c.flag}"] if c.flag else []


def add_lab(db: Database, p: Principal, pid, data: dict) -> Result:
    perm = "patient.write"
    p.require(perm)
    f = Form(data)
    row, warnings = _lab_row(f)
    f.check()

    def write():
        with db.tx(p.user_id) as cur:
            _inherit(row, _load_visible(cur, p, pid, perm))
            return _insert(cur, "lab", {**row, "patient_id": pid, "source": "manual"}, "lab_id")
    return Result("saved", _db_call(write), warnings)


def update_lab(db: Database, p: Principal, pid, rid, data: dict) -> Result:
    perm = "patient.write"
    p.require(perm)
    f = Form(data)
    (row, warnings), reason = _lab_row(f), _reason(f)
    f.check()

    def write():
        with db.tx(p.user_id, reason=reason) as cur:
            _update(cur, "lab", "lab_id", rid, pid, _inherit(row, _load_visible(cur, p, pid, perm)))
    _db_call(write)
    return Result("saved", rid, warnings)


# --- маркеры ----------------------------------------------------------------
def _marker_row(f: Form, fields_list: list[dict], key: str | None = None) -> dict:
    fields = {cf["key"]: cf for cf in fields_list}
    if key is None:
        key = f.choice("field_key", "Маркер", list(fields), required=True)
    value = f.choice("value", "Результат", fields[key]["values"]) if key in fields else None
    missing = f.choice("missing", "Код пропуска", MISSING)
    if key and (value is None) == (missing is None):
        f.errors.append("Укажите либо результат, либо код пропуска")
    test_date = f.date("test_date", "Дата теста")
    return {"field_key": key, "value": value, "missing": missing, "test_date": test_date,
            "test_date_precision": f.precision("test_date_precision") if test_date else None,
            "provenance": f.choice("provenance", "Ретро/проспектив", PROVENANCES)}


def add_marker(db: Database, p: Principal, pid, data: dict) -> Result:
    perm = "patient.write"
    p.require(perm)
    f = Form(data)

    def write():
        with db.tx(p.user_id) as cur:
            patient = _load_visible(cur, p, pid, perm)
            prof = profile(patient["who_haem5"])
            row = _inherit(_marker_row(f, prof["fields"]), patient)
            f.check()
            return _insert(cur, "module_data", {**row, "patient_id": pid, "module": prof["module"],
                                                "source": "manual"}, "md_id")
    return Result("saved", _db_call(write))


def update_marker(db: Database, p: Principal, pid, rid, data: dict) -> Result:
    """Маркер остаётся тем же (ключ не меняется); правятся результат, код пропуска и дата."""
    p.require("patient.write")
    rec = get_record(db, p, pid, "marker", rid)
    f = Form(data)
    row, reason = _marker_row(f, _fields_of_module(rec["module"]), rec["field_key"]), _reason(f)
    f.check()

    def write():
        with db.tx(p.user_id, reason=reason) as cur:
            _update(cur, "module_data", "md_id", rid, pid, _inherit(row, _load_visible(cur, p, pid, "patient.write")))
    _db_call(write)
    return Result("saved", rid)



# --- исследования (ИФТ, генетика, вирусы, аутоиммунный скрининг) ----------------
def add_investigation(db: Database, p: Principal, pid, data: dict) -> Result:
    from kalypso import investigations as inv
    p.require("patient.write")
    f = Form(data)
    row = inv.row_from_form(f, f.raw("kind") or "")
    f.check()

    def write():
        with db.tx(p.user_id) as cur:
            _inherit(row, _load_visible(cur, p, pid, "patient.write"))
            return _insert(cur, "investigation", {**row, "patient_id": pid, "source": "manual"}, "inv_id")
    return Result("saved", _db_call(write))


def update_investigation(db: Database, p: Principal, pid, rid, data: dict) -> Result:
    """Раздел записи не меняется (ИФТ остаётся ИФТ); правятся исследование, дата, результат."""
    from kalypso import investigations as inv
    p.require("patient.write")
    kind = get_record(db, p, pid, "investigation", rid)["kind"]
    f = Form(data)
    row, reason = inv.row_from_form(f, kind), _reason(f)
    f.check()

    def write():
        with db.tx(p.user_id, reason=reason) as cur:
            _update(cur, "investigation", "inv_id", rid, pid, _inherit(row, _load_visible(cur, p, pid, "patient.write")))
    _db_call(write)
    return Result("saved", rid)

def soft_delete(db: Database, p: Principal, pid, kind: str, record_id) -> Result:
    """Мягкое удаление: запись остаётся в БД и аудите, из карточки и расчётов исчезает."""
    p.require("patient.write")
    if kind not in _KINDS:
        raise NotFound()
    table, pk = _KINDS[kind]

    def write():
        with db.tx(p.user_id) as cur:
            _load_visible(cur, p, pid, "patient.write")
            if kind == "line":
                cur.execute("SELECT 1 FROM observation WHERE line_id = %s AND deleted_at IS NULL", (record_id,))
                if cur.fetchone():
                    raise ValidationError(["Сначала удалите наблюдения по этой линии"])
            cur.execute(f"UPDATE {table} SET deleted_at = now(), deleted_by = %s "
                        f"WHERE {pk} = %s AND patient_id = %s AND deleted_at IS NULL",
                        (p.user_id, record_id, pid))
            if cur.rowcount == 0:
                raise NotFound()
    _db_call(write)
    return Result("deleted")


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
