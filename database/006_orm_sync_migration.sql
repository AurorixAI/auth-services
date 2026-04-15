-- ============================================================================
-- ALGO V1 - Migration 006: Sync ORM ↔ SQL
-- Добавляет недостающие колонки и фиксит расхождения
-- ============================================================================

-- 1. tasks_master: добавляем IRT колонки (ORM уже имеет, SQL нет)
ALTER TABLE tasks_master
ADD COLUMN IF NOT EXISTS irt_discrimination DECIMAL(6,4) DEFAULT 1.0,
ADD COLUMN IF NOT EXISTS irt_difficulty DECIMAL(6,4) DEFAULT 0.0,
ADD COLUMN IF NOT EXISTS irt_guessing DECIMAL(6,4) DEFAULT 0.0,
ADD COLUMN IF NOT EXISTS irt_calibrated BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS irt_sample_size INTEGER DEFAULT 0;

-- 2. tasks_master: добавляем шаблонизацию (ORM уже имеет)
ALTER TABLE tasks_master
ADD COLUMN IF NOT EXISTS has_template BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS template_params JSONB,
ADD COLUMN IF NOT EXISTS generation_function TEXT;

-- 3. tasks_master: добавляем подсказки и типичные ошибки
ALTER TABLE tasks_master
ADD COLUMN IF NOT EXISTS hints JSONB,
ADD COLUMN IF NOT EXISTS common_mistakes JSONB;

-- 4. skill_prerequisites: добавляем weight и discovery_source
-- (003_analytics_schema.sql уже имеет ALTER, но на всякий случай)
ALTER TABLE skill_prerequisites
ADD COLUMN IF NOT EXISTS weight DECIMAL(4,2) DEFAULT 1.0,
ADD COLUMN IF NOT EXISTS learned_weight DECIMAL(4,2),
ADD COLUMN IF NOT EXISTS discovery_source VARCHAR(20) DEFAULT 'expert';

-- 5. knowledge_hierarchy: добавляем is_composite поля
ALTER TABLE knowledge_hierarchy
ADD COLUMN IF NOT EXISTS is_composite BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS composite_type VARCHAR(20),
ADD COLUMN IF NOT EXISTS decomposition_notes TEXT;

-- 6. Индексы для IRT
CREATE INDEX IF NOT EXISTS idx_tm_irt_calibrated ON tasks_master(irt_calibrated);
CREATE INDEX IF NOT EXISTS idx_tm_irt_difficulty ON tasks_master(irt_difficulty);

-- 7. textbook_structure: добавляем estimated_hours
ALTER TABLE textbook_structure
ADD COLUMN IF NOT EXISTS estimated_hours DECIMAL(4,2);

-- 8. Обновляем CHECK constraint для verification_status
-- (добавляем 'needs_review' и 'conflict')
ALTER TABLE tasks_master DROP CONSTRAINT IF EXISTS tasks_master_verification_status_check;
ALTER TABLE tasks_master ADD CONSTRAINT tasks_master_verification_status_check 
    CHECK (verification_status IN ('pending', 'verified', 'conflict', 'rejected', 'needs_review'));

-- 9. Переименовываем cognitive_load из VARCHAR в соответствие (если нужно)
-- SQL уже имеет VARCHAR(20), ORM теперь тоже VARCHAR(20) — OK

-- 10. Добавляем digitization_log если нет
CREATE TABLE IF NOT EXISTS digitization_log (
    id SERIAL PRIMARY KEY,
    textbook_id UUID REFERENCES textbooks(textbook_id),
    page_number INTEGER,
    status VARCHAR(20) DEFAULT 'pending',
    ocr_confidence DECIMAL(3,2),
    tasks_found INTEGER DEFAULT 0,
    tasks_approved INTEGER DEFAULT 0,
    error_message TEXT,
    processed_at TIMESTAMP DEFAULT NOW(),
    processing_time_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_dlog_textbook ON digitization_log(textbook_id);
CREATE INDEX IF NOT EXISTS idx_dlog_status ON digitization_log(status);

-- 11. Добавляем таблицу для дистракторов
CREATE TABLE IF NOT EXISTS task_distractors (
    id SERIAL PRIMARY KEY,
    task_id VARCHAR(30) NOT NULL REFERENCES tasks_master(id) ON DELETE CASCADE,
    distractor_value TEXT NOT NULL,
    error_type VARCHAR(50),
    explanation TEXT,
    plausibility DECIMAL(3,2) DEFAULT 0.5,
    times_selected INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_td_task ON task_distractors(task_id);

-- Done
SELECT 'Migration 006 complete' AS status;
