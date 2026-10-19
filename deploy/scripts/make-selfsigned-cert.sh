#!/bin/sh
# Самоподписанный сертификат для внутренней сети (если клиника не выдала свой).
#   deploy/scripts/make-selfsigned-cert.sh kalypso.clinic.local
# Браузеры предупредят о недоверенном сертификате, пока его (или корневой сертификат
# клиники) не добавят в доверенные на рабочих местах. Свой сертификат клиники — просто
# положите его как deploy/certs/kalypso.crt и kalypso.key.
set -eu
name="${1:?укажите имя сервера, например kalypso.clinic.local}"
cd "$(dirname "$0")/.."
umask 077
mkdir -p certs
openssl req -x509 -newkey rsa:3072 -nodes -days 825 \
    -subj "/CN=$name" -addext "subjectAltName=DNS:$name" \
    -keyout certs/kalypso.key -out certs/kalypso.crt
chmod 644 certs/kalypso.crt
echo "Сертификат: deploy/certs/kalypso.crt (ключ: deploy/certs/kalypso.key, только для root)"
