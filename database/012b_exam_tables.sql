-- ============================================================================
-- 012b_exam_tables.sql
-- DDL для таблиц экзаменов (ранее создавались только через ORM/Alembic)
--
-- Таблицы:
--   generated_exams  — сгенерированные учителем экзамены
--   exam_sessions    — сессии прохождения экзамена учениками
--
-- Дата: 2026-02-22
-- Зависимости: Нет (standalone DDL)
-- Должен выполняться ДО 012_variant_engine.sql (FK на generated_exams)
-- ============================================================================

BEGIN;

-- ============================================================================
-- 1. GENERATED_EXAMS — Экзамены, созданные учителями
-- ============================================================================

CREATE TABLE IF NOT EXISTS generated_exams (
    id VARCHAR(50) PRIMARY KEY,              -- EXAM_{hex8}
    org_id VARCHAR(50),
    teacher_id VARCHAR(50),
    teacher_name VARCHAR(200),

    title VARCHAR(300) NOT NULL,
    access_code VARCHAR(20) UNIQUE,          -- NULL = draft, set on publish
    subject VARCHAR(100) DEFAULT 'Математика',

    -- Content stored as JSONB
    variants JSONB NOT NULL DEFAULT '[]'::jsonb,        -- List of variant dicts with tasks
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,        -- time_minutes, shuffle_tasks, etc.
    skills_covered JSONB NOT NULL DEFAULT '[]'::jsonb,  -- List of skill_ids

    status VARCHAR(20) DEFAULT 'draft',     -- draft, variants_ready, active, closed
    total_time_minutes INTEGER DEFAULT 45,
    variants_count INTEGER DEFAULT 1,
    tasks_per_variant INTEGER DEFAULT 10,

    created_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_generated_exams_org ON generated_exams(org_id);
CREATE INDEX IF NOT EXISTS ix_generated_exams_teacher ON generated_exams(teacher_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_generated_exams_code ON generated_exams(access_code);


-- ============================================================================
-- 2. EXAM_SESSIONS — Сессии прохождения экзамена учениками
-- ============================================================================

CREATE TABLE IF NOT EXISTS exam_sessions (
    id VARCHAR(50) PRIMARY KEY,              -- SES_{hex8}
    exam_id VARCHAR(50) NOT NULL REFERENCES generated_exams(id),

    student_name VARCHAR(200) NOT NULL,
    class_level INTEGER,
    variant_number INTEGER DEFAULT 1,

    -- Answers stored as JSONB: { "task_id": "student_answer", ... }
    answers JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Scoring
    score INTEGER DEFAULT 0,
    max_score INTEGER DEFAULT 0,
    answered_count INTEGER DEFAULT 0,
    correct_count INTEGER DEFAULT 0,

    status VARCHAR(20) DEFAULT 'in_progress',  -- in_progress, submitted
    started_at TIMESTAMP DEFAULT NOW(),
    submitted_at TIMESTAMP,
    time_spent_seconds INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_exam_sessions_exam ON exam_sessions(exam_id);
CREATE INDEX IF NOT EXISTS ix_exam_sessions_status ON exam_sessions(status);


COMMIT;
