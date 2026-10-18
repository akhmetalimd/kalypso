"""Коннектор МИС (стадии 1–3 PRD §6.2): извлечение -> деидентификация -> маппинг -> промежуточная зона.

Исполняется на сервере внутри сети клиники от служебного пользователя his-connector.
Сырые записи (с ИИН и ФИО) живут только в памяти этого процесса: в БД попадают
HMAC-хэш ИИН, HMAC внешнего ID записи и поля из белого списка маппинга. Ничего не
пишется в клинические таблицы — всё применяет куратор (kalypso.ingest.apply).
В журнал (логи) идут только счётчики, без идентификаторов.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections import Counter
from datetime import datetime, timezone

from kalypso import auth
from kalypso.db import Database
from kalypso.deid import IinHasher, InvalidIIN
from kalypso.ingest.mapping import get_path, included, map_section
from kalypso.rbac import Principal
from source_adapter import SourceAdapter

log = logging.getLogger("kalypso.ingest")
CONNECTOR_LOGIN = "his-connector"


def connector_principal(db: Database) -> Principal:
    with db.tx() as cur:
        cur.execute("SELECT * FROM app_user WHERE login = %s AND role_code = 'ingest_worker'", (CONNECTOR_LOGIN,))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("служебный пользователь his-connector не найден (миграция 0006)")
    return auth._principal(row)


def record_key(hasher: IinHasher, source_system: str, kind: str, external_id) -> str:
    """HMAC внешнего ID записи МИС: идемпотентность без возможности связать регистр с МИС."""
    msg = f"{source_system}|{kind}|{external_id}".encode("utf-8")
    return hmac.new(hasher.key, msg, hashlib.sha256).hexdigest()


def _stage(cur, run_id, cfg, kind, key, iin_hash, pid, mapped) -> str:
    """Положить элемент в зону приёма. Возвращает new | update | duplicate."""
    payload = dict(mapped.fields)
    if mapped.deid_report:
        payload["_deid_report"] = mapped.deid_report
    content = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    cur.execute("SELECT 1 FROM ingest_item WHERE source_system = %s AND record_key = %s LIMIT 1",
                (cfg["source_system"], key))
    change = "update" if cur.fetchone() else "new"
    cur.execute("""INSERT INTO ingest_item (run_id, source_system, kind, record_key, content_hash, iin_hash, patient_id,
                                            payload, issues, change)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (source_system, record_key, content_hash) DO NOTHING RETURNING item_id""",
                (run_id, cfg["source_system"], kind, key, content, iin_hash, pid,
                 json.dumps(payload, ensure_ascii=False, default=str), json.dumps(mapped.issues, ensure_ascii=False), change))
    row = cur.fetchone()
    if row is None:
        return "duplicate"
    # Более старые нерешённые версии той же записи устаревают — решать по ним уже нечего.
    cur.execute("""UPDATE ingest_item SET status = 'superseded'
                   WHERE source_system = %s AND record_key = %s AND status = 'pending' AND item_id <> %s""",
                (cfg["source_system"], key, row["item_id"]))
    return change


def process_source_record(db, adapter: SourceAdapter, cfg: dict, hasher: IinHasher, worker: Principal,
                          run_id, source_id: str, since) -> Counter:
    counts: Counter = Counter()
    rec = adapter.get_patient(source_id)                       # 1. извлечение (в памяти)
    diagnoses = included(cfg, rec)
    if not diagnoses:
        counts["skipped_inclusion"] += 1                      # вне критериев: не сохраняется ничего
        return counts
    try:
        iin_hash = hasher.hash(str(get_path(rec, cfg["identity"]["iin"]) or ""))   # 2. деидентификация
    except InvalidIIN:
        counts["invalid_iin"] += 1
        return counts
    labs = adapter.get_labs(source_id, since)
    docs = adapter.get_documents(source_id)
    rid = cfg["identity"]["record_id"]
    system = cfg["source_system"]

    with db.tx(worker.user_id) as cur:                         # 3. маппинг и раскладка — одна транзакция на пациента
        cur.execute("SELECT patient_id_by_hash(%s) AS pid", (iin_hash,))
        pid = cur.fetchone()["pid"]
        if pid is None:
            mapped = map_section(cfg, "patient", rec)
            dx = map_section(cfg, "diagnosis", diagnoses[0])
            mapped.fields = {"patient": mapped.fields, "diagnosis": dx.fields,
                             "hash_key_version": hasher.key_version}
            mapped.issues += dx.issues
            counts["patient_" + _stage(cur, run_id, cfg, "patient", record_key(hasher, system, "patient", iin_hash),
                                       iin_hash, None, mapped)] += 1
        else:
            for d in diagnoses:
                counts["diagnosis_" + _stage(cur, run_id, cfg, "diagnosis", record_key(hasher, system, "diagnosis", get_path(d, rid)),
                                             iin_hash, pid, map_section(cfg, "diagnosis", d))] += 1
        for lab in labs:
            counts["lab_" + _stage(cur, run_id, cfg, "lab", record_key(hasher, system, "lab", get_path(lab, rid)),
                                   iin_hash, pid, map_section(cfg, "lab", lab))] += 1
        for doc in docs:
            counts["document_" + _stage(cur, run_id, cfg, "document", record_key(hasher, system, "document", get_path(doc, rid)),
                                        iin_hash, pid, map_section(cfg, "document", doc))] += 1
    return counts


def run(db: Database, adapter: SourceAdapter, cfg: dict, hasher: IinHasher, since=None,
        now: datetime | None = None) -> dict:
    """Один прогон. since=None — с последней успешной метки (ingest_state), иначе с начала."""
    worker = connector_principal(db)
    until = now or datetime.now(timezone.utc)
    with db.tx(worker.user_id) as cur:
        if since is None:
            cur.execute("SELECT last_until FROM ingest_state WHERE source_system = %s", (cfg["source_system"],))
            row = cur.fetchone()
            since = row["last_until"] if row else None
        cur.execute("""INSERT INTO ingest_run (source_system, adapter, mapping_version, since, until)
                       VALUES (%s, %s, %s, %s, %s) RETURNING run_id""",
                    (cfg["source_system"], adapter.name, cfg["version"], since, until))
        run_id = cur.fetchone()["run_id"]

    counts: Counter = Counter()
    try:
        ids = adapter.list_changes(since)
    except Exception as e:  # noqa: BLE001 — недоступность источника: прогон «ошибка», метка не двигается
        _finish(db, worker, run_id, "failed", counts, f"источник недоступен: {type(e).__name__}")
        log.warning("ingest run failed: source unavailable (%s)", type(e).__name__)
        return {"run_id": run_id, "status": "failed", "counts": dict(counts)}
    for sid in ids:
        try:
            counts.update(process_source_record(db, adapter, cfg, hasher, worker, run_id, sid, since))
        except Exception as e:  # noqa: BLE001 — сбой одной записи не роняет прогон
            counts["error"] += 1
            log.warning("ingest record failed (%s)", type(e).__name__)   # без ID: это может быть ИИН
    status = "ok"
    _finish(db, worker, run_id, status, counts, None)
    with db.tx(worker.user_id) as cur:
        cur.execute("""INSERT INTO ingest_state (source_system, last_until) VALUES (%s, %s)
                       ON CONFLICT (source_system) DO UPDATE SET last_until = EXCLUDED.last_until, updated_at = now()""",
                    (cfg["source_system"], until))
    log.info("ingest run ok: %s", dict(counts))
    return {"run_id": run_id, "status": status, "counts": dict(counts)}


def _finish(db, worker, run_id, status, counts, error) -> None:
    with db.tx(worker.user_id) as cur:
        cur.execute("UPDATE ingest_run SET status = %s, counts = %s, error = %s, finished_at = now() WHERE run_id = %s",
                    (status, json.dumps(dict(counts)), error, run_id))
