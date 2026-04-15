-- ============================================================================
-- 011_dual_architecture.sql
-- Дуальная архитектура: Ядро (knowledge_hierarchy) + Учебники (textbook_toc)
--
-- Ключевое решение:
--   • knowledge_hierarchy — чистая методика, НИКОГДА не расширяется из книг
--   • textbook_toc — 1:1 оглавление учебника, все задачи привязаны через toc_id
--   • tasks_master.skill_id → NULLABLE (задачи без маппинга на ядро допустимы)
--   • task_templates.skill_id → NULLABLE (шаблоны для textbook-only задач)
--   • CHECK: каждая задача должна иметь skill_id ИЛИ toc_id (или оба)
--
-- Пост-экзаменная аналитика:
--   • Задачи с skill_id → полная диагностика (дистракторы, пробелы, deep scan)
--   • Задачи без skill_id → только верно/неверно
--
-- Дата: 2026-02-21
-- ============================================================================

BEGIN;

-- ============================================================================
-- 1. tasks_master.skill_id → NULLABLE
-- ============================================================================
ALTER TABLE tasks_master ALTER COLUMN skill_id DROP NOT NULL;

-- CHECK: задача привязана хотя бы к ядру (skill_id) ИЛИ к учебнику (toc_id)
ALTER TABLE tasks_master ADD CONSTRAINT chk_task_has_binding
    CHECK (skill_id IS NOT NULL OR toc_id IS NOT NULL);

-- Частичный индекс: быстрая фильтрация unmapped задач
CREATE INDEX IF NOT EXISTS idx_tm_unmapped
    ON tasks_master(toc_id) WHERE skill_id IS NULL;

-- Частичный индекс: задачи с маппингом на ядро (для CAT/диагностики)
CREATE INDEX IF NOT EXISTS idx_tm_mapped
    ON tasks_master(skill_id) WHERE skill_id IS NOT NULL;

-- ============================================================================
-- 2. task_templates.skill_id → NULLABLE
-- ============================================================================
ALTER TABLE task_templates ALTER COLUMN skill_id DROP NOT NULL;

-- ============================================================================
-- 3. Обновить SQL-функцию select_exam_tasks
--    JOIN → LEFT JOIN (чтобы задачи без skill_id попадали в выборку по toc)
-- ============================================================================
CREATE OR REPLACE FUNCTION select_exam_tasks(
    p_toc_ids   INTEGER[],
    p_count     INTEGER DEFAULT 10,
    p_difficulty VARCHAR DEFAULT NULL
)
RETURNS TABLE (
    task_id         VARCHAR,
    skill_id        VARCHAR,
    skill_name      VARCHAR,
    difficulty      VARCHAR,
    question_text   TEXT,
    question_latex  TEXT,
    toc_id          INTEGER,
    toc_title       VARCHAR,
    has_core_mapping BOOLEAN
) AS $$
BEGIN
    RETURN QUERY
    SELECT tm.id,
           tm.skill_id,
           COALESCE(kh.name_ru, tt.title)::VARCHAR AS skill_name,
           tm.difficulty,
           tm.question_text,
           tm.question_latex,
           tm.toc_id,
           tt.title,
           (tm.skill_id IS NOT NULL) AS has_core_mapping
    FROM tasks_master tm
    LEFT JOIN knowledge_hierarchy kh ON kh.id = tm.skill_id
    LEFT JOIN textbook_toc tt ON tt.id = tm.toc_id
    WHERE tm.toc_id = ANY(p_toc_ids)
      AND tm.is_active = TRUE
      AND tm.verification_status = 'verified'
      AND (p_difficulty IS NULL OR tm.difficulty = p_difficulty)
    ORDER BY RANDOM()
    LIMIT p_count;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- 4. Обновить select_exam_tasks_balanced (вызывает select_exam_tasks)
--    Добавляем has_core_mapping в RETURN TABLE
-- ============================================================================
CREATE OR REPLACE FUNCTION select_exam_tasks_balanced(
    p_toc_ids    INTEGER[],
    p_total      INTEGER DEFAULT 10,
    p_ratio_a    NUMERIC DEFAULT 0.3,
    p_ratio_b    NUMERIC DEFAULT 0.5,
    p_ratio_c    NUMERIC DEFAULT 0.2
)
RETURNS TABLE (
    task_id         VARCHAR,
    skill_id        VARCHAR,
    skill_name      VARCHAR,
    difficulty      VARCHAR,
    question_text   TEXT,
    question_latex  TEXT,
    toc_id          INTEGER,
    toc_title       VARCHAR,
    has_core_mapping BOOLEAN
) AS $$
DECLARE
    v_count_a INTEGER := ROUND(p_total * p_ratio_a);
    v_count_b INTEGER := ROUND(p_total * p_ratio_b);
    v_count_c INTEGER := p_total - v_count_a - v_count_b;
