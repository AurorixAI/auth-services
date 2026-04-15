-- ============================================================================
-- ALGO V1 - Migration 007: Organization-Based Auth + Student Analytics
-- B2B модель: организация → сотрудники (owner/admin/teacher) → ученики (без логина)
-- ============================================================================

BEGIN;

-- ============================================================================
-- 1. ORGANIZATIONS — Организации
-- ============================================================================
CREATE TABLE IF NOT EXISTS organizations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(300) NOT NULL,
    org_type        VARCHAR(20) NOT NULL DEFAULT 'center',  -- center, school, university, tutor
    city            VARCHAR(200),
    address         TEXT,
    phone           VARCHAR(30),
    email           VARCHAR(255),
    logo_url        VARCHAR(500),
    
    -- Статус заявки
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending, active, suspended
    approved_by     UUID,           -- superadmin who approved
    approved_at     TIMESTAMP,
    rejection_reason TEXT,
    
    -- Настройки
    settings        JSONB DEFAULT '{}',
    max_students    INTEGER DEFAULT 500,
    max_teachers    INTEGER DEFAULT 50,
    
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT chk_org_type CHECK (org_type IN ('center', 'school', 'university', 'tutor')),
    CONSTRAINT chk_org_status CHECK (status IN ('pending', 'active', 'suspended'))
);

CREATE INDEX IF NOT EXISTS idx_org_status ON organizations(status);
CREATE INDEX IF NOT EXISTS idx_org_type ON organizations(org_type);

-- ============================================================================
-- 2. ALTER USERS — добавить org привязку, обновить роли
-- ============================================================================
ALTER TABLE users ADD COLUMN IF NOT EXISTS default_org_id UUID REFERENCES organizations(id);

-- Обновляем CHECK constraint для ролей (убираем student, добавляем owner)
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check 
    CHECK (role IN ('owner', 'admin', 'teacher', 'superadmin'));

-- Индекс
CREATE INDEX IF NOT EXISTS idx_users_org ON users(default_org_id);

