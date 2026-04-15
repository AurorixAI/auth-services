-- ============================================================================
-- ALGO V1 - ANALYTICS SCHEMA (Самообучающийся граф)
-- Сбор данных для динамического обновления связей между навыками
-- ============================================================================

-- ============================================================================
-- 1. ANSWER_EVENTS - Полная история ответов
-- ============================================================================
-- Каждый ответ ученика сохраняется для обучения системы

CREATE TABLE answer_events (
    id BIGSERIAL PRIMARY KEY,
    
    -- Идентификация
    session_id UUID NOT NULL,              -- ID сессии диагностики
    student_id UUID,                       -- NULL для анонимных
    
    -- Что решали
    task_id VARCHAR(30) NOT NULL,
    skill_id VARCHAR(30) NOT NULL,
    
    -- Результат
    is_correct BOOLEAN NOT NULL,
    student_answer TEXT,                   -- Что ответил ученик
    correct_answer TEXT,                   -- Правильный ответ
    
    -- Временные метрики
    time_spent_ms INTEGER NOT NULL,        -- Время в миллисекундах
    started_at TIMESTAMP NOT NULL,
    submitted_at TIMESTAMP NOT NULL DEFAULT NOW(),
    
    -- Контекст
    difficulty_level VARCHAR(1),           -- A/B/C
    task_order_in_session INTEGER,         -- Порядковый номер в сессии
    
    -- IRT параметры задачи на момент ответа
    task_irt_a DECIMAL(6,4),              -- Дискриминация
    task_irt_b DECIMAL(6,4),              -- Сложность
    task_irt_c DECIMAL(6,4),              -- Угадывание
    
    -- Оценка способности ученика ДО этого ответа
    student_theta_before DECIMAL(6,4),
    student_theta_after DECIMAL(6,4),
    
    -- Метаданные
    device_type VARCHAR(20),               -- mobile/tablet/desktop
    client_version VARCHAR(20),
    
    FOREIGN KEY (task_id) REFERENCES tasks_master(id),
    FOREIGN KEY (skill_id) REFERENCES knowledge_hierarchy(id)
);

-- Индексы для аналитики
CREATE INDEX idx_ae_session ON answer_events(session_id);
CREATE INDEX idx_ae_student ON answer_events(student_id);
CREATE INDEX idx_ae_skill ON answer_events(skill_id);
CREATE INDEX idx_ae_submitted ON answer_events(submitted_at);
CREATE INDEX idx_ae_correct ON answer_events(is_correct);
CREATE INDEX idx_ae_skill_correct ON answer_events(skill_id, is_correct);

-- Партиционирование по дате (для масштабирования)
-- CREATE TABLE answer_events_2024 PARTITION OF answer_events 
--     FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');

-- ============================================================================
-- 2. SKILL_CORRELATIONS - Обнаруженные корреляции между навыками
-- ============================================================================
-- Статистически значимые связи, обнаруженные ML

CREATE TABLE skill_correlations (
    id SERIAL PRIMARY KEY,
    
    -- Пара навыков
    skill_a_id VARCHAR(30) NOT NULL,
    skill_b_id VARCHAR(30) NOT NULL,
    
    -- Статистика
    correlation_coefficient DECIMAL(6,4) NOT NULL,  -- От -1 до 1
    p_value DECIMAL(10,8),                          -- Статистическая значимость
    sample_size INTEGER NOT NULL,                   -- На скольких учениках
    
    -- Тип связи
    relationship_type VARCHAR(20) NOT NULL CHECK (relationship_type IN (
        'prerequisite',   -- A предшествует B (направленная)
        'corequisite',    -- A и B связаны (ненаправленная)
        'diagnostic',     -- Ошибка в A предсказывает ошибку в B
        'transfer'        -- Знание A помогает в B
    )),
    
    -- Направление (для prerequisite и transfer)
    direction VARCHAR(10) CHECK (direction IN ('a_to_b', 'b_to_a', 'bidirectional')),
    
    -- Сила связи
    strength DECIMAL(4,2) CHECK (strength BETWEEN 0 AND 10),
    confidence DECIMAL(4,3) CHECK (confidence BETWEEN 0 AND 1),
    
    -- Источник открытия
    discovery_method VARCHAR(30) NOT NULL CHECK (discovery_method IN (
        'expert_defined',      -- Заложено экспертами
        'statistical',         -- Статистический анализ
        'ml_discovered',       -- ML модель
        'pattern_mining'       -- Анализ паттернов ошибок
    )),
    
    -- Валидация
    is_validated BOOLEAN DEFAULT FALSE,
    validated_by VARCHAR(100),
    validated_at TIMESTAMP,
    
    -- Даты
    discovered_at TIMESTAMP DEFAULT NOW(),
    last_updated_at TIMESTAMP DEFAULT NOW(),
    
    FOREIGN KEY (skill_a_id) REFERENCES knowledge_hierarchy(id),
    FOREIGN KEY (skill_b_id) REFERENCES knowledge_hierarchy(id),
    
    -- Уникальность пары (порядок не важен для corequisite)
    UNIQUE(skill_a_id, skill_b_id, relationship_type)
);

