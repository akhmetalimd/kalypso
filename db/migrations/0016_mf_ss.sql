-- ============================================================================
--  KALYPSO — миграция 0016: грибовидный микоз / синдром Сезари (ГМ/СС)
-- ============================================================================
-- Новая система стадирования ISCL/EORTC (IA–IVB; категории TNMB — поля модуля),
-- ответ — глобальный ответ ISCL 2011 (CR / PR / SD / PD). CLIPi — только расчёт.

ALTER TABLE patient DROP CONSTRAINT patient_stage_system_check;
ALTER TABLE patient
    ADD CONSTRAINT patient_stage_system_check CHECK (
        stage_system IN ('Rai', 'Binet', 'Lugano', 'Ann Arbor', 'ISCL/EORTC')),
    ADD CONSTRAINT patient_stage_iscl CHECK (stage_system <> 'ISCL/EORTC' OR stage_value IN (
        'IA', 'IB', 'IIA', 'IIB', 'IIIA', 'IIIB', 'IVA1', 'IVA2', 'IVB'));

ALTER TABLE observation DROP CONSTRAINT observation_response_criteria_check;
ALTER TABLE observation
    ADD CONSTRAINT observation_response_criteria_check CHECK (
        response_criteria IN ('iwCLL 2018', 'Lugano 2014', 'IWWM-11', 'IPCG', 'HCL 2017', 'ISCL 2011')),
    ADD CONSTRAINT observation_response_iscl CHECK (
        response_criteria IS DISTINCT FROM 'ISCL 2011'
        OR response_value IN ('CR', 'PR', 'SD', 'PD'));