-- ============================================================================
-- 3. ORGANIZATION_MEMBERS — Участники организации
-- ============================================================================
CREATE TABLE IF NOT EXISTS organization_members (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_role    VARCHAR(20) NOT NULL DEFAULT 'teacher',  -- owner, admin, teacher
    is_active   BOOLEAN DEFAULT TRUE,
    joined_at   TIMESTAMP DEFAULT NOW(),
    invited_by  UUID REFERENCES users(id),
    
    CONSTRAINT chk_org_role CHECK (org_role IN ('owner', 'admin', 'teacher')),
    CONSTRAINT uq_org_member UNIQUE (org_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_orgmem_org ON organization_members(org_id);
CREATE INDEX IF NOT EXISTS idx_orgmem_user ON organization_members(user_id);

-- ============================================================================
-- 4. INVITATIONS — Приглашения в организацию
-- ============================================================================
CREATE TABLE IF NOT EXISTS invitations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    invited_by  UUID NOT NULL REFERENCES users(id),
    email       VARCHAR(255),
    org_role    VARCHAR(20) NOT NULL DEFAULT 'teacher',
    code        VARCHAR(20) NOT NULL UNIQUE,
    status      VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending, accepted, expired, cancelled
    expires_at  TIMESTAMP NOT NULL,
    accepted_at TIMESTAMP,
    accepted_by UUID REFERENCES users(id),
    created_at  TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT chk_invite_role CHECK (org_role IN ('admin', 'teacher')),
    CONSTRAINT chk_invite_status CHECK (status IN ('pending', 'accepted', 'expired', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_invite_code ON invitations(code);
CREATE INDEX IF NOT EXISTS idx_invite_org ON invitations(org_id);

-- ============================================================================
-- 5. STUDENTS — Ученики (БЕЗ логина, запись создаётся администратором)
-- ============================================================================
CREATE TABLE IF NOT EXISTS students (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    
    -- Основные данные (вводит админ при приёме)
    first_name      VARCHAR(100) NOT NULL,
    last_name       VARCHAR(100) NOT NULL,
    class_level     INTEGER NOT NULL,           -- 5-11, класс = предполагаемый уровень
    
    -- Дополнительные данные (вводятся позже, после диагностики)
    group_id        UUID,                       -- FK → groups, назначается после диагностики
    student_number  VARCHAR(30),                -- номер/ID в группе
    parent_phone    VARCHAR(30),
    comment         TEXT,                       -- цель: поступить в ВУЗ, подтянуть и т.д.
    
    -- Статус жизненного цикла
    status          VARCHAR(20) NOT NULL DEFAULT 'new',  -- new, diagnosed, active, archived
    
    -- Метаданные
    created_by      UUID REFERENCES users(id),
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT chk_student_class CHECK (class_level >= 1 AND class_level <= 11),
    CONSTRAINT chk_student_status CHECK (status IN ('new', 'diagnosed', 'active', 'archived'))
);

CREATE INDEX IF NOT EXISTS idx_students_org ON students(org_id);
CREATE INDEX IF NOT EXISTS idx_students_group ON students(group_id);
CREATE INDEX IF NOT EXISTS idx_students_status ON students(status);
CREATE INDEX IF NOT EXISTS idx_students_class ON students(class_level);

-- ============================================================================
-- 6. GROUPS — Учебные группы
-- ============================================================================
CREATE TABLE IF NOT EXISTS groups (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name        VARCHAR(200) NOT NULL,          -- "5А", "Алгебра-продвинутые"
    class_level INTEGER,                        -- уровень группы
    teacher_id  UUID REFERENCES users(id),
    description TEXT,
    schedule    JSONB,                          -- расписание
    status      VARCHAR(20) DEFAULT 'active',   -- active, archived
    max_students INTEGER DEFAULT 30,
    
    created_at  TIMESTAMP DEFAULT NOW(),
    updated_at  TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT chk_group_status CHECK (status IN ('active', 'archived'))
);

CREATE INDEX IF NOT EXISTS idx_groups_org ON groups(org_id);
CREATE INDEX IF NOT EXISTS idx_groups_teacher ON groups(teacher_id);

-- FK students → groups
ALTER TABLE students ADD CONSTRAINT fk_student_group 
    FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE SET NULL;

-- ============================================================================
-- 7. STUDENT_GROUPS — M2M: ученик может быть в нескольких группах
-- ============================================================================
CREATE TABLE IF NOT EXISTS student_groups (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id  UUID NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    group_id    UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    is_active   BOOLEAN DEFAULT TRUE,
    joined_at   TIMESTAMP DEFAULT NOW(),
    left_at     TIMESTAMP,
    
    CONSTRAINT uq_student_group UNIQUE (student_id, group_id)
);

CREATE INDEX IF NOT EXISTS idx_sg_student ON student_groups(student_id);
CREATE INDEX IF NOT EXISTS idx_sg_group ON student_groups(group_id);

-- ============================================================================
-- 8. DIAGNOSTIC_INVITES — QR/ссылки на диагностику (Модуль 1)
-- ============================================================================
CREATE TABLE IF NOT EXISTS diagnostic_invites (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    student_id      UUID NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    created_by      UUID NOT NULL REFERENCES users(id),
    
    -- Код для QR/ссылки
    code            VARCHAR(10) NOT NULL UNIQUE,  -- короткий код: A7K2M9
    
    -- Параметры диагностики
    class_level     INTEGER NOT NULL,
    diagnostic_type VARCHAR(20) DEFAULT 'initial', -- initial, recheck, pre_exam
    
    -- Статус
    status          VARCHAR(20) NOT NULL DEFAULT 'pending', -- pending, confirmed, in_progress, completed, expired
    
    -- Связь с CAT-сессией (заполняется при старте)
    cat_session_id  UUID,
    
    -- Подтверждение ученика
    confirmed_at    TIMESTAMP,
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP,
    
    expires_at      TIMESTAMP NOT NULL,
    created_at      TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT chk_diag_type CHECK (diagnostic_type IN ('initial', 'recheck', 'pre_exam')),
    CONSTRAINT chk_diag_status CHECK (status IN ('pending', 'confirmed', 'in_progress', 'completed', 'expired'))
);

CREATE INDEX IF NOT EXISTS idx_diag_code ON diagnostic_invites(code);
CREATE INDEX IF NOT EXISTS idx_diag_org ON diagnostic_invites(org_id);
CREATE INDEX IF NOT EXISTS idx_diag_student ON diagnostic_invites(student_id);
CREATE INDEX IF NOT EXISTS idx_diag_status ON diagnostic_invites(status);

-- ============================================================================
-- 9. STUDENT_DIAGNOSTIC_RESULTS — Результаты диагностик + ошибки
-- ============================================================================
CREATE TABLE IF NOT EXISTS student_diagnostic_results (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id          UUID NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    org_id              UUID NOT NULL REFERENCES organizations(id),
    invite_id           UUID REFERENCES diagnostic_invites(id),
    session_id          UUID,           -- связь с session_summaries
    
    -- Тип
    diagnostic_type     VARCHAR(20) NOT NULL DEFAULT 'initial',
    
    -- Результаты IRT
    final_theta         DECIMAL(6,4),
    se                  DECIMAL(6,4),
    ci_low              DECIMAL(6,4),
    ci_high             DECIMAL(6,4),
    
    -- Статистика
    total_tasks         INTEGER,
    correct_answers     INTEGER,
    accuracy            DECIMAL(5,4),
    total_duration_ms   INTEGER,
    
    -- Аналитика (массивы skill_id)
    identified_gaps     JSONB DEFAULT '[]',     -- пробелы
    root_gaps           JSONB DEFAULT '[]',     -- корневые причины
    recommended_path    JSONB DEFAULT '[]',     -- что изучать (порядок)
    
    -- Ошибки по типам (агрегация)
    error_summary       JSONB DEFAULT '{}',     -- {sign_error: 3, formula_confusion: 2, ...}
    
    -- Integrity
    integrity_report    JSONB DEFAULT '{}',
    
    -- Рекомендованный уровень/группа (предложение системы)
    recommended_level   VARCHAR(5),             -- A, B, C, D
    recommended_group_id UUID REFERENCES groups(id),
    
    diagnosed_at        TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT chk_result_type CHECK (diagnostic_type IN ('initial', 'recheck', 'pre_exam', 'exam'))
);

CREATE INDEX IF NOT EXISTS idx_diagres_student ON student_diagnostic_results(student_id);
CREATE INDEX IF NOT EXISTS idx_diagres_org ON student_diagnostic_results(org_id);
CREATE INDEX IF NOT EXISTS idx_diagres_date ON student_diagnostic_results(diagnosed_at);

-- ============================================================================
-- 10. ALTER existing analytics tables — привязка к students
-- ============================================================================

-- answer_events: добавить org_id
ALTER TABLE answer_events ADD COLUMN IF NOT EXISTS org_id UUID REFERENCES organizations(id);
CREATE INDEX IF NOT EXISTS idx_ae_org ON answer_events(org_id);

-- session_summaries: добавить org_id + integrity
ALTER TABLE session_summaries ADD COLUMN IF NOT EXISTS org_id UUID REFERENCES organizations(id);
ALTER TABLE session_summaries ADD COLUMN IF NOT EXISTS integrity_report JSONB;
ALTER TABLE session_summaries ADD COLUMN IF NOT EXISTS error_patterns JSONB;
CREATE INDEX IF NOT EXISTS idx_ss_org ON session_summaries(org_id);

-- student_skill_states: добавить org_id
ALTER TABLE student_skill_states ADD COLUMN IF NOT EXISTS org_id UUID REFERENCES organizations(id);
CREATE INDEX IF NOT EXISTS idx_sss_org ON student_skill_states(org_id);

-- ============================================================================
-- 11. VIEWS — Полезные представления для аналитики
-- ============================================================================

-- Обзор организации
CREATE OR REPLACE VIEW v_org_overview AS
SELECT 
    o.id AS org_id,
    o.name AS org_name,
    o.status,
    COUNT(DISTINCT s.id) AS total_students,
    COUNT(DISTINCT s.id) FILTER (WHERE s.status = 'active') AS active_students,
    COUNT(DISTINCT g.id) FILTER (WHERE g.status = 'active') AS active_groups,
    COUNT(DISTINCT om.user_id) FILTER (WHERE om.org_role = 'teacher') AS teacher_count,
    AVG(sdr.final_theta) AS avg_theta
FROM organizations o
LEFT JOIN students s ON s.org_id = o.id
LEFT JOIN groups g ON g.org_id = o.id
LEFT JOIN organization_members om ON om.org_id = o.id AND om.is_active = TRUE
LEFT JOIN student_diagnostic_results sdr ON sdr.org_id = o.id
GROUP BY o.id, o.name, o.status;

-- Обзор группы для учителя
CREATE OR REPLACE VIEW v_group_overview AS
SELECT
    g.id AS group_id,
    g.name AS group_name,
    g.class_level,
    g.org_id,
    g.teacher_id,
    COUNT(DISTINCT sg.student_id) FILTER (WHERE sg.is_active) AS student_count,
    AVG(sss.mastery_probability) AS avg_mastery,
    AVG(sdr.final_theta) AS avg_theta,
    COUNT(DISTINCT sdr.id) AS total_diagnostics
FROM groups g
LEFT JOIN student_groups sg ON sg.group_id = g.id
LEFT JOIN student_skill_states sss ON sss.student_id = sg.student_id
LEFT JOIN student_diagnostic_results sdr ON sdr.student_id = sg.student_id
WHERE g.status = 'active'
GROUP BY g.id, g.name, g.class_level, g.org_id, g.teacher_id;

-- Наплыв учеников по дням
CREATE OR REPLACE VIEW v_enrollment_daily AS
SELECT
    org_id,
    DATE(created_at) AS day,
    COUNT(*) AS new_students,
    COUNT(*) FILTER (WHERE status = 'diagnosed') AS diagnosed,
    COUNT(*) FILTER (WHERE status = 'active') AS placed_in_group
FROM students
GROUP BY org_id, DATE(created_at);

COMMIT;
