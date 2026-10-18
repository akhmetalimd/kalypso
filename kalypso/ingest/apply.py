"""Применение данных МИС куратором (стадии 4–5 PRD §6.2). Автоприменения нет.

Каждый элемент промежуточной зоны решается отдельно: «применить» или «отклонить».
Элемент с проблемами маппинга применить нельзя — сначала правится конфиг маппинга,
затем данные приходят следующим прогоном. Применённое пишется через сервисы
регистра с source = 'his_structured' и source_record_id = HMAC записи МИС, поэтому
повторное применение не создаёт дублей, а «обновление» правит ту же запись.
"""
from __future__ import annotations

from datetime import date

from kalypso import documents, registry
from kalypso.ai.fields import DIAGNOSIS_CODES
from kalypso.db import Database
from kalypso.rbac import NotFound, Principal
from kalypso.registry import Result, ValidationError, _db_call

SOURCE = "his_structured"
KIND_LABELS = {"patient": "Новый пациент", "diagnosis": "Диагноз", "lab": "Анализ", "document": "Документ"}


def summary(db: Database, p: Principal) -> dict:
    if not (p.can("review.apply") or p.can("audit.read")):
        p.require("review.apply")
    with db.tx(p.user_id) as cur:
        cur.execute("SELECT * FROM ingest_run ORDER BY started_at DESC LIMIT 20")
        runs = cur.fetchall()
        cur.execute("SELECT source_system, last_until FROM ingest_state")
        state = cur.fetchall()
        cur.execute("""SELECT kind, status, count(*) AS n FROM ingest_item GROUP BY kind, status""")
        counts = cur.fetchall()
    return {"runs": runs, "state": state, "counts": counts}


def list_pending(db: Database, p: Principal, kind: str | None = None, limit: int = 200) -> list[dict]:
    p.require("review.apply")
    where, params = ["i.status = 'pending'"], []
    if kind in KIND_LABELS:
        where.append("i.kind = %s")
        params.append(kind)
    with db.tx(p.user_id) as cur:
        cur.execute(f"""SELECT i.*, coalesce(i.patient_id, patient_id_by_hash(i.iin_hash)) AS resolved_pid
                        FROM ingest_item i WHERE {' AND '.join(where)}
                        ORDER BY CASE i.kind WHEN 'patient' THEN 0 WHEN 'diagnosis' THEN 1 ELSE 2 END, i.created_at
                        LIMIT %s""", [*params, limit])
        return cur.fetchall()


def _provenance(day: str | date | None, entry: date | None) -> str:
    """Данные, возникшие до включения пациента в регистр, — ретроспективные."""
    d = date.fromisoformat(day) if isinstance(day, str) else day
    return "prospective" if d and entry and d >= entry else "retrospective"


def decide(db: Database, p: Principal, item_id, approve: bool, note: str | None = None,
           confirm_text: bool = False, ai_enabled: bool = False) -> Result:
    p.require("review.apply")
    note = (note or "").strip()[:500] or None
    with db.tx(p.user_id) as cur:
        cur.execute("""SELECT i.*, coalesce(i.patient_id, patient_id_by_hash(i.iin_hash)) AS resolved_pid
                       FROM ingest_item i WHERE i.item_id = %s""", (item_id,))
        item = cur.fetchone()
    if item is None:
        raise NotFound()
    if item["status"] != "pending":
        raise ValidationError(["По этому элементу уже есть решение"])
    applied = (None, None)
    if approve:
        if item["issues"]:
            raise ValidationError(["Есть проблемы маппинга — исправьте конфиг и дождитесь следующего прогона; "
                                   "элемент можно только отклонить"])
        applied = _apply(db, p, item, note, confirm_text, ai_enabled)

    def write():
        with db.tx(p.user_id) as cur:
            cur.execute("""UPDATE ingest_item SET status = %s, decided_by = %s, decided_at = now(), decision_note = %s,
                                  applied_table = %s, applied_id = %s,
                                  patient_id = coalesce(patient_id, %s)
                           WHERE item_id = %s AND status = 'pending'""",
                        ("applied" if approve else "rejected", p.user_id, note, applied[0], applied[1],
                         item["resolved_pid"] if item["kind"] != "patient" else (applied[1] if applied[1] else None),
                         item_id))
            if cur.rowcount == 0:
                raise ValidationError(["По этому элементу уже есть решение"])
    _db_call(write)
    return Result("applied" if approve else "rejected", item_id)


def _apply(db, p, item, note, confirm_text, ai_enabled):
    payload, pid, key = item["payload"], item["resolved_pid"], item["record_key"]
    reason = f"his.kz: элемент {str(item['item_id'])[:8]} принят куратором" + (f" — {note}" if note else "")
    if item["kind"] == "patient":
        pt, dx = payload["patient"], payload["diagnosis"]
        res = registry.register_from_source(db, p, item["iin_hash"], payload.get("hash_key_version", 1), pt["branch"], {
            "sex": pt["sex"], "birth_date": pt.get("birth_date"), "who_haem5": dx["who_haem5"],
            "icd_o_3_2": dx["icd_o_3_2"], "mkb": dx["mkb"], "dx_date": dx["dx_date"],
            "last_contact_date": pt["last_contact_date"], "provenance": "retrospective",
            "status_last_contact": "alive"}, source=SOURCE, source_record_id=key, reason=reason)
        return "patient", str(res.record_id)

    if pid is None:
        raise ValidationError(["Пациента ещё нет в регистре — сначала примите элемент «Новый пациент»"])
    card = registry.get_card(db, p, pid)
    entry = card["patient"]["entry_date"].date()

    if item["kind"] == "diagnosis":
        if not note:
            raise ValidationError(["Изменение диагноза — укажите причину в комментарии (сохранится в версии диагноза)"])
        current = (card["patient"]["who_haem5"], card["patient"]["icd_o_3_2"], card["patient"]["mkb"])
        new = (payload["who_haem5"], payload["icd_o_3_2"], payload["mkb"])
        if new not in DIAGNOSIS_CODES.values():
            raise ValidationError(["Коды диагноза из МИС не соответствуют дереву нозологий"])
        if current == new:
            return None, None
        registry.revise_diagnosis_codes(db, p, pid, *new, reason=reason, source=SOURCE)
        return "patient", str(pid)

    if item["kind"] == "lab":
        data = {"loinc": payload["loinc"], "value": payload["value"], "unit": payload["unit"],
                "obs_date": payload["obs_date"], "provenance": _provenance(payload["obs_date"], entry)}
        existing = next((l for l in card["labs"] if l["source"] == SOURCE and l["source_record_id"] == key), None)
        if existing:   # «обновление» записи МИС — правим ту же строку
            registry.update_lab(db, p, pid, existing["lab_id"], {**data, "change_reason": reason}, source=SOURCE)
            return "lab", str(existing["lab_id"])
        res = registry.add_lab(db, p, pid, data, source=SOURCE, source_record_id=key, reason=reason)
        return "lab", str(res.record_id)

    if item["kind"] == "document":
        if not confirm_text:
            raise ValidationError(["Подтвердите, что в очищенном тексте нет идентификаторов пациента"])
        res = documents.add_from_source(db, p, pid, payload["doc_type"], payload["doc_date"],
                                        _provenance(payload["doc_date"], entry), payload["text"],
                                        payload.get("_deid_report", {}), ai_enabled, source=SOURCE,
                                        source_record_id=key)
        return "source_document", str(res.record_id)
    raise ValidationError(["Неизвестный тип элемента"])
