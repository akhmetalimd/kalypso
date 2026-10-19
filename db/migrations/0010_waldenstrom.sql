-- ============================================================================
--  KALYPSO — миграция 0010: модуль макроглобулинемии Вальденстрёма (МВ)
-- ============================================================================
-- Стадирования нет. Индекс карточки — IPSS-WM (0–5 факторов; при одном факторе группа
-- зависит от того, возраст ли это). rIPSS-WM — только расчёт.
-- Ответ — критерии IWWM-11, оценка по парапротеину: основание «laboratory».

ALTER TABLE patient DROP CONSTRAINT patient_index_name;
ALTER TABLE patient
    ADD CONSTRAINT patient_index_name CHECK (index_name IN (
        'CLL-IPI', 'IPI', 'R-IPI', 'FLIPI', 'MIPI', 'IPS', 'sMIPI', 'IPSS-WM')),
    ADD CONSTRAINT patient_index_ipss_wm CHECK (index_name <> 'IPSS-WM' OR (
        index_score BETWEEN 0 AND 5
        AND index_group IN ('low', 'intermediate', 'high')
        AND (index_score <> 0 OR index_group = 'low')
        AND (index_score <> 2 OR index_group = 'intermediate')
        AND (index_score < 3 OR index_group = 'high')
        AND (index_score <> 1 OR index_group IN ('low', 'intermediate'))));

ALTER TABLE observation DROP CONSTRAINT observation_response_criteria_check;
ALTER TABLE observation DROP CONSTRAINT observation_response_basis_check;
ALTER TABLE observation
    ADD CONSTRAINT observation_response_criteria_check CHECK (
        response_criteria IN ('iwCLL 2018', 'Lugano 2014', 'IWWM-11')),
    ADD CONSTRAINT observation_response_basis_check CHECK (
        response_basis IN ('clinical', 'ct', 'pet_ct', 'morphology', 'laboratory')),
    ADD CONSTRAINT observation_response_iwwm CHECK (
        response_criteria IS DISTINCT FROM 'IWWM-11'
        OR response_value IN ('CR', 'VGPR', 'PR', 'MR', 'SD', 'PD'));