CREATE INDEX idx_sc_skill_a ON skill_correlations(skill_a_id);
CREATE INDEX idx_sc_skill_b ON skill_correlations(skill_b_id);
CREATE INDEX idx_sc_correlation ON skill_correlations(correlation_coefficient);
CREATE INDEX idx_sc_strong ON skill_correlations(strength) WHERE strength >= 7;

-- ============================================================================
-- 3. ERROR_PATTERNS - Паттерны ошибок
-- ============================================================================
-- Анализ типичных ошибок для улучшения диагностики

CREATE TABLE error_patterns (
    id SERIAL PRIMARY KEY,
    
    -- К какому навыку относится
    skill_id VARCHAR(30) NOT NULL,
    
    -- Описание паттерна
    pattern_type VARCHAR(30) NOT NULL CHECK (pattern_type IN (
        'sign_error',           -- Ошибка со знаком
        'order_of_operations',  -- Порядок действий
        'formula_confusion',    -- Путаница формул
        'concept_gap',          -- Пробел в понимании концепции
        'calculation_error',    -- Вычислительная ошибка
        'careless_mistake',     -- Невнимательность
        'time_pressure',        -- Ошибка под давлением времени
        'partial_knowledge',    -- Частичное знание
        'misconception',        -- Заблуждение
        'other'
    )),
    
    pattern_description TEXT NOT NULL,
    
    -- Примеры ошибочных ответов (regex или список)
    error_examples JSONB DEFAULT '[]',
    
    -- Связь с другими навыками (какие пробелы приводят к этой ошибке)
    root_cause_skills JSONB DEFAULT '[]',  -- ["SKILL_ID_1", "SKILL_ID_2"]
    
    -- Статистика
    occurrence_count INTEGER DEFAULT 0,
    occurrence_rate DECIMAL(5,4),          -- Как часто встречается
    recovery_rate DECIMAL(5,4),            -- Как часто исправляют после подсказки
    
    -- Рекомендации
    remediation_suggestions JSONB DEFAULT '[]',
    related_tasks JSONB DEFAULT '[]',      -- Задачи для отработки
    
    -- Метаданные
    discovered_at TIMESTAMP DEFAULT NOW(),
    last_seen_at TIMESTAMP DEFAULT NOW(),
    
    FOREIGN KEY (skill_id) REFERENCES knowledge_hierarchy(id)
);

CREATE INDEX idx_ep_skill ON error_patterns(skill_id);
CREATE INDEX idx_ep_type ON error_patterns(pattern_type);
CREATE INDEX idx_ep_rate ON error_patterns(occurrence_rate DESC);

-- ============================================================================
-- 4. SKILL_RESPONSE_TIMES - Статистика времени ответов
-- ============================================================================
-- Для точной оценки "аномально быстро" / "аномально медленно"

