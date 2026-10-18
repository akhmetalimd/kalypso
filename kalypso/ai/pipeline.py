"""Конвейер AI-обработки (стадии 3–4 PRD §6.2): документ из очереди -> предложения.

Обработчик работает от служебного пользователя ai-worker. Любой сбой извлечения
(нет сети, отказ, ответ не по схеме, ошибка кода) не роняет конвейер: прогон
журналируется, документ уходит в ручную очередь (изящная деградация).
Предложения ничего не пишут в регистр — это делает только сверка (kalypso.review).
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from kalypso import auth
from kalypso.ai.base import Extraction, ExtractionFailed, Extractor
from kalypso.ai.fields import (DIAGNOSIS_CODES, HISTOLOGY_TEXT, MARKER_TARGETS, allowed_values,
                               targets_for)
from kalypso.db import Database
from kalypso.rbac import Principal
from kalypso.registry import _marker_map

log = logging.getLogger("kalypso.ai")
WORKER_LOGIN = "ai-worker"
_RANK = {"high": 0, "medium": 1, "low": 2}


def worker_principal(db: Database) -> Principal:
    with db.tx() as cur:
        cur.execute("SELECT * FROM app_user WHERE login = %s AND role_code = 'ai_worker'", (WORKER_LOGIN,))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("служебный пользователь ai-worker не найден (миграция 0005)")
    return auth._principal(row)


def claim_next(db: Database, worker: Principal) -> dict | None:
    with db.tx(worker.user_id) as cur:
        cur.execute("""UPDATE source_document SET status = 'processing', attempts = attempts + 1
                       WHERE doc_id = (SELECT doc_id FROM source_document
                                       WHERE status = 'queued' AND deid_passed AND deleted_at IS NULL
                                       ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
                       RETURNING *""")
        return cur.fetchone()


def _current_values(cur, pid) -> dict:
    """Что уже есть в регистре — для поиска противоречий."""
    cur.execute("SELECT icd_o_3_2, histology_report FROM patient WHERE patient_id = %s", (pid,))
    pt = cur.fetchone()
    cur.execute("SELECT * FROM module_data WHERE patient_id = %s AND deleted_at IS NULL", (pid,))
    current: dict = dict(_marker_map(cur.fetchall()))
    current["diagnosis"] = next((k for k, (_, icd, _) in DIAGNOSIS_CODES.items() if icd == pt["icd_o_3_2"]), None)
    current[HISTOLOGY_TEXT] = pt["histology_report"]
    return current


def _ground(text: str, evidence: str | None) -> tuple[int | None, int | None]:
    """Позиция цитаты в тексте: точное совпадение, затем без учёта регистра."""
    if not evidence:
        return None, None
    i = text.find(evidence)
    if i < 0 and len(text.casefold()) == len(text):
        i = text.casefold().find(evidence.casefold())
    return (i, i + len(evidence)) if i >= 0 else (None, None)


def build_proposals(doc: dict, extraction: Extraction, current: dict) -> tuple[list[dict], list[str]]:
    """Находки -> строки ai_proposal: проверка значений, привязка цитат, противоречия."""
    allowed_targets = set(targets_for(doc["doc_type"]))
    notes, best = [], {}
    for f in extraction.findings:
        if f.target not in allowed_targets:
            notes.append(f"поле {f.target} не ожидается в документе этого типа")
            continue
        value = f.value.strip()
        if value not in allowed_values(f.target):
            notes.append(f"{f.target}: недопустимое значение")
            continue
        start, end = _ground(doc["text_deid"], f.evidence)
        confidence = f.confidence if f.confidence in _RANK else "low"
        if start is None:
            confidence = "low"            # цитата не найдена — возможно, значение выдумано
        cand = {"target": f.target, "value": value, "evidence": f.evidence, "start": start, "end": end,
                "grounded": start is not None, "confidence": confidence}
        prev = best.get(f.target)
        if prev is None or (_RANK[cand["confidence"]], not cand["grounded"]) < (_RANK[prev["confidence"]], not prev["grounded"]):
            best[f.target] = cand
    if doc["doc_type"] == "histology":
        best[HISTOLOGY_TEXT] = {"target": HISTOLOGY_TEXT, "value": doc["text_deid"], "evidence": None,
                                "start": None, "end": None, "grounded": True, "confidence": "high"}
    rows = []
    for t, c in best.items():
        cur_value = current.get(t)
        if t in MARKER_TARGETS and cur_value not in allowed_values(t):
            cur_value_cmp = None          # код пропуска — не противоречие, а пробел
        else:
            cur_value_cmp = cur_value
        rows.append({**c, "current": cur_value,
                     "conflict": cur_value_cmp is not None and cur_value_cmp != c["value"]})
    return rows, notes


def _record_run(cur, doc, extractor: Extractor, started, status, extraction: Extraction | None = None,
                failure: ExtractionFailed | None = None, error: str | None = None):
    usage = (extraction.usage if extraction else (failure.usage if failure else {})) or {}
    cur.execute("""INSERT INTO ai_run (doc_id, extractor, model, prompt_version, status, error, request_id,
                                       input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                                       started_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING run_id""",
                (doc["doc_id"], extractor.name, (extraction.model if extraction else None) or extractor.model,
                 extractor.prompt_version, status, error or (str(failure) if failure else None),
                 (extraction.request_id if extraction else None) or (failure.request_id if failure else None),
                 usage.get("input_tokens"), usage.get("output_tokens"),
                 usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens"), started))
    return cur.fetchone()["run_id"]


def _to_manual(cur, doc_id, note: str) -> None:
    cur.execute("UPDATE source_document SET status = 'manual', status_note = %s WHERE doc_id = %s",
                (note, doc_id))


def process_document(db: Database, extractor: Extractor, worker: Principal, doc: dict) -> str:
    started = datetime.now(timezone.utc)
    try:
        extraction = extractor.extract(doc["doc_type"], doc["text_deid"])
    except ExtractionFailed as e:
        with db.tx(worker.user_id) as cur:
            _record_run(cur, doc, extractor, started, e.status, failure=e)
            _to_manual(cur, doc["doc_id"], f"AI не обработал документ ({e.status}) — внесите вручную "
                                           f"или отправьте повторно")
        log.warning("ai run failed doc=%s status=%s", doc["doc_id"], e.status)
        return "manual"
    except Exception as e:  # noqa: BLE001 — конвейер не падает на ошибке извлечения
        with db.tx(worker.user_id) as cur:
            _record_run(cur, doc, extractor, started, "error", error=type(e).__name__)
            _to_manual(cur, doc["doc_id"], "Ошибка обработки — внесите вручную или отправьте повторно")
        log.exception("ai run crashed doc=%s", doc["doc_id"])
        return "manual"

    with db.tx(worker.user_id) as cur:
        rows, notes = build_proposals(doc, extraction, _current_values(cur, doc["patient_id"]))
        run_id = _record_run(cur, doc, extractor, started, "ok", extraction=extraction,
                             error="; ".join(notes) or None)
        for r in rows:
            cur.execute("""INSERT INTO ai_proposal (run_id, doc_id, patient_id, target, proposed, current, conflict,
                                                    evidence, evidence_start, evidence_end, grounded, confidence)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (run_id, doc["doc_id"], doc["patient_id"], r["target"], json.dumps({"value": r["value"]}),
                         json.dumps({"value": r["current"]}) if r["current"] is not None else None,
                         r["conflict"], r["evidence"], r["start"], r["end"], r["grounded"], r["confidence"]))
        if rows:
            cur.execute("UPDATE source_document SET status = 'proposed', status_note = NULL WHERE doc_id = %s",
                        (doc["doc_id"],))
            return "proposed"
        _to_manual(cur, doc["doc_id"], "AI не нашёл значений полей — проверьте и внесите вручную")
        return "manual"


def process_pending(db: Database, extractor: Extractor, limit: int = 50) -> int:
    worker = worker_principal(db)
    n = 0
    while n < limit:
        doc = claim_next(db, worker)
        if doc is None:
            break
        process_document(db, extractor, worker, doc)
        n += 1
    return n


def run_forever(db: Database, extractor: Extractor, interval: float = 5.0,
                stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            process_pending(db, extractor)
        except Exception:  # noqa: BLE001 — обработчик переживает сбои БД и т.п.
            log.exception("ai worker iteration failed")
        stop.wait(interval)


def make_extractor(kind: str, model: str | None = None, effort: str = "medium") -> Extractor | None:
    """off -> None (всё в ручную очередь); rules — без сети; claude — Anthropic API."""
    if kind == "off":
        return None
    if kind == "rules":
        from kalypso.ai.rules import RuleExtractor
        return RuleExtractor()
    if kind == "claude":
        from kalypso.ai.claude import DEFAULT_MODEL, ClaudeExtractor
        return ClaudeExtractor(model=model or DEFAULT_MODEL, effort=effort)
    raise ValueError(f"неизвестный извлекатель: {kind}")
