-- ============================================================================
--  KALYPSO — миграция 0011: модуль лимфом маргинальной зоны (ЛМЗ)
-- ============================================================================
-- Индекс карточки — MALT-IPI (0–3; применим к экстранодальной ЛМЗ, для нодальной и
-- селезёночной — код пропуска). Подтип, локализация MALT, H. pylori, HCV — module_data.

ALTER TABLE patient DROP CONSTRAINT patient_index_name;
ALTER TABLE patient
    ADD CONSTRAINT patient_index_name CHECK (index_name IN (
        'CLL-IPI', 'IPI', 'R-IPI', 'FLIPI', 'MIPI', 'IPS', 'sMIPI', 'IPSS-WM', 'MALT-IPI')),
    ADD CONSTRAINT patient_index_malt_ipi CHECK (index_name <> 'MALT-IPI' OR (
        index_score BETWEEN 0 AND 3
        AND index_group IN ('low', 'intermediate', 'high')));
