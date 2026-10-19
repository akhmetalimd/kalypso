-- ============================================================================
--  KALYPSO — миграция 0008: модуль мантийноклеточной лимфомы (МКЛ)
-- ============================================================================
-- Индекс карточки — sMIPI (упрощённый MIPI, 0–11). MIPI-c — только расчёт по данным.
-- Поля модуля (ЛДГ/ВГН, Ki-67, TP53, бластоидный вариант…) — module_data, module = 'mcl'.

ALTER TABLE patient DROP CONSTRAINT patient_index_name;
ALTER TABLE patient
    ADD CONSTRAINT patient_index_name CHECK (index_name IN (
        'CLL-IPI', 'IPI', 'R-IPI', 'FLIPI', 'MIPI', 'IPS', 'sMIPI')),
    ADD CONSTRAINT patient_index_smipi CHECK (index_name <> 'sMIPI' OR (
        index_score BETWEEN 0 AND 11
        AND index_group IN ('low', 'intermediate', 'high')));
