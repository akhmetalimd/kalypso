-- ============================================================================
--  KALYPSO — миграция 0014: модуль первичной лимфомы ЦНС (ПЛЦНС)
-- ============================================================================
-- Стадирования нет. Индекс карточки — IELSG (0–5); классы MSKCC — только расчёт.
-- Ответ — критерии IPCG (CR / CRu / PR / SD / PD), основание оценки — МРТ ('mri').

ALTER TABLE patient DROP CONSTRAINT patient_index_name;
ALTER TABLE patient
    ADD CONSTRAINT patient_index_name CHECK (index_name IN (
        'CLL-IPI', 'IPI', 'R-IPI', 'FLIPI', 'MIPI', 'IPS', 'sMIPI', 'IPSS-WM', 'MALT-IPI', 'aaIPI', 'BL-IPI',
        'IELSG')),
    ADD CONSTRAINT patient_index_ielsg CHECK (index_name <> 'IELSG' OR (
        index_score BETWEEN 0 AND 5
        AND index_group IN ('low', 'intermediate', 'high')));

ALTER TABLE observation DROP CONSTRAINT observation_response_criteria_check;
ALTER TABLE observation DROP CONSTRAINT observation_response_basis_check;
ALTER TABLE observation
    ADD CONSTRAINT observation_response_criteria_check CHECK (
        response_criteria IN ('iwCLL 2018', 'Lugano 2014', 'IWWM-11', 'IPCG')),
    ADD CONSTRAINT observation_response_basis_check CHECK (
        response_basis IN ('clinical', 'ct', 'pet_ct', 'morphology', 'laboratory', 'mri')),
    ADD CONSTRAINT observation_response_ipcg CHECK (
        response_criteria IS DISTINCT FROM 'IPCG'
        OR response_value IN ('CR', 'CRu', 'PR', 'SD', 'PD'));
