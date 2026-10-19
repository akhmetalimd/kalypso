-- ============================================================================
--  KALYPSO — миграция 0012: модуль первичной медиастинальной B-клеточной лимфомы (ПМБКЛ)
-- ============================================================================
-- Индекс карточки — aaIPI (IPI с поправкой на возраст, 0–3). Решающий прогноз — ПЭТ-КТ
-- после лечения (Deauville): группировка «Исследователя» от ориентира, без новых колонок.

ALTER TABLE patient DROP CONSTRAINT patient_index_name;
ALTER TABLE patient
    ADD CONSTRAINT patient_index_name CHECK (index_name IN (
        'CLL-IPI', 'IPI', 'R-IPI', 'FLIPI', 'MIPI', 'IPS', 'sMIPI', 'IPSS-WM', 'MALT-IPI', 'aaIPI')),
    ADD CONSTRAINT patient_index_aa_ipi CHECK (index_name <> 'aaIPI' OR (
        index_score BETWEEN 0 AND 3
        AND index_group IN ('low', 'low_intermediate', 'high_intermediate', 'high')));
