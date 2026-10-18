"""Первичные документы: приём (деидентификация до сохранения), просмотр, ручная очередь.

Сырой текст живёт только в памяти запроса: в БД попадает очищенный текст и отчёт
о заменах (виды и число, без значений). К AI документ допускается, только если
автопроверка не нашла остатков идентификаторов И пользователь подтвердил текст.
"""
from __future__ import annotations

import json

from kalypso.db import Database
from kalypso.deid_text import DeidResult, deidentify
from kalypso.rbac import AccessDenied, NotFound, Principal, visible_patients_sql
from kalypso.registry import Form, Result, ValidationError, _db_call, _load_visible

DOC_TYPES = ("histology", "cytogenetics", "molecular", "pet_ct", "other")


def preview(raw_text: str) -> DeidResult:
    if not (raw_text or "").strip():
        raise ValidationError(["Вставьте текст заключения"])
    if len(raw_text) > 50000:
        raise ValidationError(["Текст длиннее 50 000 символов"])
    return deidentify(raw_text)


def _merge_report(prev_json: str | None, now: dict[str, int]) -> dict[str, int]:
    """Отчёт предпросмотра (из скрытого поля) + повторной очистки. Только числа по видам."""
    merged: dict[str, int] = {}
    try:
        prev = json.loads(prev_json) if prev_json else {}
    except json.JSONDecodeError:
        prev = {}
    for src in (prev if isinstance(prev, dict) else {}, now):
        for k, v in src.items():
            if isinstance(k, str) and isinstance(v, int) and 0 <= v < 10000 and len(k) <= 32:
                merged[k] = merged.get(k, 0) + v
    return merged


def add_document(db: Database, p: Principal, pid, data: dict, ai_enabled: bool) -> Result:
    p.require("documents.add")
    f = Form(data)
    doc_type = f.choice("doc_type", "Тип документа", DOC_TYPES, required=True)
    doc_date = f.date("doc_date", "Дата документа", required=True)
    provenance = f.choice("provenance", "Ретро/проспектив", ("retrospective", "prospective"), required=True)
    f.check()
    result = preview(str(f.raw("text") or ""))   # повторная очистка уже очищенного текста
    if not result.clean:
        # Текст с остатками идентификаторов в БД не сохраняем вовсе.
        raise ValidationError(["В тексте остались идентификаторы (ИИН, телефон или e-mail) — удалите их"])
    passed = f.raw("confirm") in ("1", "on", True)
    if passed and ai_enabled:
        status, note = "queued", None
    elif passed:
        status, note = "manual", "AI отключён — внесите данные вручную"
    else:
        status, note = "manual", "Текст не подтверждён пользователем — документ к AI не допущен"

    def write():
        with db.tx(p.user_id) as cur:
            _load_visible(cur, p, pid, "documents.add")
            cur.execute("""INSERT INTO source_document (patient_id, doc_type, doc_date, provenance, text_deid,
                                                        deid_report, deid_passed, status, status_note)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING doc_id""",
                        (pid, doc_type, doc_date, provenance, result.text,
                         json.dumps(_merge_report(f.raw("deid_report"), result.report)), passed, status, note))
            return cur.fetchone()["doc_id"]
    return Result(status, _db_call(write))


def list_documents(db: Database, p: Principal, pid) -> list[dict]:
    with db.tx(p.user_id) as cur:
        _load_visible(cur, p, pid)
        cur.execute("""SELECT d.doc_id, d.doc_type, d.doc_date, d.status, d.status_note, d.created_at,
                              count(a.*) FILTER (WHERE a.status = 'pending') AS pending,
                              count(a.*) FILTER (WHERE a.status IN ('accepted', 'edited')) AS applied
                       FROM source_document d LEFT JOIN ai_proposal a USING (doc_id)
                       WHERE d.patient_id = %s AND d.deleted_at IS NULL
                       GROUP BY d.doc_id ORDER BY d.doc_date DESC, d.created_at DESC""", (pid,))
        return cur.fetchall()


def get_document(db: Database, p: Principal, doc_id) -> dict:
    p.require("patient.read")
    cond, params = visible_patients_sql(p)
    with db.tx(p.user_id) as cur:
        cur.execute(f"""SELECT d.* FROM source_document d JOIN patient p USING (patient_id)
                        WHERE d.doc_id = %s AND d.deleted_at IS NULL AND {cond}""", [doc_id, *params])
        doc = cur.fetchone()
        if doc is None:
            raise NotFound()
        cur.execute("SELECT * FROM ai_proposal WHERE doc_id = %s ORDER BY created_at, target", (doc_id,))
        proposals = cur.fetchall()
        cur.execute("SELECT * FROM ai_run WHERE doc_id = %s ORDER BY started_at", (doc_id,))
        runs = cur.fetchall()
    return {"doc": doc, "proposals": proposals, "runs": runs}


def close_manual(db: Database, p: Principal, doc_id) -> Result:
    """Документ из ручной очереди обработан: данные внесены вручную (врач) или проверены (куратор)."""
    doc = get_document(db, p, doc_id)["doc"]
    if not (p.can("review.apply") or p.can("patient.write")):
        raise AccessDenied("documents.close")
    if doc["status"] != "manual":
        raise ValidationError(["Документ не в ручной очереди"])

    def write():
        with db.tx(p.user_id) as cur:
            cur.execute("UPDATE source_document SET status = 'reviewed', review_finished_at = now() "
                        "WHERE doc_id = %s AND status = 'manual'", (doc_id,))
            if cur.rowcount == 0:
                raise NotFound()
    _db_call(write)
    return Result("saved", doc_id)


def requeue(db: Database, p: Principal, doc_id) -> Result:
    """Повторная отправка в AI (например, AI был недоступен). Только куратор."""
    p.require("review.apply")
    doc = get_document(db, p, doc_id)["doc"]
    if doc["status"] != "manual" or not doc["deid_passed"]:
        raise ValidationError(["Повторно отправить можно только документ из ручной очереди, прошедший деидентификацию"])

    def write():
        with db.tx(p.user_id) as cur:
            cur.execute("UPDATE source_document SET status = 'queued', status_note = NULL "
                        "WHERE doc_id = %s AND status = 'manual'", (doc_id,))
    _db_call(write)
    return Result("queued", doc_id)


def add_from_source(db: Database, p: Principal, pid, doc_type: str, doc_date, provenance: str, text: str,
                    deid_report: dict, ai_enabled: bool, source: str = "his_structured",
                    source_record_id: str | None = None) -> Result:
    """Документ из МИС по решению куратора. Куратор проверил очищенный текст — это и есть
    подтверждение (deid_passed); повторная автоочистка и проверка остатков — как при ручном приёме."""
    p.require("review.apply")
    result = preview(text)
    if not result.clean:
        raise ValidationError(["В тексте остались идентификаторы (ИИН, телефон или e-mail)"])
    status, note = ("queued", None) if ai_enabled else ("manual", "AI отключён — внесите данные вручную")

    def write():
        with db.tx(p.user_id) as cur:
            _load_visible(cur, p, pid, "review.apply")
            cur.execute("""INSERT INTO source_document (patient_id, doc_type, doc_date, provenance, source,
                                                        source_record_id, text_deid, deid_report, deid_passed,
                                                        status, status_note)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s) RETURNING doc_id""",
                        (pid, doc_type, doc_date, provenance, source, source_record_id, result.text,
                         json.dumps(_merge_report(json.dumps(deid_report), result.report)), status, note))
            return cur.fetchone()["doc_id"]
    return Result(status, _db_call(write))
