#!/bin/sh
# Ночная резервная копия БД (запускает таймер deploy/systemd/kalypso-backup.timer).
#   BACKUP_DIR  — каталог копий, лучше на отдельном диске (по умолчанию /var/backups/kalypso)
#   KEEP_DAYS   — сколько суток хранить (по умолчанию 14)
# Копия содержит ИИН в открытом виде: каталог — только для root (umask 077).
# С реальными данными копии нужно шифровать (см. docs/deploy.md) — сейчас в пилоте только синтетика.
set -eu
cd "$(dirname "$0")/.."                       # каталог deploy/ с docker-compose.yml
BACKUP_DIR="${BACKUP_DIR:-/var/backups/kalypso}"
KEEP_DAYS="${KEEP_DAYS:-14}"

umask 077
mkdir -p "$BACKUP_DIR"
file="$BACKUP_DIR/kalypso-$(date +%Y%m%d-%H%M%S).dump"
docker compose exec -T db pg_dump -U postgres -d kalypso --format=custom > "$file.part"
mv "$file.part" "$file"
find "$BACKUP_DIR" -name 'kalypso-*.dump' -type f -mtime +"$KEEP_DAYS" -delete
echo "Резервная копия: $file ($(du -h "$file" | cut -f1))"