CREATE TABLE skill_response_times (
    skill_id VARCHAR(30) PRIMARY KEY,
    difficulty_level VARCHAR(1) NOT NULL CHECK (difficulty_level IN ('A', 'B', 'C')),
    
    -- Статистика по времени (в миллисекундах)
    sample_size INTEGER DEFAULT 0,
    mean_time_ms INTEGER,
    median_time_ms INTEGER,
    std_dev_ms INTEGER,
    percentile_10 INTEGER,                 -- 10% быстрых
    percentile_25 INTEGER,
    percentile_75 INTEGER,
    percentile_90 INTEGER,                 -- 10% медленных
    percentile_99 INTEGER,
    
    -- Время для определения "слишком быстро" (угадывание)
    min_realistic_time_ms INTEGER,         -- Меньше = угадывание
    
    -- Разбивка по результату
    mean_time_correct_ms INTEGER,
    mean_time_incorrect_ms INTEGER,
    
    -- Последнее обновление
    last_calculated_at TIMESTAMP DEFAULT NOW(),
    
    FOREIGN KEY (skill_id) REFERENCES knowledge_hierarchy(id),
    
    -- Составной первичный ключ
    UNIQUE(skill_id, difficulty_level)
);

-- ============================================================================
-- 5. SESSION_SUMMARIES - Сводка по диагностическим сессиям
-- ============================================================================

