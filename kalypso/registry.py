"""Доменные сервисы регистра (П1, модуль ХЛЛ): единственный путь записи в регистр.

Каждая функция принимает пользователя (Principal), проверяет права и видимость
пациента, приводит и проверяет ввод, пишет в одной транзакции и возвращает
предупреждения контроля качества. Последний рубеж — ограничения БД (0002).
На этапе П4 этот же слой станет стадией 5 конвейера приёма (запись после сверки).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

import psycopg

import registry_core as core
from kalypso.db import Database, db_error_message
from kalypso.deid import IinHasher, InvalidIIN, find_identifiers
from kalypso.rbac import NotFound, Principal, visible_patients_sql

CLL = "cll_sll"
MISSING = sorted(core.MISSING_CODES)
PRECISIONS = ("day", "month", "year")
# «Условные» поля пациента: пусто без кода пропуска -> флаг полноты.
CONDITIONAL_FIELDS = {
    "birth_date": "Дата рождения", "ecog": "ECOG", "cirs_score": "CIRS-G",
    "b_symptoms": "B-симптомы", "stage_value": "Стадия", "index_score": "Индекс CLL-IPI",
}
DX_FIELDS = ("who_haem5", "icd_o_3_2", "mkb", "dx_date", "dx_date_precision")
LOINC_B2M = "1952-1"
IWCLL_RESPONSES = ("CR", "CRi", "nPR", "PR", "PR-L", "SD", "PD")


class ValidationError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


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


def core_fields(who_haem5: str = CLL) -> list[dict]:
    node = core.find_node(core.NOSOLOGY_TREE, who_haem5) or {}
    return node.get("core_fields", [])


# ---------------------------------------------------------------------------
# Приведение ввода (строки форм или значения Python)
# ---------------------------------------------------------------------------
class Form:
    def __init__(self, data: dict):
        self.data = {k: (v.strip() if isinstance(v, str) else v) for k, v in data.items()}
        self.errors: list[str] = []

    def raw(self, name: str):
        v = self.data.get(name)
        return None if v == "" else v

    def text(self, name: str, label: str, required=False, maxlen=20000) -> str | None:
        v = self.raw(name)
        if v is None:
            if required:
                self.errors.append(f"{label}: обязательное поле")
            return None
        v = str(v)
        if len(v) > maxlen:
            self.errors.append(f"{label}: не длиннее {maxlen} символов")
        return v

    def choice(self, name: str, label: str, allowed, required=False, default=None) -> str | None:
        v = self.raw(name)
        if v is None:
            if required and default is None:
                self.errors.append(f"{label}: выберите значение")
            return default
        if v not in allowed:
            self.errors.append(f"{label}: недопустимое значение")
            return None
        return v

    def date(self, name: str, label: str, required=False) -> date | None:
        v = self.raw(name)
        if v is None:
            if required:
                self.errors.append(f"{label}: обязательное поле")
            return None
        try:
            return core._as_date(v)
        except ValueError:
            self.errors.append(f"{label}: дата в формате ГГГГ-ММ-ДД")
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
            self.errors.append(f"{label}: целое число")
            return None
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            self.errors.append(f"{label}: от {lo} до {hi}")
            return None
        return n

    def num(self, name: str, label: str, required=False) -> float | None:
        v = self.raw(name)
        if v is None:
            if required:
                self.errors.append(f"{label}: обязательное поле")
            return None
        try:
            return float(str(v).replace(",", "."))
        except ValueError:
            self.errors.append(f"{label}: число")
            return None

    def free_text(self, name: str, label: str) -> str | None:
        """Свободный текст документа: идентификаторы не допускаются (CLAUDE.md, п. 4)."""
        v = self.text(name, label)
        found = find_identifiers(v)
        if found:
            kinds = {"iin": "ИИН (12 цифр)", "phone": "номер телефона", "email": "e-mail"}
            self.errors.append(f"{label}: удалите идентификаторы — " + ", ".join(kinds[k] for k in found))
        return v

    def check(self) -> None:
        if self.errors:
            raise ValidationError(self.errors)


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
        "provenance": f.choice("provenance", "Ретро/проспектив", ("retrospective", "prospective"), required=True),
        "status_last_contact": f.choice("status_last_contact", "Статус",
                                        ("alive", "dead", "lost_to_follow_up"), required=True),
        "ecog": f.int("ecog", "ECOG", 0, 4),
        "cirs_score": f.int("cirs_score", "CIRS-G", 0, 56),
        "cirs_fitness": f.choice("cirs_fitness", "Fitness", ("fit", "unfit", "frail")),
        "charlson_score": f.int("charlson_score", "Charlson", 0, 40),
        "b_symptoms": f.choice("b_symptoms", "B-симптомы", ("yes", "no", "unknown")),
        "stage_system": f.choice("stage_system", "Система стадирования", ("Rai", "Binet")),
        "stage_value": f.text("stage_value", "Стадия", maxlen=8),
        "index_score": f.int("index_score", "Балл CLL-IPI", 0, 10),
        "index_group": f.choice("index_group", "Группа CLL-IPI", ("low", "intermediate", "high", "very_high")),
        "histology_report": f.free_text("histology_report", "Заключение гистологии"),
        "histology_scan": f.text("histology_scan", "Ссылка на скан", maxlen=500),
    }
    row["index_name"] = "CLL-IPI" if row["index_score"] is not None else None
    for flag in core.validate_codes(row):
        f.errors.append({
            "icd_o_mismatch": "Код ICD-O-3.2 не соответствует выбранной нозологии",
            "mkb_mismatch": "Код МКБ-10 не соответствует выбранной нозологии",
            "code_pair_mismatch": "Коды ICD-O и МКБ относятся к разным вариантам нозологии",
        }.get(flag, flag))
    if row["dx_date"] and row["last_contact_date"] and row["last_contact_date"] < row["dx_date"]:
        f.errors.append("Последний контакт не может быть раньше даты диагноза")
    return row


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


def register_patient(db: Database, hasher: IinHasher, p: Principal, data: dict) -> Result:
    """Новый пациент. ИИН -> хэш; при совпадении хэша — дедупликация без раскрытия данных."""
    p.require("patient.write")
    f = Form(data)
    try:
        iin_hash = hasher.hash(str(f.raw("iin") or ""))
    except InvalidIIN as e:
        raise ValidationError([str(e)]) from None

    with db.tx(p.user_id) as cur:
        # Под RLS врач не видит пациентов чужого филиала; функция БД сообщает только факт и id.
        cur.execute("SELECT patient_id_by_hash(%s) AS pid", (iin_hash,))
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
    row.update(iin_hash=iin_hash, hash_key_version=hasher.key_version,
               branch=p.branch_code, source="manual")

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


def register_from_source(db: Database, p: Principal, iin_hash: str, hash_key_version: int, branch: str,
                         data: dict, source: str = "his_structured", source_record_id: str | None = None,
                         reason: str | None = None) -> Result:
    """Пациент из внешнего источника (МИС) по решению куратора: ИИН у куратора нет — только хэш,
    посчитанный коннектором тем же ключом. Филиал — из справочника учреждений маппинга."""
    perm = write_perm(source)
    p.require(perm)
    f = Form(data)
    row = _patient_fields(f)
    f.check()
    row.update(iin_hash=iin_hash, hash_key_version=hash_key_version, branch=branch, source=source,
               source_record_id=source_record_id)

    def write():
        with db.tx(p.user_id, reason=reason) as cur:
            cur.execute("SELECT patient_id_by_hash(%s) AS pid", (iin_hash,))
            existing = cur.fetchone()["pid"]
            if existing:
                return existing, "exists"
            cur.execute("SELECT 1 FROM branch WHERE branch_code = %s AND is_active", (branch,))
            if not cur.fetchone():
                raise ValidationError([f"Филиал {branch} не найден или отключён"])
            return _insert(cur, "patient", row, "patient_id"), "created"
    pid, status = _db_call(write)
    return Result(status, pid, _quality(row))


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


def find_by_iin(db: Database, hasher: IinHasher, p: Principal, iin: str) -> UUID | None:
    try:
        h = hasher.hash(iin)
    except InvalidIIN as e:
        raise ValidationError([str(e)]) from None
    cond, params = visible_patients_sql(p)
    with db.tx(p.user_id) as cur:
        cur.execute(f"SELECT patient_id FROM patient p WHERE iin_hash = %s AND deleted_at IS NULL AND {cond}",
                    [h, *params])
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


def _tp53_abnormal(mm: dict[str, str]) -> bool | None:
    tp53, del17p = mm.get("TP53"), mm.get("del17p")
    if tp53 == "mutated" or del17p == "detected":
        return True
    normal = {"wild_type", "not_detected", "normal_negative"}
    if tp53 in normal and del17p in normal:
        return False
    return None


def _age(birth: date | None, at: date | None) -> float | None:
    if not birth or not at:
        return None
    return (at - birth).days / 365.25


def _flags_for(patient: dict, markers, lines, observations) -> dict[str, list[str]]:
    completeness = core.completeness_flags({
        "who_haem5": patient["who_haem5"], "status_last_contact": patient["status_last_contact"],
        "module_data": _marker_map(markers),
        "therapy_lines": lines, "observations": observations})
    return {"completeness": completeness}


def list_patients(db: Database, p: Principal, limit: int = 500) -> list[dict]:
    cond, params = visible_patients_sql(p)
    with db.tx(p.user_id) as cur:
        cur.execute(f"""SELECT p.patient_id, p.branch, p.sex, p.who_haem5, p.mkb, p.dx_date,
                               p.dx_version, p.status_last_contact, p.last_contact_date,
                               p.stage_system, p.stage_value
                        FROM patient p WHERE p.deleted_at IS NULL AND {cond}
                        ORDER BY p.entry_date DESC LIMIT %s""", [*params, limit])
        patients = cur.fetchall()
        ids = [r["patient_id"] for r in patients]
        cur.execute("SELECT * FROM module_data WHERE patient_id = ANY(%s) AND deleted_at IS NULL", (ids,))
        markers = cur.fetchall()
        cur.execute("SELECT * FROM therapy_line WHERE patient_id = ANY(%s) AND deleted_at IS NULL", (ids,))
        lines = cur.fetchall()
        cur.execute("SELECT * FROM observation WHERE patient_id = ANY(%s) AND deleted_at IS NULL", (ids,))
        obs = cur.fetchall()
    for r in patients:
        pid = r["patient_id"]
        r["flags"] = _flags_for(r, [m for m in markers if m["patient_id"] == pid],
                                [l for l in lines if l["patient_id"] == pid],
                                [o for o in obs if o["patient_id"] == pid])["completeness"]
        r["n_lines"] = sum(1 for l in lines if l["patient_id"] == pid)
    return patients


def get_card(db: Database, p: Principal, pid) -> dict:
    with db.tx(p.user_id) as cur:
        patient = _load_visible(cur, p, pid)
        patient.pop("iin_hash")  # псевдоним не показываем и не отдаём в шаблоны
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
            "documents": q("""SELECT d.doc_id, d.doc_type, d.doc_date, d.status, d.status_note,
                                     count(a.*) FILTER (WHERE a.status = 'pending') AS pending,
                                     count(a.*) FILTER (WHERE a.status IN ('accepted', 'edited')) AS applied
                              FROM source_document d LEFT JOIN ai_proposal a USING (doc_id)
                              WHERE d.patient_id = %s AND d.deleted_at IS NULL
                              GROUP BY d.doc_id ORDER BY d.doc_date DESC, d.created_at DESC"""),
        }
    mm = _marker_map(card["markers"])
    card["marker_map"] = mm
    card["flags"] = _flags_for(patient, card["markers"], card["lines"], card["observations"])
    quality = set(core.validate_codes(patient))
    for o in card["observations"] or [{}]:
        quality.update(_quality({**patient, "response_date": o.get("obs_date") if o.get("response_value") else None,
                                 "event_date": o.get("event_date"), "event_type": o.get("event_type"),
                                 "death_cause": o.get("death_cause")}))
    card["flags"]["quality"] = sorted(quality)
    for name in CONDITIONAL_FIELDS:
        if patient.get(name) is None and name not in card["missing"]:
            card["flags"]["completeness"].append(f"empty_{name}")
    b2m = next((l["value_canonical"] for l in card["labs"]
                if l["loinc"] == LOINC_B2M and l["value_canonical"] is not None), None)
    card["cll_ipi"] = core.cll_ipi(
        _age(patient["birth_date"], patient["dx_date"]), patient["stage_system"], patient["stage_value"],
        float(b2m) if b2m is not None else None, mm.get("IGHV"), _tp53_abnormal(mm))
    return card


# ---------------------------------------------------------------------------
# Клинические записи: линии, наблюдения, лабораторные, маркеры
# Добавление и правка идут через одни и те же разборщики полей (_*_row).
# ---------------------------------------------------------------------------
PROVENANCES = ("retrospective", "prospective")
_KINDS = {"line": ("therapy_line", "line_id"), "observation": ("observation", "obs_id"),
          "lab": ("lab", "lab_id"), "marker": ("module_data", "md_id")}


CURATED_SOURCES = ("ai_document", "his_structured")


def write_perm(source: str) -> str:
    """Ручной ввод — право врача; запись по принятому AI-предложению или данным МИС —
    право куратора (review.apply), только с соответствующим источником."""
    return "review.apply" if source in CURATED_SOURCES else "patient.write"


def _reason(f: Form) -> str | None:
    """Необязательная причина правки — уходит в audit_log.reason."""
    return f.text("change_reason", "Причина правки", maxlen=500)


def _update(cur, table: str, pk: str, rid, pid, row: dict) -> None:
    sets = ", ".join(f"{k} = %s" for k in row)
    cur.execute(f"UPDATE {table} SET {sets} WHERE {pk} = %s AND patient_id = %s AND deleted_at IS NULL",
                [*row.values(), rid, pid])
    if cur.rowcount == 0:
        raise NotFound()


def get_record(db: Database, p: Principal, pid, kind: str, rid, source: str = "manual") -> dict:
    """Запись для формы правки (только действующая и только видимого пациента)."""
    perm = write_perm(source)
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
        "provenance": f.choice("provenance", "Ретро/проспектив", PROVENANCES, required=True),
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
            _check_line_start(row, _load_visible(cur, p, pid, "patient.write"))
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
            _check_line_start(row, _load_visible(cur, p, pid, "patient.write"))
            _update(cur, "therapy_line", "line_id", rid, pid, row)
    _db_call(write)
    return Result("saved", rid)


# --- наблюдения -------------------------------------------------------------
def _obs_row(f: Form) -> dict:
    row = {
        "obs_date": f.date("obs_date", "Дата наблюдения", required=True),
        "obs_precision": f.precision("obs_precision"),
        "line_id": f.raw("line_id"),
        "response_value": f.choice("response_value", "Ответ", IWCLL_RESPONSES),
        "response_basis": f.choice("response_basis", "Основание оценки", ("clinical", "ct", "pet_ct", "morphology")),
        "event_type": f.choice("event_type", "Событие",
                               ("none", "progression", "relapse", "death", "new_line_start"), default="none"),
        "event_date": f.date("event_date", "Дата события"),
        "death_cause": f.choice("death_cause", "Причина смерти", ("lpd_related", "not_related", "unknown")),
        "pet_ct_report": f.free_text("pet_ct_report", "Заключение ПЭТ-КТ"),
        "deauville": f.int("deauville", "Deauville", 1, 5),
        "pacs_accession": f.text("pacs_accession", "PACS accession", maxlen=200),
        "provenance": f.choice("provenance", "Ретро/проспектив", PROVENANCES, required=True),
    }
    if row["line_id"]:
        try:
            row["line_id"] = UUID(str(row["line_id"]))
        except ValueError:
            f.errors.append("Неизвестная линия терапии")
    row["response_criteria"] = "iwCLL 2018" if row["response_value"] else None
    if row["response_value"] and not row["response_basis"]:
        f.errors.append("Для ответа укажите основание оценки")
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


def add_observation(db: Database, p: Principal, pid, data: dict, source: str = "manual",
                    source_record_id: str | None = None, reason: str | None = None) -> Result:
    perm = write_perm(source)
    p.require(perm)
    f = Form(data)
    row = _obs_row(f)
    f.check()

    def write():
        with db.tx(p.user_id, reason=reason) as cur:
            patient = _load_visible(cur, p, pid, perm)
            oid = _insert(cur, "observation", {**row, "patient_id": pid, "source": source,
                                               "source_record_id": source_record_id}, "obs_id")
            _obs_effects(cur, pid, patient, row)
            return oid, patient
    oid, patient = _db_call(write)
    return Result("saved", oid, _obs_warnings(patient, row))


def update_observation(db: Database, p: Principal, pid, rid, data: dict, source: str = "manual") -> Result:
    perm = write_perm(source)
    p.require(perm)
    f = Form(data)
    row, reason = _obs_row(f), _reason(f)
    f.check()

    def write():
        with db.tx(p.user_id, reason=reason) as cur:
            patient = _load_visible(cur, p, pid, perm)
            cur.execute("SELECT event_type FROM observation WHERE obs_id = %s AND patient_id = %s "
                        "AND deleted_at IS NULL", (rid, pid))
            old = cur.fetchone()
            if old is None:
                raise NotFound()
            if old["event_type"] == "death" and row["event_type"] != "death":
                raise ValidationError(["Смерть отменяется сменой статуса в карточке пациента (Правка карточки)"])
            _update(cur, "observation", "obs_id", rid, pid, row)
            _obs_effects(cur, pid, patient, row)
            return patient
    patient = _db_call(write)
    return Result("saved", rid, _obs_warnings(patient, row))


# --- лабораторные -----------------------------------------------------------
def _lab_row(f: Form) -> tuple[dict, list[str]]:
    loinc = f.choice("loinc", "Показатель", [k for k in core.LAB_UNITS if not k.startswith("_")], required=True)
    value = f.num("value", "Значение", required=True)
    unit = f.text("unit", "Единица", required=True, maxlen=30)
    row = {"obs_date": f.date("obs_date", "Дата анализа", required=True), "loinc": loinc,
           "value_raw": value, "unit_raw": unit,
           "provenance": f.choice("provenance", "Ретро/проспектив", PROVENANCES, required=True)}
    if f.errors:
        return row, []
    c = core.canonicalize_lab(loinc, value, unit)  # исходное значение сохраняется рядом
    row.update(value_canonical=c.value_canonical, unit_canonical=c.unit_canonical, flag=c.flag)
    return row, [f"lab_{c.flag}"] if c.flag else []


def add_lab(db: Database, p: Principal, pid, data: dict, source: str = "manual",
            source_record_id: str | None = None, reason: str | None = None) -> Result:
    perm = write_perm(source)
    p.require(perm)
    f = Form(data)
    row, warnings = _lab_row(f)
    f.check()

    def write():
        with db.tx(p.user_id, reason=reason) as cur:
            _load_visible(cur, p, pid, perm)
            return _insert(cur, "lab", {**row, "patient_id": pid, "source": source,
                                        "source_record_id": source_record_id}, "lab_id")
    return Result("saved", _db_call(write), warnings)


def update_lab(db: Database, p: Principal, pid, rid, data: dict, source: str = "manual") -> Result:
    perm = write_perm(source)
    p.require(perm)
    f = Form(data)
    (row, warnings), reason = _lab_row(f), _reason(f)
    f.check()

    def write():
        with db.tx(p.user_id, reason=reason) as cur:
            _load_visible(cur, p, pid, perm)
            _update(cur, "lab", "lab_id", rid, pid, row)
    _db_call(write)
    return Result("saved", rid, warnings)


# --- маркеры ----------------------------------------------------------------
def _marker_row(f: Form, key: str | None = None) -> dict:
    fields = {cf["key"]: cf for cf in core_fields(CLL)}
    if key is None:
        key = f.choice("field_key", "Маркер", list(fields), required=True)
    value = f.choice("value", "Результат", fields[key]["values"]) if key in fields else None
    missing = f.choice("missing", "Код пропуска", MISSING)
    if key and (value is None) == (missing is None):
        f.errors.append("Укажите либо результат, либо код пропуска")
    test_date = f.date("test_date", "Дата теста")
    return {"field_key": key, "value": value, "missing": missing, "test_date": test_date,
            "test_date_precision": f.precision("test_date_precision") if test_date else None,
            "provenance": f.choice("provenance", "Ретро/проспектив", PROVENANCES, required=True)}


def add_marker(db: Database, p: Principal, pid, data: dict, source: str = "manual",
               source_record_id: str | None = None, reason: str | None = None) -> Result:
    perm = write_perm(source)
    p.require(perm)
    f = Form(data)
    row = _marker_row(f)
    f.check()

    def write():
        with db.tx(p.user_id, reason=reason) as cur:
            _load_visible(cur, p, pid, perm)
            return _insert(cur, "module_data", {**row, "patient_id": pid, "module": "cll", "source": source,
                                                "source_record_id": source_record_id}, "md_id")
    return Result("saved", _db_call(write))


def update_marker(db: Database, p: Principal, pid, rid, data: dict) -> Result:
    """Маркер остаётся тем же (ключ не меняется); правятся результат, код пропуска и дата."""
    p.require("patient.write")
    key = get_record(db, p, pid, "marker", rid)["field_key"]
    f = Form(data)
    row, reason = _marker_row(f, key), _reason(f)
    f.check()

    def write():
        with db.tx(p.user_id, reason=reason) as cur:
            _load_visible(cur, p, pid, "patient.write")
            _update(cur, "module_data", "md_id", rid, pid, row)
    _db_call(write)
    return Result("saved", rid)


def revise_diagnosis_codes(db: Database, p: Principal, pid, who_haem5: str, icd_o_3_2: str, mkb: str,
                           reason: str, source: str = "manual") -> Result:
    """Пересмотр кодов диагноза (дата диагноза не меняется). Версия диагноза — в БД."""
    perm = write_perm(source)
    p.require(perm)
    flags = core.validate_codes({"who_haem5": who_haem5, "icd_o_3_2": icd_o_3_2, "mkb": mkb})
    if flags:
        raise ValidationError([f"Коды диагноза не согласованы: {', '.join(flags)}"])

    def write():
        with db.tx(p.user_id, reason=reason) as cur:
            _load_visible(cur, p, pid, perm)
            cur.execute("UPDATE patient SET who_haem5 = %s, icd_o_3_2 = %s, mkb = %s WHERE patient_id = %s",
                        (who_haem5, icd_o_3_2, mkb, pid))
    _db_call(write)
    return Result("saved", pid)


def set_histology_report(db: Database, p: Principal, pid, text: str, source: str = "manual",
                         reason: str | None = None) -> Result:
    perm = write_perm(source)
    p.require(perm)
    f = Form({"histology_report": text})
    value = f.free_text("histology_report", "Заключение гистологии")
    f.check()

    def write():
        with db.tx(p.user_id, reason=reason) as cur:
            _load_visible(cur, p, pid, perm)
            cur.execute("UPDATE patient SET histology_report = %s WHERE patient_id = %s", (value, pid))
    _db_call(write)
    return Result("saved", pid)


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
