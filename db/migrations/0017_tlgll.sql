-- ============================================================================
--  KALYPSO — миграция 0017: T-клеточный лейкоз из больших гранулярных лимфоцитов (T-LGLL)
-- ============================================================================
-- Стадирования и индекса нет. Ответ — гематологический (CR / PR / NR — нет ответа / PD).

ALTER TABLE observation DROP CONSTRAINT observation_response_criteria_check;
ALTER TABLE observation
    ADD CONSTRAINT observation_response_criteria_check CHECK (
        response_criteria IN ('iwCLL 2018', 'Lugano 2014', 'IWWM-11', 'IPCG', 'HCL 2017', 'ISCL 2011', 'T-LGLL')),
    ADD CONSTRAINT observation_response_tlgll CHECK (
        response_criteria IS DISTINCT FROM 'T-LGLL'
        OR response_value IN ('CR', 'PR', 'NR', 'PD'));
