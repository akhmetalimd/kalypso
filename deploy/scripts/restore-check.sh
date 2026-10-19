#!/bin/sh
# Проверка резервной копии: восстановить во временную БД и сверить с рабочей.
#   deploy/scripts/restore-check.sh /var/backups/kalypso/kalypso-YYYYMMDD-HHMMSS.dump
# Рабочую БД не трогает; временная БД kalypso_restore_check удаляется в конце.
set -eu
dump="${1:?укажите файл копии (.dump)}"
cd "$(dirname "$0")/.."
psql_q() { docker compose exec -T db psql -U postgres -d "$1" -tA -c "$2"; }

docker compose exec -T db dropdb -U postgres --if-exists kalypso_restore_check
docker compose exec -T db createdb -U postgres -O kalypso_owner kalypso_restore_check
docker compose exec -T db pg_restore -U postgres -d kalypso_restore_check --exit-on-error < "$dump"

fail=0
for q in "SELECT string_agg(version, ',' ORDER BY version) FROM schema_migrations" \
         "SELECT count(*) FROM patient" \
         "SELECT count(*) FROM audit_log" \
         "SELECT count(*) FROM app_user"; do
    got=$(psql_q kalypso_restore_check "$q")
    live=$(psql_q kalypso "$q")
    echo "$q"
    echo "    копия: $got | рабочая БД сейчас: $live"
    [ -n "$got" ] || fail=1
done
docker compose exec -T db dropdb -U postgres kalypso_restore_check

if [ "$fail" -ne 0 ]; then echo "ОШИБКА: копия неполная"; exit 1; fi
echo "Копия восстанавливается. Рабочая БД могла измениться после копии — расхождение в счётчиках ожидаемо."
