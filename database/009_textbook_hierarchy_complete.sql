-- ============================================================================
-- 009_textbook_hierarchy_complete.sql
-- Дуальный маппинг: textbook_toc + textbook_skill_map + расширения tasks_master
-- Дата: 2026-02-15
-- ============================================================================

-- ============================================================================
-- 1. textbook_toc — 1:1 копия оглавления учебника (рекурсивная)
-- ============================================================================
CREATE TABLE IF NOT EXISTS textbook_toc (
    id              SERIAL PRIMARY KEY,
    textbook_id     UUID NOT NULL REFERENCES textbooks(textbook_id) ON DELETE CASCADE,
    parent_id       INTEGER REFERENCES textbook_toc(id) ON DELETE CASCADE,

    -- Уровень: textbook > part > chapter > section > paragraph
    level           VARCHAR(20) NOT NULL
        CHECK (level IN ('textbook', 'part', 'chapter', 'section', 'paragraph')),

    -- Нумерация как в книге
    number          VARCHAR(30),          -- "§3.2", "Глава 5", "1.1"
    title           VARCHAR(500) NOT NULL, -- "Сложение дробей с разными знаменателями"
    full_path       TEXT,                  -- "Глава 3 / §3.2 / Упражнения" (auto-gen)

    -- Страницы
    page_start      INTEGER,
    page_end        INTEGER,

    -- Сортировка
    sort_order      INTEGER NOT NULL DEFAULT 0,

    -- Статус оцифровки
    tasks_extracted INTEGER DEFAULT 0,
    is_digitized    BOOLEAN DEFAULT FALSE,

    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_toc_textbook ON textbook_toc(textbook_id);
CREATE INDEX idx_toc_parent   ON textbook_toc(parent_id);
CREATE INDEX idx_toc_level    ON textbook_toc(level);

-- ============================================================================
-- 2. textbook_skill_map — мост: параграф учебника ↔ навыки скелета
-- ============================================================================
CREATE TABLE IF NOT EXISTS textbook_skill_map (
    id              SERIAL PRIMARY KEY,
    toc_id          INTEGER NOT NULL REFERENCES textbook_toc(id) ON DELETE CASCADE,
    skill_id        VARCHAR(30) NOT NULL REFERENCES knowledge_hierarchy(id) ON DELETE CASCADE,

    -- Роль навыка в параграфе
    role            VARCHAR(20) NOT NULL DEFAULT 'primary'
        CHECK (role IN ('primary', 'secondary', 'prerequisite', 'extension')),

    -- Уверенность маппинга (0.0 – 1.0)
    confidence      NUMERIC(3, 2) DEFAULT 0.8,

    -- Главный навык параграфа (для быстрого поиска)
    is_main         BOOLEAN DEFAULT FALSE,

    -- Если навык — связь с темой из другого параграфа
    cross_topic_id  INTEGER REFERENCES textbook_toc(id),

    created_at      TIMESTAMP DEFAULT NOW(),

    UNIQUE (toc_id, skill_id)
);

CREATE INDEX idx_tsm_toc   ON textbook_skill_map(toc_id);
CREATE INDEX idx_tsm_skill ON textbook_skill_map(skill_id);
CREATE INDEX idx_tsm_main  ON textbook_skill_map(is_main) WHERE is_main = TRUE;

-- ============================================================================
-- 3. Расширения tasks_master: toc_id + tags
-- ============================================================================
DO $$
BEGIN
    -- FK на textbook_toc
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tasks_master' AND column_name = 'toc_id'
    ) THEN
        ALTER TABLE tasks_master ADD COLUMN toc_id INTEGER REFERENCES textbook_toc(id);
        CREATE INDEX idx_tm_toc ON tasks_master(toc_id);
    END IF;

    -- JSONB теги: {"page": 42, "exercise": "3.15", "variant": "а", "star": true}
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tasks_master' AND column_name = 'tags'
    ) THEN
        ALTER TABLE tasks_master ADD COLUMN tags JSONB DEFAULT '{}';
        CREATE INDEX idx_tm_tags ON tasks_master USING GIN(tags);
    END IF;
END $$;

-- ============================================================================
-- 4. SQL-функции для Модуля 2 (экзамен по учебнику)
-- ============================================================================

