-- ============================================================================
--  KALYPSO — миграция 0005: модуль ДВКЛ (DLBCL, NOS)
-- ============================================================================
-- Схема уже допускает Lugano, IPI, ответ Lugano 2014 и балл Deauville; здесь —
-- значения, которые сервис проверяет первым (kalypso/registry.py, registry_core).
-- Поля модуля (клеточное происхождение, перестройки MYC/BCL2/BCL6, ЛДГ, экстранодальные
-- очаги, bulky) — строки module_data с module = 'dlbcl', без новых колонок.

ALTER TABLE patient
    ADD CONSTRAINT patient_stage_lugano CHECK (
        stage_system <> 'Lugano' OR stage_value IN ('I', 'IE', 'II', 'IIE', 'III', 'IV')),
    ADD CONSTRAINT patient_index_ipi CHECK (index_name <> 'IPI' OR (
        index_score BETWEEN 0 AND 5
        AND index_group IN ('low', 'low_intermediate', 'high_intermediate', 'high')));

-- Lugano 2014: по ПЭТ-КТ — метаболический ответ с баллом Deauville (CMR — 1–3,
-- PMR/NMR/PMD — 4–5); по КТ и другим основаниям — CR/PR/SD/PD.
ALTER TABLE observation
    ADD CONSTRAINT observation_lugano_basis CHECK (
        response_criteria IS DISTINCT FROM 'Lugano 2014'
        OR (response_value IN ('CMR', 'PMR', 'NMR', 'PMD')) = (response_basis = 'pet_ct')),
    ADD CONSTRAINT observation_lugano_deauville CHECK (
        response_criteria IS DISTINCT FROM 'Lugano 2014'
        OR response_value NOT IN ('CMR', 'PMR', 'NMR', 'PMD')
        OR (deauville IS NOT NULL AND (response_value = 'CMR') = (deauville <= 3)));
