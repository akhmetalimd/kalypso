"""Деидентифицированная выгрузка для аналитика (CSV, одна строка на пациента).

Не выгружаются: ИИН, точная дата рождения (только год и возраст на диагнозе),
свободный текст документов, ссылки на сканы и PACS. Время до события —
по v_survival (PFS-подобное; раздельные OS/PFS/EFS/TTNT — этап П3).
Log-rank/Cox считаются на этой выгрузке вне платформы.
"""
from __future__ import annotations

import csv
import io

from kalypso.db import Database
from kalypso.rbac import Principal
from kalypso.registry import _marker_map, core_fields

COLUMNS = [
    "patient_id", "branch", "sex", "birth_year", "age_at_dx", "who_haem5", "icd_o_3_2", "mkb",
    "dx_version", "dx_date", "dx_date_precision", "last_contact_date", "status_last_contact",
    "provenance", "ecog", "cirs_score", "cirs_fitness", "b_symptoms", "stage_system", "stage_value",
    "index_name", "index_score", "index_group", "n_lines", "pfs_event", "pfs_days",
]


def patients_csv(db: Database, p: Principal) -> str:
    p.require("export.deid")
    markers_keys = [f["key"] for f in core_fields()]
    with db.tx(p.user_id) as cur:
        cur.execute("""SELECT pt.*, s.event AS pfs_event,
                              (s.event_or_censor_date - s.time_zero) AS pfs_days,
                              (SELECT count(*) FROM therapy_line l
                                WHERE l.patient_id = pt.patient_id AND l.deleted_at IS NULL) AS n_lines
                       FROM patient pt JOIN v_survival s USING (patient_id)
                       WHERE pt.deleted_at IS NULL ORDER BY pt.entry_date""")
        patients = cur.fetchall()
        cur.execute("SELECT * FROM module_data WHERE deleted_at IS NULL")
        markers = cur.fetchall()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS + markers_keys)
    for r in patients:
        bd, dx = r["birth_date"], r["dx_date"]
        r["birth_year"] = bd.year if bd else None
        r["age_at_dx"] = round((dx - bd).days / 365.25, 1) if bd else None
        mm = _marker_map([m for m in markers if m["patient_id"] == r["patient_id"]])
        w.writerow([r.get(c) for c in COLUMNS] + [mm.get(k) for k in markers_keys])
    return buf.getvalue()
