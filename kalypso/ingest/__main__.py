"""Коннектор МИС — запуск прогона (раз в сутки/неделю, Планировщик заданий или cron).

    python -m kalypso.ingest run --adapter file --file export.json
    python -m kalypso.ingest run --adapter synthetic          # синтетика (пилот)
    python -m kalypso.ingest run --adapter his.kz             # П5: после спецификации поставщика

Окружение — как у приложения (KALYPSO_DSN, KALYPSO_HMAC_KEY — тот же ключ, что у
регистра, иначе дедупликация не совпадёт). Исполняется внутри сети клиники.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from kalypso.config import load_settings
from kalypso.db import Database
from kalypso.deid import IinHasher
from kalypso.ingest.connector import run
from kalypso.ingest.mapping import load_mapping


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kalypso.ingest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="один прогон приёма из источника")
    r.add_argument("--adapter", choices=["file", "synthetic", "his.kz"], required=True)
    r.add_argument("--file", help="выгрузка JSON для --adapter file")
    r.add_argument("--mapping", default="his_kz", help="имя конфига в config/field_mapping")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    settings = load_settings()
    if args.adapter == "file":
        from source_adapter import ManualAdapter
        adapter = ManualAdapter.from_file(args.file)
    elif args.adapter == "synthetic":
        from kalypso.ingest.synthetic_his import SyntheticHisAdapter
        adapter = SyntheticHisAdapter()
    else:
        from source_adapter import HisKzAdapter
        adapter = HisKzAdapter(base_url="")   # П5: настройки подключения по спецификации
    db = Database(settings.dsn, max_size=2)
    result = run(db, adapter, load_mapping(args.mapping), IinHasher(settings.hmac_key, settings.hash_key_version))
    print(json.dumps({**result, "run_id": str(result["run_id"])}, ensure_ascii=False))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
