-- ============================================================================
--  KALYPSO — миграция 0007: модуль фолликулярной лимфомы (ФЛ)
-- ============================================================================
-- Стадия Lugano и ответ Lugano 2014 уже проверяются (0005). Здесь — индекс FLIPI
-- в карточке. Поля модуля (грейд, зоны лимфоузлов, костный мозг, β2M/ЛДГ относительно
-- ВГН, GELF, трансформация) — строки module_data с module = 'follicular'.
-- FLIPI-2 и PRIMA-PI в карточку не вносятся — только расчёт по данным.

ALTER TABLE patient
    ADD CONSTRAINT patient_index_flipi CHECK (index_name <> 'FLIPI' OR (
        index_score BETWEEN 0 AND 5
        AND index_group IN ('low', 'intermediate', 'high')));
