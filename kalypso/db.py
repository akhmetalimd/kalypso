"""Доступ к БД: каждая транзакция идёт под ролью kalypso_app с автором правки.

    with db.tx(user_id, reason="...") as cur:
        cur.execute(...)

Выход из блока — commit (в т.ч. отложенные проверки БД), исключение — rollback.
Ошибки БД переводятся в сообщения для пользователя через db_error_message().
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class Database:
    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 10):
        self.pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size,
                                   kwargs={"row_factory": dict_row}, open=True)

    @contextmanager
    def tx(self, user_id=None, reason: str | None = None) -> Iterator[psycopg.Cursor]:
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL ROLE kalypso_app")
                if user_id is not None:
                    cur.execute("SELECT set_config('app.user_id', %s, true)", (str(user_id),))
                if reason:
                    cur.execute("SELECT set_config('app.change_reason', %s, true)", (reason,))
                yield cur

    def close(self) -> None:
        self.pool.close()


# Сообщения для пользователя по имени ограничения БД (последний рубеж проверки).
_CONSTRAINT_MESSAGES = {
    "patient_iin_hash_key": "Пациент с таким ИИН уже есть в регистре",
    "patient_last_contact_after_dx": "Дата последнего контакта не может быть раньше даты диагноза",
    "patient_birth_before_dx": "Дата рождения не может быть позже даты диагноза",
    "patient_stage_pair": "Стадия указывается вместе с системой стадирования",
    "patient_stage_rai": "Стадия по Rai: 0, I, II, III или IV",
    "patient_stage_binet": "Стадия по Binet: A, B или C",
    "patient_index_pair": "Индекс указывается вместе с баллом",
    "patient_index_cll_ipi": "CLL-IPI: балл 0–10 и группа риска",
    "patient_stage_lugano": "Стадия по Lugano: I, IE, II, IIE, III или IV",
    "patient_index_ipi": "IPI: балл 0–5 и группа риска",
    "patient_index_flipi": "FLIPI: балл 0–5 и группа риска",
    "patient_index_smipi": "sMIPI: балл 0–11 и группа риска",
    "patient_index_ips": "IPS: балл 0–7 и группа риска",
    "patient_index_ipss_wm": "IPSS-WM: балл 0–5 и группа риска",
    "patient_index_malt_ipi": "MALT-IPI: балл 0–3 и группа риска",
    "patient_index_aa_ipi": "aaIPI: балл 0–3 и группа риска",
    "patient_index_bl_ipi": "BL-IPI: балл 0–4 и группа риска",
    "patient_index_ielsg": "IELSG: балл 0–5 и группа риска",
    "observation_response_ipcg": "Недопустимое значение ответа для IPCG",
    "observation_response_iwwm": "Недопустимое значение ответа для IWWM-11",
    "patient_index_name": "Неизвестный прогностический индекс",
    "observation_lugano_basis": "Lugano 2014: по ПЭТ-КТ — метаболический ответ (CMR/PMR/NMR/PMD), иначе — CR/PR/SD/PD",
    "observation_lugano_deauville": "Метаболический ответ требует Deauville: CMR — 1–3, PMR/NMR/PMD — 4–5",
    "patient_histology_no_iin": "В тексте гистологии есть 12-значное число (похоже на ИИН) — удалите его",
    "observation_pet_ct_no_iin": "В тексте ПЭТ-КТ есть 12-значное число (похоже на ИИН) — удалите его",
    "observation_response_complete": "Ответ указывается вместе с критериями и основанием оценки",
    "observation_response_iwcll": "Недопустимое значение ответа для iwCLL 2018",
    "observation_response_lugano": "Недопустимое значение ответа для Lugano 2014",
    "observation_event_date": "У события должна быть дата (у «нет события» — без даты)",
    "observation_death_cause_required": "Для события «смерть» укажите причину (допустимо «неизвестно»)",
    "observation_line_fkey": "Линия терапии принадлежит другому пациенту",
    "therapy_line_end_after_start": "Окончание линии не может быть раньше начала",
    "therapy_line_patient_id_line_no_key": "Линия с таким номером уже есть",
    "module_data_value_or_missing": "Укажите значение или код пропуска",
    "module_data_marker_uq": "Результат этого маркера на эту дату уже внесён",
    "app_user_login_key": "Пользователь с таким логином уже есть",
    "app_user_physician_branch": "Для врача обязателен филиал",
    "branch_access_request_pending_uq": "Запрос доступа уже ожидает решения куратора",
}


def db_error_message(exc: psycopg.Error) -> str:
    diag = getattr(exc, "diag", None)
    name = getattr(diag, "constraint_name", None)
    if name in _CONSTRAINT_MESSAGES:
        return _CONSTRAINT_MESSAGES[name]
    if isinstance(exc, psycopg.errors.RaiseException) and diag is not None:
        return diag.message_primary  # сообщения триггеров уже на русском
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return "Недостаточно прав для этого действия"
    return "Данные не прошли проверку базы данных"
