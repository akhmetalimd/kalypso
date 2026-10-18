"""Фоновый AI-обработчик (исполняется на сервере внутри сети клиники).

    KALYPSO_DSN=... KALYPSO_HMAC_KEY=... KALYPSO_SESSION_SECRET=... \
    KALYPSO_AI_EXTRACTOR=rules python -m kalypso.ai.worker
"""
from __future__ import annotations

import logging
import sys

from kalypso.ai.pipeline import make_extractor, run_forever
from kalypso.config import load_settings
from kalypso.db import Database


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = load_settings()
    extractor = make_extractor(settings.ai_extractor, settings.ai_model, settings.ai_effort)
    if extractor is None:
        print("KALYPSO_AI_EXTRACTOR=off — обработчик не запускается, документы идут в ручную очередь")
        return 0
    run_forever(Database(settings.dsn, max_size=2), extractor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
