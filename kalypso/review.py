"""Сверка AI-предложений куратором (стадия 4 PRD §6.2) — шлюз качества.

Каждое предложение решается отдельно: принять, исправить и принять, отклонить.
Пакетного принятия нет. Принятое пишется через сервисы регистра с
source = 'ai_document' и source_record_id = id предложения (или документа — для
наблюдения ПЭТ-КТ), поэтому повторное применение не создаёт дублей.
"""
from __future__ import annotations

import json

from kalypso import registry
from kalypso.ai.fields import (DIAGNOSIS_CODES, HISTOLOGY_TEXT, MARKER_TARGETS, OBSERVATION_TARGETS,
                               allowed_values)
from kalypso.db import Database
from kalypso.documents import get_document
from kalypso.rbac import NotFound, Principal
from kalypso.registry import Result, ValidationError, _db_call

SOURCE = "ai_document"
ACTIONS = ("accept", "edit", "reject")


def list_queue(db: Database, p: Principal) -> dict:
    p.require("review.apply")
    with db.tx(p.user_id) as cur:
        cur.execute("""SELECT d.doc_id, d.patient_id, d.doc_type, d.doc_date, d.status, d.status_note, d.created_at,
                              pt.branch,
                              count(a.*) FILTER (WHERE a.status = 'pending') AS pending,
                              count(a.*) FILTER (WHERE a.status = 'pending' AND a.conflict) AS conflicts,
                              count(a.*) FILTER (WHERE a.status = 'pending' AND NOT a.grounded) AS ungrounded
                       FROM source_document d JOIN patient pt USING (patient_id)
                       LEFT JOIN ai_proposal a USING (doc_id)
                       WHERE d.deleted_at IS NULL AND d.status IN ('proposed', 'manual', 'queued', 'processing')
                       GROUP BY d.doc_id, pt.branch ORDER BY d.created_at""")
        rows = cur.fetchall()
    return {"proposed": [r for r in rows if r["status"] == "proposed"],
            "manual": [r for r in rows if r["status"] == "manual"],
            "waiting": [r for r in rows if r["status"] in ("queued", "processing")]}


def segments(text: str, proposals: list[dict]) -> list[dict]:
    """Текст документа, разбитый на куски с подсветкой цитат (без пересечений)."""
    spans = sorted((pr["evidence_start"], pr["evidence_end"], i) for i, pr in enumerate(proposals)
                   if pr["evidence_start"] is not None)
    out, pos = [], 0
    for start, end, i in spans:
        if start < pos:
            continue
        if start > pos:
            out.append({"text": text[pos:start], "mark": None})
        out.append({"text": text[start:end], "mark": i + 1})
        pos = end
    if pos < len(text):
        out.append({"text": text[pos:], "mark": None})
    return out


def get_review(db: Database, p: Principal, doc_id) -> dict:
    p.require("review.apply")
    data = get_document(db, p, doc_id)
    doc = data["doc"]
    if doc["status"] == "proposed" and doc["review_started_at"] is None:
        with db.tx(p.user_id) as cur:   # начало сверки — для метрики нагрузки на куратора
            cur.execute("UPDATE source_document SET review_started_at = now() WHERE doc_id = %s "
                        "AND review_started_at IS NULL", (doc_id,))
    data["segments"] = segments(doc["text_deid"], data["proposals"])
    data["card"] = registry.get_card(db, p, doc["patient_id"])
    return data


def _value(j):
    return None if j is None else j.get("value")


def _apply(db: Database, p: Principal, doc: dict, proposal: dict, value: str) -> tuple[str | None, str | None]:
    """Запись принятого значения в регистр. Возвращает (таблица, id записи)."""
    pid, target, short = doc["patient_id"], proposal["target"], str(proposal["proposal_id"])[:8]
    reason = f"AI-предложение {short} принято куратором"
    if target in MARKER_TARGETS:
        res = registry.add_marker(db, p, pid, {"field_key": target, "value": value, "test_date": doc["doc_date"],
                                               "provenance": doc["provenance"]},
                                  source=SOURCE, source_record_id=str(proposal["proposal_id"]), reason=reason)
        return "module_data", str(res.record_id)
    if target == "diagnosis":
        who, icd, mkb = DIAGNOSIS_CODES[value]
        card = registry.get_card(db, p, pid)["patient"]
        if (card["who_haem5"], card["icd_o_3_2"], card["mkb"]) == (who, icd, mkb):
            return None, None       # в регистре уже так — записывать нечего
        registry.revise_diagnosis_codes(db, p, pid, who, icd, mkb, reason=reason, source=SOURCE)
        return "patient", str(pid)
    if target == HISTOLOGY_TEXT:
        registry.set_histology_report(db, p, pid, value, source=SOURCE, reason=reason)
        return "patient", str(pid)
    if target in OBSERVATION_TARGETS:
        return _apply_observation(db, p, doc, target, value, reason)
    raise ValidationError([f"Неизвестное поле предложения: {target}"])


