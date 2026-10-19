# Образ сервера KALYPSO. Сборка из корня репозитория:
#   docker compose -f deploy/docker-compose.yml build
# Код запускается из каталога /app: schema.sql, db/migrations и config/ читаются оттуда.
FROM python:3.12.7-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY registry_core.py schema.sql ./
COPY config ./config
COPY db ./db
COPY kalypso ./kalypso
# Учётки dev-окружения (тестовые пароли) в серверный образ не попадают.
RUN rm -f kalypso/dev_seed.json kalypso/dev.py \
    && useradd --system --uid 10001 --home-dir /app kalypso

USER kalypso
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"]
CMD ["python", "-m", "kalypso.cli", "serve"]
