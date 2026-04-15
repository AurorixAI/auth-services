-- ============================================================================
-- 012_variant_engine.sql
-- Расширение task_templates + таблица кэша вариантов
--
-- Изменения:
--   1. task_templates: добавлены колонки для SymPy-движка
--      - answer_formula_sympy     (srepr() SymPy-выражение)
--      - constraints_repr         (JSONB: список строк-constraints)
--      - task_type                (тип задачи: linear, quadratic, geometry...)
--      - text_template            (шаблон текста с {a}, {b}, {c})
--      - original_values          (JSONB: оригинальные значения параметров)
--      - param_details            (JSONB: расширенная информация о параметрах)
--
--   2. Новая таблица exam_variant_cache:
--      Кэширует сгенерированные варианты с метаданными
--
--   3. generated_exams.status: добавить 'variants_ready'
--
-- Дата: 2026-02-22
-- ============================================================================

BEGIN;

-- ============================================================================
-- 1. Расширение task_templates для SymPy движка
-- ============================================================================

-- SymPy-представление формулы ответа (через srepr())
ALTER TABLE task_templates
    ADD COLUMN IF NOT EXISTS answer_formula_sympy TEXT;

-- Constraints в формате SymPy (список строковых выражений)
ALTER TABLE task_templates
    ADD COLUMN IF NOT EXISTS constraints_repr JSONB DEFAULT '[]'::jsonb;

-- Тип задачи (для выбора solver'а)
ALTER TABLE task_templates
    ADD COLUMN IF NOT EXISTS task_type VARCHAR(30) DEFAULT 'arithmetic';

-- Шаблон текста с именованными плейсхолдерами {a}, {b}, {c}
ALTER TABLE task_templates
    ADD COLUMN IF NOT EXISTS text_template TEXT;

-- Оригинальные значения параметров (для воспроизведения исходной задачи)
ALTER TABLE task_templates
    ADD COLUMN IF NOT EXISTS original_values JSONB DEFAULT '{}'::jsonb;

-- Расширенная информация о параметрах:
-- {"a": {"min": 1, "max": 20, "type": "int", "exclude": [0]}, ...}
ALTER TABLE task_templates
    ADD COLUMN IF NOT EXISTS param_details JSONB DEFAULT '{}'::jsonb;

-- Индекс по типу задачи
CREATE INDEX IF NOT EXISTS idx_tt_task_type ON task_templates(task_type);


-- ============================================================================
-- 2. Таблица кэша вариантов экзамена
-- ============================================================================
-- Хранит метаданные о генерации вариантов для экзамена.
-- Сами варианты хранятся в generated_exams.variants (JSONB).
-- Эта таблица — для аудита, статистики, повторной генерации.

CREATE TABLE IF NOT EXISTS exam_variant_cache (
    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
    exam_id VARCHAR(50) NOT NULL REFERENCES generated_exams(id) ON DELETE CASCADE,

    -- Параметры генерации
    num_variants INTEGER NOT NULL DEFAULT 2,
    seed INTEGER NOT NULL DEFAULT 42,

    -- Результат
    tasks_templatized INTEGER DEFAULT 0,
    tasks_fallback INTEGER DEFAULT 0,
    tasks_total INTEGER DEFAULT 0,

    -- Ошибки
    errors JSONB DEFAULT '[]'::jsonb,

    -- Шаблоны (snapshot): {task_id: {template_text, answer_formula, params, ...}}
    templates_snapshot JSONB DEFAULT '{}'::jsonb,

    -- Временные метки
    created_at TIMESTAMP DEFAULT NOW(),
    generation_time_ms INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_evc_exam ON exam_variant_cache(exam_id);


-- ============================================================================
-- 3. generated_exams: обновить допустимые статусы
-- ============================================================================
-- PostgreSQL: добавить проверку, если нет — создать
-- (status уже VARCHAR(20), просто добавляем CHECK)
-- Удаляем старый CHECK если есть, добавляем новый

DO $$
BEGIN
    -- Пробуем удалить старое ограничение
    ALTER TABLE generated_exams DROP CONSTRAINT IF EXISTS chk_exam_status;
EXCEPTION
    WHEN OTHERS THEN NULL;
END $$;

ALTER TABLE generated_exams
    ADD CONSTRAINT chk_exam_status
    CHECK (status IN ('draft', 'variants_ready', 'active', 'closed'));


COMMIT;
