-- ============================================================================
--  KALYPSO — миграция 0015: модуль волосатоклеточного лейкоза (ВКЛ)
-- ============================================================================
-- Стадирования и прогностического индекса нет. Ответ — консенсусные критерии 2017
-- (CR / PR / SD / PD по крови и костному мозгу).

ALTER TABLE observation DROP CONSTRAINT observation_response_criteria_check;
ALTER TABLE observation
    ADD CONSTRAINT observation_response_criteria_check CHECK (
        response_criteria IN ('iwCLL 2018', 'Lugano 2014', 'IWWM-11', 'IPCG', 'HCL 2017')),
    ADD CONSTRAINT observation_response_hcl CHECK (
        response_criteria IS DISTINCT FROM 'HCL 2017'
        OR response_value IN ('CR', 'PR', 'SD', 'PD'));
