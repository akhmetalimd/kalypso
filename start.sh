#!/usr/bin/env sh
# KALYPSO: запуск на синтетике (macOS / Linux). Первый запуск ставит окружение — 3–10 минут.
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"

if [ ! -f .venv/.kalypso-installed ]; then
  command -v "$PY" >/dev/null 2>&1 || { echo "[KALYPSO] Не найден $PY. Установите Python 3.12+ (python.org) или укажите PYTHON=..."; exit 1; }
  "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || { echo "[KALYPSO] Нужен Python 3.12 или новее (PYTHON=python3.12 ./start.sh)"; exit 1; }
  echo "[KALYPSO] Создаю окружение Python (.venv) и ставлю зависимости..."
  [ -x .venv/bin/python ] || "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements-dev.txt
  touch .venv/.kalypso-installed
fi

echo
echo "[KALYPSO] Запуск. Когда появится \"Uvicorn running\", откройте http://127.0.0.1:${PORT:-8765}"
echo "[KALYPSO] Первый запуск создаёт синтетическую когорту — несколько минут. Остановить — Ctrl+C."
echo
exec .venv/bin/python -X utf8 -m kalypso.dev
