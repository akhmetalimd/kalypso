"""Локальный запуск на синтетике: встроенный PostgreSQL + миграции + seed + веб.

    python -m kalypso.dev            # http://127.0.0.1:8765
    PORT=9000 python -m kalypso.dev

Данные и секреты dev-окружения лежат в .devdb/ (в git не попадает).
Учётные записи — kalypso/dev_seed.json. Удалить .devdb/ = начать с чистой БД.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
from pathlib import Path

import psycopg

from kalypso import auth, migrate, synthetic
from kalypso.ai import pipeline
from kalypso.config import Settings
from kalypso.db import Database
from kalypso.deid import IinHasher

REPO = Path(__file__).resolve().parent.parent
DEVDIR = REPO / ".devdb"
SEED = Path(__file__).resolve().parent / "dev_seed.json"


def _dev_secrets() -> dict:
    path = DEVDIR / "dev_secrets.json"
    if not path.exists():  # ключ HMAC создаётся один раз: иначе дедупликация сломается
        path.write_text(json.dumps({"hmac_key": secrets.token_hex(32),
                                    "session_secret": secrets.token_hex(32)}), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


def seed_if_empty(db: Database, hasher: IinHasher) -> dict | None:
    with db.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM app_user")
        if cur.fetchone()["n"]:
            return None
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    with db.tx() as cur:
        for b in seed["branches"]:
            cur.execute("INSERT INTO branch (branch_code, name_ru) VALUES (%s, %s)",
                        (b["branch_code"], b["name_ru"]))
        for u in seed["users"]:
            cur.execute("""INSERT INTO app_user (login, display_name, password_hash, role_code,
                                                 branch_code, must_change_password)
                           VALUES (%s, %s, %s, %s, %s, false)""",
                        (u["login"], u["display_name"], auth.hash_password(u["password"]),
                         u["role_code"], u["branch_code"]))
        cur.execute("SELECT * FROM app_user WHERE role_code = 'physician' ORDER BY login")
        physicians = [auth._principal(r) for r in cur.fetchall()]
    summary = synthetic.generate(db, hasher, physicians, n=seed["synthetic_patients"])
    summary.pop("iins")  # синтетические ИИН не печатаем и не сохраняем
    return summary


def top_up_patients(db: Database, hasher: IinHasher) -> int:
    """Довести синтетическую когорту до synthetic_patients (для существующей .devdb)."""
    target = json.loads(SEED.read_text(encoding="utf-8"))["synthetic_patients"]
    with db.tx() as cur:
        cur.execute("RESET ROLE")
        cur.execute("SELECT count(*) AS n FROM patient")
        have = cur.fetchone()["n"]
        cur.execute("SELECT * FROM app_user WHERE role_code = 'physician' ORDER BY login")
        physicians = [auth._principal(r) for r in cur.fetchall()]
    if have >= target or not physicians:
        return 0
    return synthetic.generate(db, hasher, physicians, n=target - have, seed=1000 + have, duplicates=0)["patients"]


def ingest_synthetic_once(db: Database, hasher: IinHasher) -> dict | None:
    """Один прогон коннектора по синтетическому МИС-подобному источнику (если ещё не было)."""
    from kalypso.ingest.connector import run
    from kalypso.ingest.mapping import load_mapping
    from kalypso.ingest.synthetic_his import SyntheticHisAdapter
    with db.tx() as cur:
        cur.execute("RESET ROLE")
        cur.execute("SELECT count(*) AS n FROM ingest_run")
        if cur.fetchone()["n"]:
            return None
    return run(db, SyntheticHisAdapter(n=10, seed=77), load_mapping(), hasher)["counts"]


def seed_documents_if_empty(db: Database, ai_enabled: bool, n: int = 16) -> int:
    """Синтетические заключения для части пациентов — чтобы очередь сверки была не пустой."""
    import random

    from kalypso import documents
    from kalypso.ai.synthetic_docs import generate
    with db.tx() as cur:
        cur.execute("RESET ROLE")   # dev-seed: владелец, мимо RLS
        cur.execute("SELECT count(*) AS n FROM source_document")
        if cur.fetchone()["n"]:
            return 0
        cur.execute("SELECT patient_id, branch, dx_date, provenance FROM patient WHERE deleted_at IS NULL "
                    "ORDER BY entry_date")
        patients = cur.fetchall()
        cur.execute("SELECT * FROM app_user WHERE role_code = 'physician'")
        doctors = {r["branch_code"]: auth._principal(r) for r in cur.fetchall()}
    rng = random.Random(7)
    made = 0
    for doc, pt in zip(generate(n, seed=7), rng.sample(patients, min(n, len(patients)))):
        clean = documents.preview(doc.raw_text)
        documents.add_document(db, doctors[pt["branch"]], pt["patient_id"], {
            "doc_type": doc.doc_type, "doc_date": max(doc.doc_date, pt["dx_date"]).isoformat(),
            "provenance": pt["provenance"], "text": clean.text, "deid_report": json.dumps(clean.report),
            "confirm": "1"}, ai_enabled=ai_enabled)
        made += 1
    return made


def main() -> None:
    import pgserver
    import uvicorn
    from kalypso.web.app import create_app

    DEVDIR.mkdir(exist_ok=True)
    sec = _dev_secrets()
    try:
        srv = pgserver.get_server(DEVDIR / "pg", cleanup_mode="stop")
    except subprocess.TimeoutExpired:
        # После аварийной остановки PostgreSQL восстанавливается дольше таймаута pgserver (10 с).
        print("PostgreSQL восстанавливается после аварийной остановки — повторная попытка…")
        srv = pgserver.get_server(DEVDIR / "pg", cleanup_mode="stop")
    with psycopg.connect(srv.get_uri(), autocommit=True) as con:
        if not con.execute("SELECT 1 FROM pg_database WHERE datname = 'kalypso_dev'").fetchone():
            con.execute("CREATE DATABASE kalypso_dev")
    dsn = srv.get_uri("kalypso_dev")
    done = migrate.apply_pending(dsn)
    if done:
        print("Применены миграции:", ", ".join(done))

    # В dev по умолчанию — извлечение на правилах (без сети). Claude — только если явно
    # задать KALYPSO_AI_EXTRACTOR=claude и ANTHROPIC_API_KEY: это платные вызовы.
    extractor_kind = os.environ.get("KALYPSO_AI_EXTRACTOR", "rules")
    settings = Settings(dsn=dsn, hmac_key=sec["hmac_key"].encode(), session_secret=sec["session_secret"].encode(),
                        secure_cookies=False, ai_extractor=extractor_kind,
                        ai_model=os.environ.get("KALYPSO_AI_MODEL") or None)
    db = Database(dsn)
    summary = seed_if_empty(db, IinHasher(settings.hmac_key, settings.hash_key_version))
    if summary:
        print("Синтетика:", summary, "· учётные записи: kalypso/dev_seed.json")
    added = top_up_patients(db, IinHasher(settings.hmac_key, settings.hash_key_version))
    if added:
        print(f"Синтетика: добавлено пациентов {added}")
    made = seed_documents_if_empty(db, settings.ai_enabled)
    if made:
        print(f"Синтетические документы: {made}")
    ingested = ingest_synthetic_once(db, IinHasher(settings.hmac_key, settings.hash_key_version))
    if ingested:
        print("Синтетический приём из МИС:", ingested)

    extractor = pipeline.make_extractor(settings.ai_extractor, settings.ai_model, settings.ai_effort)
    if extractor is not None:
        if extractor.name == "claude":
            print("ВНИМАНИЕ: AI-извлечение через Claude — платные вызовы API")
        threading.Thread(target=pipeline.run_forever, args=(db, extractor), daemon=True,
                         name="ai-worker").start()
        print(f"AI-обработчик запущен: {extractor.name}")
    uvicorn.run(create_app(settings, db), host="127.0.0.1", port=int(os.environ.get("PORT", "8765")))


if __name__ == "__main__":
    main()
