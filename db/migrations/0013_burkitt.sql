-- ============================================================================
--  KALYPSO — миграция 0013: модуль лимфомы Бёркитта (ЛБ)
-- ============================================================================
-- Индекс карточки — BL-IPI (0–4). Поля (IG-MYC, ЦНС, костный мозг, ВИЧ, EBV) — module_data.

ALTER TABLE patient DROP CONSTRAINT patient_index_name;
ALTER TABLE patient
    ADD CONSTRAINT patient_index_name CHECK (index_name IN (
        'CLL-IPI', 'IPI', 'R-IPI', 'FLIPI', 'MIPI', 'IPS', 'sMIPI', 'IPSS-WM', 'MALT-IPI', 'aaIPI', 'BL-IPI')),
    ADD CONSTRAINT patient_index_bl_ipi CHECK (index_name <> 'BL-IPI' OR (
        index_score BETWEEN 0 AND 4
        AND index_group IN ('low', 'intermediate', 'high')));
