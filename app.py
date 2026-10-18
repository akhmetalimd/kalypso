"""
Референс-API регистра ЛПЗ (FastAPI) — тонкая обёртка над registry_core + SQLite.

Назначение: показать форму платформы (ingest, автоканонизация лабораторных,
контроль качества, «Исследователь выживаемости»), а не готовый продукт.
Аутентификация, роли и UI аудита сознательно оставлены слою развёртывания
(REDCap или полноценный веб-фреймворк) — см. README.

Запуск:
    pip install fastapi uvicorn
    python -c "import sqlite3; sqlite3.connect('rglpz.db').executescript(open('schema.sqlite.sql').read())"
    uvicorn app:app --reload
"""

from __future__ import annotations
import sqlite3
from contextlib import closing
from datetime import date

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from registry_core import (
    canonicalize_lab, validate_record, survival_by_threshold,
    kaplan_meier, find_node, NOSOLOGY_TREE,
)

DB = "rglpz.db"
app = FastAPI(title="Регистр ЛПЗ — референс-API", version="1.0")


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


# --- Модели запросов ---------------------------------------------------------
class PatientIn(BaseModel):
    patient_id: str            # уже хэш от ИИН; сырой ИИН на сервер не передаётся
    branch: str
    sex: str
    who_haem5: str
    icd_o_3_2: str
    mkb: str
    dx_date: date
    last_contact_date: date
    provenance: str
    status_last_contact: str
    ecog: int | None = None
    stage_value: str | None = None
    stage_system: str | None = None


class LabIn(BaseModel):
    patient_id: str
    obs_date: date
    loinc: str
    value: float
    unit: str


# --- Эндпоинты ---------------------------------------------------------------
@app.get("/nosology")
def nosology_tree():
    """Дерево нозологий = навигация + реестр модулей."""
    return NOSOLOGY_TREE


@app.get("/nosology/{node_id}")
def nosology_node(node_id: str):
    node = find_node(NOSOLOGY_TREE, node_id)
    if not node:
        raise HTTPException(404, "узел не найден")
    return node


@app.post("/patients")
def add_patient(p: PatientIn):
    flags = validate_record(p.model_dump())          # контроль качества
    with closing(db()) as con:
        con.execute(
            """INSERT INTO patient
               (patient_id,branch,sex,who_haem5,icd_o_3_2,mkb,dx_date,
                last_contact_date,provenance,status_last_contact,ecog,
                stage_value,stage_system)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (p.patient_id, p.branch, p.sex, p.who_haem5, p.icd_o_3_2, p.mkb,
             p.dx_date.isoformat(), p.last_contact_date.isoformat(), p.provenance,
             p.status_last_contact, p.ecog, p.stage_value, p.stage_system))
        con.commit()
    return {"patient_id": p.patient_id, "quality_flags": flags}


@app.post("/labs")
def add_lab(l: LabIn):
    """Лабораторное значение приводится к канонической единице при вводе."""
    c = canonicalize_lab(l.loinc, l.value, l.unit)
    with closing(db()) as con:
        con.execute(
            """INSERT INTO lab
               (patient_id,obs_date,loinc,value_raw,unit_raw,
                value_canonical,unit_canonical,flag)
               VALUES (?,?,?,?,?,?,?,?)""",
            (l.patient_id, l.obs_date.isoformat(), l.loinc, l.value, l.unit,
             c.value_canonical, c.unit_canonical, c.flag))
        con.commit()
    return c.__dict__


@app.get("/survival")
def survival(analyte_loinc: str | None = None, threshold: float | None = None,
             who_haem5: str | None = None):
    """Кривая выживаемости. Если задан показатель+порог — две группы (песочница).
    Порог берётся из литературы/внешней когорты, не подбирается по этим данным."""
    with closing(db()) as con:
        base = "SELECT patient_id, time_zero, event_or_censor_date, event FROM v_survival"
        params: list = []
        if who_haem5:
            base += " WHERE who_haem5 = ?"
            params.append(who_haem5)
        pts = con.execute(base, params).fetchall()

        rows = []
        for r in pts:
            dur = (date.fromisoformat(r["event_or_censor_date"])
                   - date.fromisoformat(r["time_zero"])).days
            item = {"duration": dur, "event": r["event"]}
            if analyte_loinc is not None:
                lab = con.execute(
                    """SELECT value_canonical FROM lab
                       WHERE patient_id=? AND loinc=? AND value_canonical IS NOT NULL
                       ORDER BY obs_date LIMIT 1""",
                    (r["patient_id"], analyte_loinc)).fetchone()
                item["value"] = lab["value_canonical"] if lab else None
            rows.append(item)

    if analyte_loinc is not None and threshold is not None:
        return survival_by_threshold(rows, analyte_loinc, threshold)
    return kaplan_meier([r["duration"] for r in rows], [r["event"] for r in rows])
