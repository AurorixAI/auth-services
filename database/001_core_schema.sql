-- ============================================================================
-- ALGO V1 - CORE SCHEMA (Ядро диагностики)
-- Гибридная архитектура: Фиксированная L1-L4 иерархия для точной диагностики
-- ============================================================================

-- Расширения
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";  -- Для поиска по тексту

-- ============================================================================
-- 1. KNOWLEDGE_HIERARCHY - Иерархия знаний (L1-L4)
-- ============================================================================
-- L1: Domain (Алгебра, Геометрия, Арифметика)
-- L2: Cluster (Уравнения, Неравенства, Функции)
-- L3: Topic (Линейные уравнения, Квадратные уравнения)
-- L4: Atomic Skill (Вычисление дискриминанта, Перенос слагаемых)

CREATE TABLE knowledge_hierarchy (
    id VARCHAR(30) PRIMARY KEY,  -- Например: ALG_EQ_QUAD_DISCRIM
    level VARCHAR(2) NOT NULL CHECK (level IN ('L1', 'L2', 'L3', 'L4')),
    parent_id VARCHAR(30),
    
    -- Названия
    name VARCHAR(200) NOT NULL,      -- Английское название
    name_ru VARCHAR(200) NOT NULL,   -- Русское название
    name_uz VARCHAR(200),            -- Узбекское название
    
    -- Метаданные
    description TEXT,
    class_level_start INTEGER,       -- С какого класса (5)
    class_level_end INTEGER,         -- До какого класса (11)
    importance INTEGER DEFAULT 5 CHECK (importance BETWEEN 1 AND 10),
    sequence_order INTEGER DEFAULT 0, -- Порядок в рамках родителя
    
    -- Атомарность (только для L4)
    is_composite BOOLEAN DEFAULT FALSE,
    composite_type VARCHAR(20) CHECK (composite_type IN ('sequential', 'parallel', 'conditional')),
    decomposition_notes TEXT,
    
    -- Когнитивные характеристики (только для L4)
    cognitive_type VARCHAR(20) CHECK (cognitive_type IN ('recall', 'apply', 'analyze', 'evaluate', 'create')),
    typical_time_seconds INTEGER,     -- Типичное время решения
    
    -- Статус
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    
    FOREIGN KEY (parent_id) REFERENCES knowledge_hierarchy(id)
);

-- Индексы для быстрого поиска
CREATE INDEX idx_kh_level ON knowledge_hierarchy(level);
CREATE INDEX idx_kh_parent ON knowledge_hierarchy(parent_id);
CREATE INDEX idx_kh_class_level ON knowledge_hierarchy(class_level_start, class_level_end);
CREATE INDEX idx_kh_importance ON knowledge_hierarchy(importance DESC);

-- ============================================================================
-- 2. SKILL_PREREQUISITES - Граф зависимостей навыков
-- ============================================================================
-- Критически важно для Deep Scan и выявления корневых пробелов

CREATE TABLE skill_prerequisites (
    id SERIAL PRIMARY KEY,
    skill_id VARCHAR(30) NOT NULL,
    prerequisite_id VARCHAR(30) NOT NULL,
    
    -- Тип зависимости
    dependency_type VARCHAR(10) NOT NULL CHECK (dependency_type IN ('hard', 'soft')),
    -- hard: Без этого навыка НЕВОЗМОЖНО освоить целевой
    -- soft: Помогает, но не критично
    
    -- Критичность (1-10)
    criticality INTEGER DEFAULT 5 CHECK (criticality BETWEEN 1 AND 10),
    
    -- Описание связи
    relationship_description TEXT,
    
    created_at TIMESTAMP DEFAULT NOW(),
    
    FOREIGN KEY (skill_id) REFERENCES knowledge_hierarchy(id),
    FOREIGN KEY (prerequisite_id) REFERENCES knowledge_hierarchy(id),
    
    -- Уникальность связи
    UNIQUE(skill_id, prerequisite_id),
    
    -- Проверка: нельзя ссылаться на себя
    CHECK (skill_id != prerequisite_id)
);

CREATE INDEX idx_sp_skill ON skill_prerequisites(skill_id);
CREATE INDEX idx_sp_prereq ON skill_prerequisites(prerequisite_id);
CREATE INDEX idx_sp_hard ON skill_prerequisites(dependency_type) WHERE dependency_type = 'hard';