BEGIN
    RETURN QUERY
    (SELECT * FROM select_exam_tasks(p_toc_ids, v_count_a, 'A'))
    UNION ALL
    (SELECT * FROM select_exam_tasks(p_toc_ids, v_count_b, 'B'))
    UNION ALL
    (SELECT * FROM select_exam_tasks(p_toc_ids, v_count_c, 'C'));
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- 5. Обновить get_skills_for_toc — добавить unmapped_tasks_count
-- ============================================================================
CREATE OR REPLACE FUNCTION get_skills_for_toc(p_toc_id INTEGER)
RETURNS TABLE (
    skill_id            VARCHAR,
    skill_name          VARCHAR,
    role                VARCHAR,
    confidence          NUMERIC,
    is_main             BOOLEAN,
    tasks_count         BIGINT,
    unmapped_tasks_count BIGINT
) AS $$
BEGIN
    RETURN QUERY
    SELECT kh.id, kh.name_ru, tsm.role, tsm.confidence, tsm.is_main,
           COUNT(tm.id) FILTER (WHERE tm.skill_id IS NOT NULL) AS tasks_count,
           (SELECT COUNT(*)
            FROM tasks_master tm2
            WHERE tm2.toc_id = p_toc_id
              AND tm2.skill_id IS NULL
              AND tm2.is_active = TRUE) AS unmapped_tasks_count
    FROM textbook_skill_map tsm
    JOIN knowledge_hierarchy kh ON kh.id = tsm.skill_id
    LEFT JOIN tasks_master tm ON tm.skill_id = tsm.skill_id
        AND tm.toc_id = tsm.toc_id AND tm.is_active = TRUE
    WHERE tsm.toc_id = p_toc_id
    GROUP BY kh.id, kh.name_ru, tsm.role, tsm.confidence, tsm.is_main
    ORDER BY tsm.is_main DESC, tsm.role;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- 6. Обновить view v_textbook_coverage — добавить unmapped_tasks
-- ============================================================================
CREATE OR REPLACE VIEW v_textbook_coverage AS
SELECT
    t.textbook_id,
    t.title AS textbook_title,
    toc.id AS toc_id,
    toc.level,
    toc.number,
    toc.title AS toc_title,
    COUNT(DISTINCT tsm.skill_id) AS skills_mapped,
    COUNT(DISTINCT tm.id) FILTER (WHERE tm.skill_id IS NOT NULL) AS tasks_mapped,
    COUNT(DISTINCT tm.id) FILTER (WHERE tm.skill_id IS NULL) AS tasks_unmapped,
    COUNT(DISTINCT tm.id) AS tasks_total,
    toc.is_digitized
FROM textbooks t
JOIN textbook_toc toc ON toc.textbook_id = t.textbook_id
LEFT JOIN textbook_skill_map tsm ON tsm.toc_id = toc.id
LEFT JOIN tasks_master tm ON tm.toc_id = toc.id AND tm.is_active = TRUE
GROUP BY t.textbook_id, t.title, toc.id, toc.level, toc.number, toc.title, toc.is_digitized
ORDER BY toc.sort_order;

-- ============================================================================
-- 7. Запрет расширения ядра из учебников:
--    origin='textbook' больше не создаётся, но колонку оставляем как метаданные.
--    source_textbook_id — оставляем (безопасно, не удаляем колонку).
--    Обновляем комментарий для ясности.
-- ============================================================================
COMMENT ON COLUMN knowledge_hierarchy.origin IS
    'Источник навыка: base=ФГОС (единственный допустимый для новых навыков). '
    'textbook/olympiad — legacy-метаданные, новые навыки с origin!=base НЕ создаются.';

COMMENT ON COLUMN knowledge_hierarchy.source_textbook_id IS
    'DEPRECATED. Навыки ядра НЕ расширяются из учебников. '
    'Колонка оставлена для обратной совместимости.';

COMMIT;

-- ============================================================================
-- DONE — Дуальная архитектура активирована
-- ============================================================================