def _apply_observation(db, p, doc, target, value, reason):
    """Одно наблюдение на документ ПЭТ-КТ: Deauville и ответ дописываются в него."""
    marker = f"doc:{doc['doc_id']}"
    with db.tx(p.user_id) as cur:
        cur.execute("SELECT * FROM observation WHERE patient_id = %s AND source = %s AND source_record_id = %s "
                    "AND deleted_at IS NULL", (doc["patient_id"], SOURCE, marker))
        existing = cur.fetchone()
    field = {"deauville": {"deauville": value},
             "response": {"response_value": value, "response_basis": "pet_ct" if doc["doc_type"] == "pet_ct" else "ct"}}[target]
    if existing:
        data = {k: existing[k] for k in ("obs_date", "obs_precision", "line_id", "response_value", "response_basis",
                                         "event_type", "event_date", "death_cause", "pet_ct_report", "deauville",
                                         "pacs_accession", "provenance")}
        data = {k: ("" if v is None else v) for k, v in data.items()}
        data.update(field, change_reason=reason)
        registry.update_observation(db, p, doc["patient_id"], existing["obs_id"], data, source=SOURCE)
        return "observation", str(existing["obs_id"])
    data = {"obs_date": doc["doc_date"], "provenance": doc["provenance"], **field,
            "pet_ct_report": doc["text_deid"] if doc["doc_type"] == "pet_ct" else ""}
    res = registry.add_observation(db, p, doc["patient_id"], data, source=SOURCE, source_record_id=marker,
                                   reason=reason)
    return "observation", str(res.record_id)


def decide(db: Database, p: Principal, proposal_id, action: str, value: str | None = None,
           note: str | None = None) -> Result:
    p.require("review.apply")
    if action not in ACTIONS:
        raise ValidationError(["Неизвестное действие"])
    with db.tx(p.user_id) as cur:
        cur.execute("SELECT * FROM ai_proposal WHERE proposal_id = %s", (proposal_id,))
        proposal = cur.fetchone()
    if proposal is None:
        raise NotFound()
    if proposal["status"] != "pending":
        raise ValidationError(["По этому предложению уже есть решение"])
    doc = get_document(db, p, proposal["doc_id"])["doc"]
    note = (note or "").strip()[:500] or None

    applied = (None, None)
    final = None
    if action == "reject":
        status = "rejected"
    else:
        proposed = _value(proposal["proposed"])
        final = proposed if action == "accept" else (value or "").strip()
        if proposal["target"] != HISTOLOGY_TEXT and final not in allowed_values(proposal["target"]):
            raise ValidationError(["Выберите значение из списка"])
        status = "accepted" if final == proposed else "edited"
        applied = _apply(db, p, doc, proposal, final)   # ValidationError -> предложение остаётся ожидающим

    def write():
        with db.tx(p.user_id) as cur:
            cur.execute("""UPDATE ai_proposal SET status = %s, final = %s, review_note = %s, reviewed_by = %s,
                                  reviewed_at = now(), applied_table = %s, applied_id = %s
                           WHERE proposal_id = %s AND status = 'pending'""",
                        (status, json.dumps({"value": final}) if final is not None else None, note, p.user_id,
                         applied[0], applied[1], proposal_id))
            if cur.rowcount == 0:
                raise ValidationError(["По этому предложению уже есть решение"])
            cur.execute("""UPDATE source_document SET status = 'reviewed', review_finished_at = now()
                           WHERE doc_id = %s AND status = 'proposed'
                             AND NOT EXISTS (SELECT 1 FROM ai_proposal
                                              WHERE doc_id = %s AND status = 'pending')""",
                        (doc["doc_id"], doc["doc_id"]))
    _db_call(write)
    return Result(status, proposal_id)


def metrics(db: Database, p: Principal) -> dict:
    """Метрики пилота (PRD §9): точность по полю и нагрузка на куратора."""
    if not (p.can("review.apply") or p.can("audit.read")):
        p.require("review.apply")
    with db.tx(p.user_id) as cur:
        cur.execute("""SELECT a.target, r.extractor,
                              count(*) AS total,
                              count(*) FILTER (WHERE a.status = 'accepted') AS accepted,
                              count(*) FILTER (WHERE a.status = 'edited') AS edited,
                              count(*) FILTER (WHERE a.status = 'rejected') AS rejected,
                              count(*) FILTER (WHERE a.status = 'pending') AS pending,
                              count(*) FILTER (WHERE a.grounded) AS grounded,
                              count(*) FILTER (WHERE a.conflict) AS conflicts
                       FROM ai_proposal a JOIN ai_run r USING (run_id)
                       GROUP BY a.target, r.extractor ORDER BY a.target, r.extractor""")
        by_field = cur.fetchall()
        cur.execute("""SELECT count(*) AS docs,
                              percentile_cont(0.5) WITHIN GROUP (ORDER BY
                                  extract(epoch FROM review_finished_at - review_started_at)
                                  / greatest((SELECT count(*) FROM ai_proposal a WHERE a.doc_id = d.doc_id), 1))
                                  AS median_sec_per_proposal
                       FROM source_document d
                       WHERE review_started_at IS NOT NULL AND review_finished_at IS NOT NULL""")
        load = cur.fetchone()
        cur.execute("""SELECT status, count(*) AS n, sum(input_tokens) AS input_tokens,
                              sum(output_tokens) AS output_tokens, sum(cache_read_tokens) AS cache_read_tokens
                       FROM ai_run GROUP BY status ORDER BY status""")
        runs = cur.fetchall()
    for r in by_field:
        decided = r["accepted"] + r["edited"] + r["rejected"]
        r["accuracy"] = round(r["accepted"] / decided, 3) if decided else None
    return {"by_field": by_field, "load": load, "runs": runs}