-- Получить дерево оглавления учебника (рекурсивно)
CREATE OR REPLACE FUNCTION get_textbook_toc_tree(p_textbook_id UUID)
RETURNS TABLE (
    id          INTEGER,
    parent_id   INTEGER,
    level       VARCHAR,
    number      VARCHAR,
    title       VARCHAR,
    page_start  INTEGER,
    sort_order  INTEGER,
    depth       INTEGER,
    tasks_count BIGINT
) AS $$
BEGIN
    RETURN QUERY
    WITH RECURSIVE toc_tree AS (
        SELECT t.id, t.parent_id, t.level, t.number, t.title,
               t.page_start, t.sort_order, 0 AS depth
        FROM textbook_toc t
        WHERE t.textbook_id = p_textbook_id AND t.parent_id IS NULL

        UNION ALL

        SELECT t.id, t.parent_id, t.level, t.number, t.title,
               t.page_start, t.sort_order, tt.depth + 1
        FROM textbook_toc t
        JOIN toc_tree tt ON t.parent_id = tt.id
    )
    SELECT tt.id, tt.parent_id, tt.level, tt.number, tt.title,
           tt.page_start, tt.sort_order, tt.depth,
           COALESCE(tc.cnt, 0) AS tasks_count
    FROM toc_tree tt
    LEFT JOIN (
        SELECT tm.toc_id, COUNT(*) AS cnt
        FROM tasks_master tm
        WHERE tm.toc_id IS NOT NULL AND tm.is_active = TRUE
        GROUP BY tm.toc_id
    ) tc ON tc.toc_id = tt.id
    ORDER BY tt.depth, tt.sort_order;
END;
$$ LANGUAGE plpgsql;

-- Получить навыки для параграфа
CREATE OR REPLACE FUNCTION get_skills_for_toc(p_toc_id INTEGER)
RETURNS TABLE (
    skill_id    VARCHAR,
    skill_name  VARCHAR,
    role        VARCHAR,
    confidence  NUMERIC,
    is_main     BOOLEAN,
    tasks_count BIGINT
) AS $$
BEGIN
    RETURN QUERY
    SELECT kh.id, kh.name_ru, tsm.role, tsm.confidence, tsm.is_main,
           COUNT(tm.id) AS tasks_count
    FROM textbook_skill_map tsm
    JOIN knowledge_hierarchy kh ON kh.id = tsm.skill_id
    LEFT JOIN tasks_master tm ON tm.skill_id = tsm.skill_id
        AND tm.toc_id = tsm.toc_id AND tm.is_active = TRUE
    WHERE tsm.toc_id = p_toc_id
    GROUP BY kh.id, kh.name_ru, tsm.role, tsm.confidence, tsm.is_main
    ORDER BY tsm.is_main DESC, tsm.role;
END;
$$ LANGUAGE plpgsql;

-- Выбрать задачи для экзамена по параграфам
CREATE OR REPLACE FUNCTION select_exam_tasks(
    p_toc_ids   INTEGER[],
    p_count     INTEGER DEFAULT 10,
    p_difficulty VARCHAR DEFAULT NULL   -- 'A','B','C' или NULL (все)
)
RETURNS TABLE (
    task_id         VARCHAR,
    skill_id        VARCHAR,
    skill_name      VARCHAR,
    difficulty      VARCHAR,
    question_text   TEXT,
    question_latex  TEXT,
    toc_id          INTEGER,
    toc_title       VARCHAR
) AS $$
BEGIN
    RETURN QUERY
    SELECT tm.id, tm.skill_id, kh.name_ru, tm.difficulty,
           tm.question_text, tm.question_latex,
           tm.toc_id, tt.title
    FROM tasks_master tm
    JOIN knowledge_hierarchy kh ON kh.id = tm.skill_id
    LEFT JOIN textbook_toc tt ON tt.id = tm.toc_id
    WHERE tm.toc_id = ANY(p_toc_ids)
      AND tm.is_active = TRUE
      AND tm.verification_status = 'verified'
      AND (p_difficulty IS NULL OR tm.difficulty = p_difficulty)
    ORDER BY RANDOM()
    LIMIT p_count;
END;
$$ LANGUAGE plpgsql;

-- Выбрать задачи с учётом difficulty_distribution
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
    toc_title       VARCHAR
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
-- 5. View: покрытие учебника
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
    COUNT(DISTINCT tm.id) AS tasks_available,
    toc.is_digitized
FROM textbooks t
JOIN textbook_toc toc ON toc.textbook_id = t.textbook_id
LEFT JOIN textbook_skill_map tsm ON tsm.toc_id = toc.id
LEFT JOIN tasks_master tm ON tm.toc_id = toc.id AND tm.is_active = TRUE
GROUP BY t.textbook_id, t.title, toc.id, toc.level, toc.number, toc.title, toc.is_digitized
ORDER BY toc.sort_order;

-- ============================================================================
-- DONE
-- ============================================================================