-- ============================================================================
-- 3. SKILL_BENCHMARKS - Статистика навыков
-- ============================================================================
-- Для автокалибровки сложности и оценки времени

CREATE TABLE skill_benchmarks (
    skill_id VARCHAR(30) PRIMARY KEY,
    
    -- Общая статистика
    total_attempts INTEGER DEFAULT 0,
    total_correct INTEGER DEFAULT 0,
    avg_solve_time_seconds DECIMAL(10,2),
    
    -- Статистика по классам (процент успеха)
    success_rate_grade_5 DECIMAL(5,2),
    success_rate_grade_6 DECIMAL(5,2),
    success_rate_grade_7 DECIMAL(5,2),
    success_rate_grade_8 DECIMAL(5,2),
    success_rate_grade_9 DECIMAL(5,2),
    success_rate_grade_10 DECIMAL(5,2),
    success_rate_grade_11 DECIMAL(5,2),
    
    -- Статистика по сложности
    success_rate_level_a DECIMAL(5,2),  -- Базовый уровень
    success_rate_level_b DECIMAL(5,2),  -- Средний уровень
    success_rate_level_c DECIMAL(5,2),  -- Продвинутый уровень
    
    -- Типичные ошибки (JSON массив)
    common_errors JSONB DEFAULT '[]',
    
    -- Последнее обновление
    last_calculated_at TIMESTAMP DEFAULT NOW(),
    
    FOREIGN KEY (skill_id) REFERENCES knowledge_hierarchy(id)
);

-- ============================================================================
-- 4. TASKS_MASTER - Банк задач
-- ============================================================================
-- Центральное хранилище всех задач

CREATE TABLE tasks_master (
    id VARCHAR(30) PRIMARY KEY,  -- Например: TASK_QUAD_001
    
    -- Привязка к навыку
    skill_id VARCHAR(30) NOT NULL,
    
    -- Контент задачи
    question_text TEXT NOT NULL,           -- Текст вопроса
    question_latex TEXT,                   -- LaTeX формула
    question_image_url TEXT,               -- URL картинки (если есть)
    
    -- Правильный ответ
    answer_type VARCHAR(20) NOT NULL CHECK (answer_type IN (
        'exact_number',      -- Точное число (42)
        'decimal',           -- Десятичная дробь (3.14)
        'fraction',          -- Дробь (3/4)
        'multiple_values',   -- Несколько значений (x1=-2, x2=3)
        'expression',        -- Выражение (2x+3)
        'multiple_choice',   -- Выбор из вариантов
        'true_false',        -- Да/Нет
        'open_text'          -- Свободный ответ
    )),
    correct_answer TEXT NOT NULL,          -- Правильный ответ
    correct_answer_latex TEXT,             -- LaTeX ответа
    answer_tolerance DECIMAL(10,6),        -- Допустимая погрешность
    
    -- Варианты (для multiple_choice)
    answer_options JSONB,  -- ["вариант1", "вариант2", ...]
    
    -- Решение
    solution_steps JSONB,  -- Пошаговое решение
    solution_explanation TEXT,
    
    -- Сложность
    difficulty VARCHAR(1) NOT NULL CHECK (difficulty IN ('A', 'B', 'C')),
    -- A: Прямое применение, простые числа
    -- B: Требуется преобразование, средние числа
    -- C: Нестандартная формулировка, параметры
    
    difficulty_score DECIMAL(3,2),  -- 0.00-1.00 (точнее)
    cognitive_load VARCHAR(20),     -- recall, apply, analyze
    
    -- Источник
    source_type VARCHAR(20) CHECK (source_type IN ('textbook', 'exam', 'generated', 'custom')),
    source_reference TEXT,           -- "Виленкин 5 класс, стр. 87, №245а"
    
    -- Верификация
    verification_status VARCHAR(20) DEFAULT 'pending' 
        CHECK (verification_status IN ('pending', 'verified', 'conflict', 'rejected')),
    verified_by VARCHAR(100),
    verified_at TIMESTAMP,
    sympy_solution TEXT,             -- Решение через SymPy
    
    -- Использование
    times_used INTEGER DEFAULT 0,
    times_correct INTEGER DEFAULT 0,
    avg_time_seconds DECIMAL(10,2),
    
    -- Статус
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    
    FOREIGN KEY (skill_id) REFERENCES knowledge_hierarchy(id)
);

