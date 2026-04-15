-- ============================================================================
-- 010_skeleton_extension_v2.sql
-- Расширяемый скелет: origin, is_advanced, source_textbook_id
-- Дата: 2026-02-15
-- ============================================================================

-- ============================================================================
-- 1. Новые колонки в knowledge_hierarchy
-- ============================================================================
DO $$
BEGIN
    -- origin: откуда пришёл навык (ТОЛЬКО метаданные, НЕ фильтр)
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'knowledge_hierarchy' AND column_name = 'origin'
    ) THEN
        ALTER TABLE knowledge_hierarchy
            ADD COLUMN origin VARCHAR(20) DEFAULT 'base'
            CHECK (origin IN ('base', 'textbook', 'olympiad'));
        COMMENT ON COLUMN knowledge_hierarchy.origin IS
            'Источник навыка: base=ФГОС, textbook=из учебника, olympiad=олимпиадный. Только метаданные, НЕ фильтр доступа.';
    END IF;

    -- is_advanced: ★-задачи повышенной сложности
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'knowledge_hierarchy' AND column_name = 'is_advanced'
    ) THEN
        ALTER TABLE knowledge_hierarchy
            ADD COLUMN is_advanced BOOLEAN DEFAULT FALSE;
        COMMENT ON COLUMN knowledge_hierarchy.is_advanced IS
            'Навык повышенной сложности (★). CAT показывает при θ > 1.5';
    END IF;

    -- source_textbook_id: из какого учебника пришёл навык
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'knowledge_hierarchy' AND column_name = 'source_textbook_id'
    ) THEN
        ALTER TABLE knowledge_hierarchy
            ADD COLUMN source_textbook_id UUID REFERENCES textbooks(textbook_id);
        COMMENT ON COLUMN knowledge_hierarchy.source_textbook_id IS
            'UUID учебника, из которого этот навык был добавлен (для origin=textbook)';
    END IF;
END $$;

-- Индексы
CREATE INDEX IF NOT EXISTS idx_kh_origin ON knowledge_hierarchy(origin);
CREATE INDEX IF NOT EXISTS idx_kh_advanced ON knowledge_hierarchy(is_advanced) WHERE is_advanced = TRUE;
CREATE INDEX IF NOT EXISTS idx_kh_source_tb ON knowledge_hierarchy(source_textbook_id) WHERE source_textbook_id IS NOT NULL;

-- ============================================================================
-- 2. Обновить существующие навыки: все текущие = base, не advanced
-- ============================================================================
UPDATE knowledge_hierarchy
SET origin = 'base', is_advanced = FALSE
WHERE origin IS NULL;

-- ============================================================================
-- DONE
-- ============================================================================
