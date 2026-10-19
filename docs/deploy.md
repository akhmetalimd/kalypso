# KALYPSO — развёртывание пилота (для ИТ-службы)

Пилот разворачивается на сервере клиники для обучения врачей и проверки платформы.

> **Только синтетические данные.** До соглашения об обработке данных и одобрения
> этического комитета реальные данные пациентов в платформу не вносятся. Интерфейс
> показывает это предупреждение на каждой странице. Загрузка обучающих данных —
> командой `seed-synthetic` (раздел 5).

Состав: три контейнера Docker — PostgreSQL 16 (`db`), приложение (`app`), nginx с HTTPS
(`nginx`) — и разовый контейнер миграций (`migrate`). База доступна только внутри
внутренней сети Docker; снаружи открыты порты 80 (переадресация на HTTPS) и 443.

## 1. Сервер

- Ubuntu 24.04 LTS, 2 vCPU, 4 ГБ ОЗУ, 40 ГБ диска + отдельный диск/раздел для копий.
- Docker Engine и плагин Compose (официальный репозиторий Docker).
- Время синхронизировано (`timedatectl status` → `System clock synchronized: yes`).
- Межсетевой экран: входящие 443 (и 80) — только из сети клиники; SSH — только администраторам.
- Имя сервера во внутреннем DNS, например `kalypso.clinic.local`.

## 2. Установка

```bash
sudo git clone <адрес репозитория> /opt/kalypso      # или распаковать архив релиза
cd /opt/kalypso/deploy
sudo cp .env.example .env && sudo chmod 600 .env
sudo nano .env        # каждое значение — openssl rand -hex 32
```

Сертификат HTTPS — файлы `deploy/certs/kalypso.crt` и `deploy/certs/kalypso.key`:

- **сертификат клиники** (рекомендуется): положить файлы с этими именами;
- **самоподписанный** для внутренней сети: `sudo scripts/make-selfsigned-cert.sh kalypso.clinic.local`
  — браузеры будут предупреждать, пока сертификат не добавлен в доверенные на рабочих местах.

```bash
sudo chmod 600 certs/kalypso.key
sudo docker compose up -d --build
```

## 3. Первый запуск — проверка по шагам

| Шаг | Команда | Ожидаемо |
|---|---|---|
| Контейнеры | `sudo docker compose ps` | `db`, `app`, `nginx` — `running (healthy)`; `migrate` — `exited (0)` |
| Миграции | `sudo docker compose logs migrate` | `Применено: 0001_baseline, … 0017_tlgll` |
| Готовность | `sudo docker compose exec app python -m kalypso.cli check` | `Готово к запуску…` |
| HTTPS | открыть `https://kalypso.clinic.local` | страница входа; `http://` переадресуется на `https://` |
| БД закрыта | `nc -zv <сервер> 5432` с другой машины | соединение отклонено |

Если `app` не стартует — `sudo docker compose logs app`: команда `check` перечисляет
проблемы (роль входа, миграции, секрет).

## 4. Первый администратор и пользователи

```bash
sudo docker compose exec app python -m kalypso.cli create-admin admin "Администратор"
```

Пароль вводится скрыто (не короче 10 символов). Дальше — в интерфейсе, раздел
«Пользователи»: филиалы, затем врачи (с филиалом), куратор, аналитик. Каждому
пользователю — свой логин; общие учётные записи не заводить.

## 5. Обучающие (синтетические) данные

После того как созданы хотя бы один врач и куратор:

```bash
sudo docker compose exec app python -m kalypso.cli seed-synthetic --cll 60 --dlbcl 40 --fl 40 --other 30
```

Пациенты распределяются по врачам (их филиалам). Команда работает только с пустым
регистром. ИИН синтетических пациентов — сгенерированные числа, к реальным людям не относятся.

## 6. Резервные копии

```bash
sudo mkdir -p /var/backups/kalypso && sudo chmod 700 /var/backups/kalypso
sudo cp systemd/kalypso-backup.* /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now kalypso-backup.timer
sudo systemctl start kalypso-backup.service && sudo journalctl -u kalypso-backup -n 5   # первая копия
```

- Каждую ночь в 02:00 — `pg_dump` в `/var/backups/kalypso`, хранение 14 суток
  (`BACKUP_DIR`, `KEEP_DAYS` — в `kalypso-backup.service`). Каталог лучше смонтировать с отдельного диска.
- **Раз в месяц** — проверка восстановления (рабочая БД не затрагивается):
  `sudo scripts/restore-check.sh /var/backups/kalypso/<файл>.dump`.
- Восстановление рабочей БД из копии (с остановкой приложения):

  ```bash
  sudo docker compose stop app nginx
  sudo docker compose exec -T db dropdb -U postgres kalypso
  sudo docker compose exec -T db createdb -U postgres -O kalypso_owner kalypso
  sudo docker compose exec -T db pg_restore -U postgres -d kalypso --exit-on-error < <файл>.dump
  sudo docker compose start app nginx
  ```

- В копии ИИН хранится открыто. **С реальными данными копии обязательно шифровать**
  (например, `gpg --symmetric` или зашифрованный том) и хранить ключ отдельно от копий.

## 7. Обновление

```bash
cd /opt/kalypso && sudo git pull                     # или новый архив релиза
cd deploy && sudo docker compose up -d --build       # migrate применит новые миграции
sudo docker compose exec app python -m kalypso.cli check
```

Перед обновлением — внеочередная копия: `sudo systemctl start kalypso-backup.service`.

## 8. Смена секретов

- `KALYPSO_SESSION_SECRET`: изменить в `.env`, затем `sudo docker compose up -d app` —
  все пользователи выйдут из системы.
- Пароли БД (`KALYPSO_WEB_PASSWORD`, `KALYPSO_OWNER_PASSWORD`): сначала в БД, потом в `.env`:

  ```bash
  sudo docker compose exec db psql -U postgres -c "ALTER ROLE kalypso_web PASSWORD '<новый>'"
  sudo docker compose up -d app
  ```

  (`POSTGRES_PASSWORD` и роли из `.env` применяются только при создании тома — менять через `ALTER ROLE`.)

## 9. Безопасность — чек-лист

- [ ] `.env` и `certs/kalypso.key` — права 600, владелец root; в git не попадают (`.gitignore`).
- [ ] Порты 80/443 открыты только для сети клиники; 5432 наружу не опубликован.
- [ ] `check` проходит: логин приложения `kalypso_web` — не суперпользователь, не владелец
      таблиц (иначе обходится разграничение доступа по филиалам, RLS).
- [ ] Журналы: приложение маскирует любые 12-значные числа (`***`); журнал PostgreSQL —
      без DETAIL и текста запросов (`log_error_verbosity=terse`, см. `docker-compose.yml`).
      Доступ к журналам Docker — только администраторам.
- [ ] Резервные копии: таймер включён, ежемесячная проверка восстановления проводится.
- [ ] Обновления ОС и Docker — по регламенту клиники.
- [ ] Учётные записи уволившихся сотрудников отключаются в разделе «Пользователи»
      (удаления нет — история в аудите сохраняется).
- [ ] **Реальные данные пациентов не вносятся** до соглашения и одобрения этического комитета.

## 10. Что пока не входит

Мониторинг и оповещения (используйте `/healthz` изнутри и `docker compose ps`), балансировка
нагрузки, шифрование копий (обязательно до реальных данных), единый вход (SSO).