-- Индексы
CREATE INDEX idx_tm_skill ON tasks_master(skill_id);
CREATE INDEX idx_tm_difficulty ON tasks_master(difficulty);
CREATE INDEX idx_tm_verified ON tasks_master(verification_status);
CREATE INDEX idx_tm_active ON tasks_master(is_active) WHERE is_active = TRUE;
CREATE INDEX idx_tm_usage ON tasks_master(times_used ASC);

-- Полнотекстовый поиск
CREATE INDEX idx_tm_question_text ON tasks_master USING gin(to_tsvector('russian', question_text));

-- ============================================================================
-- 5. TASK_TEMPLATES - Шаблоны для генерации вариантов
-- ============================================================================
-- Параметрические шаблоны для создания бесконечных вариантов

CREATE TABLE task_templates (
    id VARCHAR(30) PRIMARY KEY,
    original_task_id VARCHAR(30) NOT NULL,
    skill_id VARCHAR(30) NOT NULL,
    
    -- Шаблон
    template_text TEXT NOT NULL,        -- "Решите уравнение {{a}}x + {{b}} = {{c}}"
    template_latex TEXT,                -- LaTeX с переменными
    
    -- Параметры
    parameters JSONB NOT NULL,          -- ["a", "b", "c"]
    constraints JSONB NOT NULL,         -- {"a": {"min": 1, "max": 10, "type": "int"}}
    
    -- Формула ответа
    answer_formula TEXT NOT NULL,       -- "(c - b) / a"
    
    -- Метод генерации
    generation_method VARCHAR(20) CHECK (generation_method IN (
        'simple_substitution',  -- Простая подстановка
        'constrained',          -- С ограничениями
        'sympy_verified'        -- Проверено SymPy
    )),
    
    -- Качество
    quality_score DECIMAL(3,2),         -- 0.00-1.00
    tested_combinations INTEGER DEFAULT 0,
    successful_combinations INTEGER DEFAULT 0,
    
    -- Статус
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    
    FOREIGN KEY (original_task_id) REFERENCES tasks_master(id),
    FOREIGN KEY (skill_id) REFERENCES knowledge_hierarchy(id)
);

CREATE INDEX idx_tt_skill ON task_templates(skill_id);
CREATE INDEX idx_tt_quality ON task_templates(quality_score DESC);

-- ============================================================================
-- 6. ВСПОМОГАТЕЛЬНЫЕ ТАБЛИЦЫ
-- ============================================================================

-- Теги для задач
CREATE TABLE task_tags (
    task_id VARCHAR(30) NOT NULL,
    tag VARCHAR(50) NOT NULL,
    
    PRIMARY KEY (task_id, tag),
    FOREIGN KEY (task_id) REFERENCES tasks_master(id)
);

CREATE INDEX idx_tags ON task_tags(tag);

-- Лог изменений
CREATE TABLE audit_log (
    id SERIAL PRIMARY KEY,
    table_name VARCHAR(50) NOT NULL,
    record_id VARCHAR(50) NOT NULL,
    action VARCHAR(20) NOT NULL,  -- INSERT, UPDATE, DELETE
    old_data JSONB,
    new_data JSONB,
    changed_by VARCHAR(100),
    changed_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_audit_table ON audit_log(table_name, record_id);
CREATE INDEX idx_audit_time ON audit_log(changed_at DESC);

-- ============================================================================
-- ТРИГГЕРЫ
-- ============================================================================

-- Автообновление updated_at
CREATE OR REPLACE FUNCTION update_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tr_kh_updated 
    BEFORE UPDATE ON knowledge_hierarchy 
    FOR EACH ROW EXECUTE FUNCTION update_timestamp();

CREATE TRIGGER tr_tm_updated 
    BEFORE UPDATE ON tasks_master 
    FOR EACH ROW EXECUTE FUNCTION update_timestamp();

-- ============================================================================
-- НАЧАЛЬНЫЕ ДАННЫЕ
-- ============================================================================
-- Стейл seed-данные (ARITH/ALG/GEOM) удалены 22.02.2026.
-- Актуальные данные загружаются из:
--   database/skeletons/grade_05.sql (L1-L4 иерархия, 142 скилла, 40 задач)
--   database/skeletons/grade_06.sql (52 скилла, 8 задач)
--   database/dev/013_grade5_full_synthetic.sql (994 задачи, dev only)
-- ============================================================================

COMMIT;
