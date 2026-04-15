-- ============================================================================
-- Migration 013: Distractor meta + Error classification columns
-- ============================================================================
-- Adds:
--   tasks_master.distractor_meta  JSONB   — предрассчитанные дистракторы
--   answer_events.error_type      VARCHAR — тип ошибки (sign_error, partial_result, ...)
--   answer_events.error_explanation TEXT  — описание ошибки
--   answer_events.matched_distractor TEXT — значение совпавшего дистрактора
-- ============================================================================

BEGIN;

-- 1. distractor_meta в tasks_master
ALTER TABLE tasks_master
    ADD COLUMN IF NOT EXISTS distractor_meta JSONB;

COMMENT ON COLUMN tasks_master.distractor_meta IS
    'Предрассчитанные дистракторы: [{value, error_type, explanation, plausibility, broken_step}]';

-- 2. Error classification в answer_events
ALTER TABLE answer_events
    ADD COLUMN IF NOT EXISTS error_type VARCHAR(50),
    ADD COLUMN IF NOT EXISTS error_explanation TEXT,
    ADD COLUMN IF NOT EXISTS matched_distractor TEXT;

COMMENT ON COLUMN answer_events.error_type IS
    'Тип ошибки: sign_error, partial_result, formula_error, wrong_operation, coefficient_error, simplification_error, unclassified';
COMMENT ON COLUMN answer_events.error_explanation IS
    'Человекочитаемое описание ошибки ученика';
COMMENT ON COLUMN answer_events.matched_distractor IS
    'Значение дистрактора, совпавшего с ответом ученика (NULL если ответ правильный)';

-- 3. Индекс для аналитики ошибок
CREATE INDEX IF NOT EXISTS idx_ae_error_type 
    ON answer_events (error_type) 
    WHERE error_type IS NOT NULL;

-- 4. Индекс для задач с готовыми дистракторами
CREATE INDEX IF NOT EXISTS idx_tm_has_distractors
    ON tasks_master ((distractor_meta IS NOT NULL))
    WHERE distractor_meta IS NOT NULL;

COMMIT;