CREATE TABLE session_summaries (
    session_id UUID PRIMARY KEY,
    
    -- Идентификация
    student_id UUID,
    
    -- Временные рамки
    started_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP,
    total_duration_ms INTEGER,
    
    -- Результаты
    total_tasks INTEGER DEFAULT 0,
    correct_answers INTEGER DEFAULT 0,
    
    -- Финальные оценки
    final_theta DECIMAL(6,4),              -- IRT способность
    confidence_interval_low DECIMAL(6,4),
    confidence_interval_high DECIMAL(6,4),
    
    -- Обнаруженные пробелы
    identified_gaps JSONB DEFAULT '[]',    -- Массив skill_id с проблемами
    root_gaps JSONB DEFAULT '[]',          -- Корневые пробелы (результат Deep Scan)
    
    -- Рекомендации
    recommended_skills JSONB DEFAULT '[]', -- Что изучать дальше
    priority_order JSONB DEFAULT '[]',     -- В каком порядке
    
    -- Тип сессии
    session_type VARCHAR(20) DEFAULT 'diagnostic' CHECK (session_type IN (
        'diagnostic',    -- Полная диагностика
        'quick_check',   -- Быстрая проверка
        'practice',      -- Тренировка
        'exam'           -- Экзамен
    )),
    
    -- Метаданные
    platform VARCHAR(20),
    client_version VARCHAR(20),
    
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_ss_student ON session_summaries(student_id);
CREATE INDEX idx_ss_started ON session_summaries(started_at);
CREATE INDEX idx_ss_type ON session_summaries(session_type);

-- ============================================================================
-- 6. GRAPH_UPDATES_LOG - Лог изменений графа
-- ============================================================================
-- Аудит изменений в графе знаний (для отладки и отката)

CREATE TABLE graph_updates_log (
    id BIGSERIAL PRIMARY KEY,
    
    -- Что изменилось
    update_type VARCHAR(30) NOT NULL CHECK (update_type IN (
        'new_correlation',      -- Новая связь обнаружена
        'strength_update',      -- Обновление силы связи
        'correlation_removed',  -- Связь удалена
        'prerequisite_added',   -- Добавлен prerequisite
        'prerequisite_removed', -- Удалён prerequisite
        'irt_calibration',      -- Калибровка IRT параметров
        'benchmark_update'      -- Обновление бенчмарков
    )),
    
    -- Объект изменения
    entity_type VARCHAR(20) NOT NULL,      -- skill, correlation, task
    entity_id VARCHAR(50) NOT NULL,
    
    -- Детали изменения
    old_value JSONB,
    new_value JSONB,
    change_reason TEXT,
    
    -- Источник
    triggered_by VARCHAR(30) NOT NULL CHECK (triggered_by IN (
        'ml_system',           -- Автоматический ML
        'statistical_batch',   -- Пакетный статанализ
        'admin_manual',        -- Ручное изменение админом
        'expert_review'        -- Экспертная ревизия
    )),
    
    -- Валидация
    requires_review BOOLEAN DEFAULT FALSE,
    reviewed_by VARCHAR(100),
    reviewed_at TIMESTAMP,
    review_status VARCHAR(20) CHECK (review_status IN ('pending', 'approved', 'rejected')),
    
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_gul_type ON graph_updates_log(update_type);
CREATE INDEX idx_gul_entity ON graph_updates_log(entity_type, entity_id);
CREATE INDEX idx_gul_created ON graph_updates_log(created_at);
CREATE INDEX idx_gul_review ON graph_updates_log(requires_review) WHERE requires_review = TRUE;

-- ============================================================================
-- 7. IRT_CALIBRATION_HISTORY - История калибровки IRT параметров
-- ============================================================================

CREATE TABLE irt_calibration_history (
    id SERIAL PRIMARY KEY,
    task_id VARCHAR(30) NOT NULL,
    
    -- Параметры до
    old_discrimination DECIMAL(6,4),
    old_difficulty DECIMAL(6,4),
    old_guessing DECIMAL(6,4),
    
    -- Параметры после
    new_discrimination DECIMAL(6,4),
    new_difficulty DECIMAL(6,4),
    new_guessing DECIMAL(6,4),
    
    -- Статистика калибровки
    sample_size INTEGER NOT NULL,
    fit_statistic DECIMAL(8,4),           -- Насколько хорошо модель фитится
    
    -- Метаданные
    calibration_method VARCHAR(30),       -- 'mmle', 'jmle', 'bayesian'
    calibrated_at TIMESTAMP DEFAULT NOW(),
    
    FOREIGN KEY (task_id) REFERENCES tasks_master(id)
);

CREATE INDEX idx_ich_task ON irt_calibration_history(task_id);
CREATE INDEX idx_ich_date ON irt_calibration_history(calibrated_at);

-- ============================================================================
-- 8. STUDENT_SKILL_STATES - Состояние навыков ученика
-- ============================================================================
-- Байесовское состояние знания каждого навыка

CREATE TABLE student_skill_states (
    id SERIAL PRIMARY KEY,
    student_id UUID NOT NULL,
    skill_id VARCHAR(30) NOT NULL,
    
    -- Байесовские оценки
    mastery_probability DECIMAL(5,4) NOT NULL DEFAULT 0.5,  -- P(L) - вероятность владения
    learn_rate DECIMAL(5,4) DEFAULT 0.1,                    -- P(T) - скорость обучения
    guess_rate DECIMAL(5,4) DEFAULT 0.2,                    -- P(G) - вероятность угадывания
    slip_rate DECIMAL(5,4) DEFAULT 0.1,                     -- P(S) - вероятность ошибки при знании
    
    -- IRT-совместимая оценка
    theta DECIMAL(6,4),                                     -- Способность по этому навыку
    theta_se DECIMAL(6,4),                                  -- Стандартная ошибка theta
    
    -- История
    total_attempts INTEGER DEFAULT 0,
    correct_attempts INTEGER DEFAULT 0,
    last_attempt_at TIMESTAMP,
    last_correct_at TIMESTAMP,
    
    -- Тренд
    trend VARCHAR(10) CHECK (trend IN ('improving', 'stable', 'declining', 'unknown')),
    consecutive_correct INTEGER DEFAULT 0,
    consecutive_incorrect INTEGER DEFAULT 0,
    
    -- Статус
    status VARCHAR(20) DEFAULT 'unknown' CHECK (status IN (
        'mastered',      -- >= 0.9
        'learning',      -- 0.5 - 0.9
        'struggling',    -- 0.3 - 0.5
        'gap',           -- < 0.3
        'unknown'        -- Недостаточно данных
    )),
    
    updated_at TIMESTAMP DEFAULT NOW(),
    
    FOREIGN KEY (skill_id) REFERENCES knowledge_hierarchy(id),
    
    UNIQUE(student_id, skill_id)
);

CREATE INDEX idx_sss_student ON student_skill_states(student_id);
CREATE INDEX idx_sss_skill ON student_skill_states(skill_id);
CREATE INDEX idx_sss_status ON student_skill_states(status);
CREATE INDEX idx_sss_mastery ON student_skill_states(mastery_probability);

-- ============================================================================
-- VIEWS - Полезные представления
-- ============================================================================

-- Топ корреляций для обзора
CREATE VIEW v_strong_correlations AS
SELECT 
    sc.skill_a_id,
    kh_a.name_ru AS skill_a_name,
    sc.skill_b_id,
    kh_b.name_ru AS skill_b_name,
    sc.correlation_coefficient,
    sc.relationship_type,
    sc.strength,
    sc.sample_size,
    sc.discovery_method
FROM skill_correlations sc
JOIN knowledge_hierarchy kh_a ON sc.skill_a_id = kh_a.id
JOIN knowledge_hierarchy kh_b ON sc.skill_b_id = kh_b.id
WHERE sc.correlation_coefficient > 0.5 OR sc.correlation_coefficient < -0.5
ORDER BY ABS(sc.correlation_coefficient) DESC;

-- Проблемные навыки (низкий success rate)
CREATE VIEW v_problem_skills AS
SELECT 
    kh.id,
    kh.name_ru,
    kh.level,
    sb.total_attempts,
    sb.total_correct,
    ROUND(sb.total_correct::DECIMAL / NULLIF(sb.total_attempts, 0) * 100, 2) AS success_rate,
    sb.avg_solve_time_seconds,
    array_length(sb.common_errors::text[], 1) AS error_types_count
FROM knowledge_hierarchy kh
JOIN skill_benchmarks sb ON kh.id = sb.skill_id
WHERE sb.total_attempts >= 100
  AND (sb.total_correct::DECIMAL / sb.total_attempts) < 0.5
ORDER BY (sb.total_correct::DECIMAL / sb.total_attempts) ASC;

-- ============================================================================
-- FUNCTIONS - Вспомогательные функции
-- ============================================================================

-- Функция обновления временных меток
CREATE OR REPLACE FUNCTION update_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Триггеры для автообновления
CREATE TRIGGER tr_skill_correlations_updated
    BEFORE UPDATE ON skill_correlations
    FOR EACH ROW EXECUTE FUNCTION update_timestamp();

CREATE TRIGGER tr_student_skill_states_updated
    BEFORE UPDATE ON student_skill_states
    FOR EACH ROW EXECUTE FUNCTION update_timestamp();

-- ============================================================================
-- РАСШИРЕНИЕ skill_prerequisites ДЛЯ САМООБУЧЕНИЯ
-- ============================================================================

-- Добавляем колонки для ML-обнаруженных связей
ALTER TABLE skill_prerequisites ADD COLUMN IF NOT EXISTS 
    weight DECIMAL(4,2) DEFAULT 1.0;  -- Начальный вес от эксперта

ALTER TABLE skill_prerequisites ADD COLUMN IF NOT EXISTS 
    learned_weight DECIMAL(4,2);  -- Вес, выученный из данных

ALTER TABLE skill_prerequisites ADD COLUMN IF NOT EXISTS 
    confidence DECIMAL(4,3) DEFAULT 1.0;  -- Уверенность в связи

ALTER TABLE skill_prerequisites ADD COLUMN IF NOT EXISTS 
    sample_size INTEGER DEFAULT 0;  -- Сколько данных подтверждает

ALTER TABLE skill_prerequisites ADD COLUMN IF NOT EXISTS 
    discovery_source VARCHAR(20) DEFAULT 'expert' 
    CHECK (discovery_source IN ('expert', 'statistical', 'ml'));

ALTER TABLE skill_prerequisites ADD COLUMN IF NOT EXISTS 
    last_validated_at TIMESTAMP;

-- Индекс для быстрого поиска ML-связей
CREATE INDEX IF NOT EXISTS idx_sp_discovery ON skill_prerequisites(discovery_source);
CREATE INDEX IF NOT EXISTS idx_sp_confidence ON skill_prerequisites(confidence DESC);

-- ============================================================================
-- COMMENTS - Документация
-- ============================================================================

COMMENT ON TABLE answer_events IS 'Полный лог ответов учеников для ML-обучения системы';
COMMENT ON TABLE skill_correlations IS 'Статистически обнаруженные связи между навыками';
COMMENT ON TABLE error_patterns IS 'Паттерны типичных ошибок для улучшения диагностики';
COMMENT ON TABLE skill_response_times IS 'Статистика времени ответов для определения угадывания';
COMMENT ON TABLE session_summaries IS 'Сводка по диагностическим сессиям';
COMMENT ON TABLE graph_updates_log IS 'Аудит всех изменений в графе знаний';
COMMENT ON TABLE student_skill_states IS 'Байесовское состояние владения навыками ученика';
