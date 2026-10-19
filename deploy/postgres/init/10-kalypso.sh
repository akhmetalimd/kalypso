#!/bin/sh
# Первичная настройка БД (образ postgres выполняет это один раз — при пустом томе pgdata).
#   kalypso_owner — владелец схемы, применяет миграции (не суперпользователь);
#   kalypso_app   — групповая роль приложения (NOLOGIN), на неё — права и политики RLS;
#   kalypso_web   — логин приложения, член kalypso_app; таблицами не владеет -> подчиняется RLS.
set -eu
: "${KALYPSO_OWNER_PASSWORD:?не задан}"
: "${KALYPSO_WEB_PASSWORD:?не задан}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
     -v owner_pw="$KALYPSO_OWNER_PASSWORD" -v web_pw="$KALYPSO_WEB_PASSWORD" <<'SQL'
CREATE ROLE kalypso_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE kalypso_app NOLOGIN;
CREATE ROLE kalypso_web LOGIN PASSWORD :'web_pw' IN ROLE kalypso_app;
CREATE DATABASE kalypso OWNER kalypso_owner ENCODING 'UTF8' TEMPLATE template0;
REVOKE ALL ON DATABASE kalypso FROM PUBLIC;
GRANT CONNECT ON DATABASE kalypso TO kalypso_owner, kalypso_web;
SQL
