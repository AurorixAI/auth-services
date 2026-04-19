-- ============================================================================
-- 014: Добавление полей методиста в knowledge_hierarchy
-- Новые поля хранят контекст из PDF-программы методиста:
--   example_task     — задача-образец для понимания сути навыка
--   assessed_ability — проверяемое умение (что именно проверяется)
--   difficulty_level — уровень сложности из программы (Low/Medium/High + Level A/B/C)
--   formula          — математическая формула / правило, если есть
-- ============================================================================

ALTER TABLE knowledge_hierarchy
    ADD COLUMN IF NOT EXISTS example_task TEXT,
    ADD COLUMN IF NOT EXISTS assessed_ability VARCHAR(300),
    ADD COLUMN IF NOT EXISTS difficulty_level VARCHAR(30),
    ADD COLUMN IF NOT EXISTS formula TEXT;

COMMENT ON COLUMN knowledge_hierarchy.example_task IS 'Задача-образец из программы методиста (PDF)';
COMMENT ON COLUMN knowledge_hierarchy.assessed_ability IS 'Проверяемое умение — что именно проверяет навык';
COMMENT ON COLUMN knowledge_hierarchy.difficulty_level IS 'Уровень сложности: Low (Level A), Medium (Level B), High (Level C)';
COMMENT ON COLUMN knowledge_hierarchy.formula IS 'Математическая формула или правило из программы методиста (NULL если нет)';
