-- ============================================================================
-- ALGO V1 - PRESENTATION SCHEMA (Слой учебников)
-- Гибкий маппинг учебников на единое ядро L4-навыков
-- ============================================================================

-- ============================================================================
-- 1. TEXTBOOKS - Каталог учебников
-- ============================================================================

CREATE TABLE textbooks (
    textbook_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    
    -- Идентификация
    isbn VARCHAR(20),
    title VARCHAR(300) NOT NULL,           -- "Математика 5 класс"
    subtitle VARCHAR(300),                 -- "Учебник для общеобразовательных учреждений"
    edition VARCHAR(50),                   -- "2023"
    
    -- Авторы
    authors TEXT[],                        -- ["Виленкин Н.Я.", "Жохов В.И."]
    publisher VARCHAR(200),                -- "Мнемозина"
    
    -- Классификация
    subject VARCHAR(50) NOT NULL,          -- "mathematics"
    class_level INTEGER NOT NULL,          -- 5, 6, 7...
    country VARCHAR(10) DEFAULT 'RU',      -- "RU", "UZ", "KZ"
    language VARCHAR(10) DEFAULT 'ru',     -- "ru", "uz", "en"
    
    -- Медиа
    cover_image_url TEXT,
    total_pages INTEGER,
    
    -- Статус оцифровки
    digitization_status VARCHAR(20) DEFAULT 'pending' 
        CHECK (digitization_status IN ('pending', 'in_progress', 'completed', 'verified')),
    digitization_progress DECIMAL(5,2) DEFAULT 0,  -- 0-100%
    tasks_extracted INTEGER DEFAULT 0,
    
    -- Метаданные
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_tb_class ON textbooks(class_level);
CREATE INDEX idx_tb_subject ON textbooks(subject);
CREATE INDEX idx_tb_country ON textbooks(country);
CREATE INDEX idx_tb_active ON textbooks(is_active) WHERE is_active = TRUE;

-- ============================================================================
-- 2. TEXTBOOK_STRUCTURE - Структура учебника (рекурсивная)
-- ============================================================================
-- Позволяет хранить любую глубину: Часть → Глава → Параграф → Пункт

CREATE TABLE textbook_structure (
    id SERIAL PRIMARY KEY,
    textbook_id UUID NOT NULL,
    parent_id INTEGER,                     -- NULL для корневых элементов
    
    -- Тип узла
    node_type VARCHAR(20) NOT NULL CHECK (node_type IN (
        'part',        -- Часть
        'chapter',     -- Глава
        'section',     -- Раздел/Параграф
        'subsection',  -- Подраздел/Пункт
        'topic'        -- Тема
    )),
    
    -- Отображение (как в книге)
    display_number VARCHAR(30),            -- "§15", "Глава 3", "1.2.3"
    display_name VARCHAR(500) NOT NULL,    -- "Умножение натуральных чисел и его свойства"
    full_title TEXT,                       -- Полное название с номером
    
    -- Позиция в книге
    page_start INTEGER,                    -- Начальная страница
    page_end INTEGER,                      -- Конечная страница
    sort_order INTEGER NOT NULL,           -- Порядок внутри родителя
    
    -- Маппинг на CORE (связь с knowledge_hierarchy)
    mapped_skill_id VARCHAR(30),           -- Ссылка на L4 навык
    mapped_topic_id VARCHAR(30),           -- Ссылка на L3 тему
    mapped_cluster_id VARCHAR(30),         -- Ссылка на L2 кластер
    mapping_confidence DECIMAL(3,2),       -- Уверенность AI в маппинге (0-1)
    mapping_verified BOOLEAN DEFAULT FALSE,
    
    -- Описание и цели (из книги)
    description TEXT,
    learning_objectives TEXT[],            -- Цели обучения
    key_concepts TEXT[],                   -- Ключевые понятия
    
    -- Статистика
    tasks_count INTEGER DEFAULT 0,
    estimated_hours DECIMAL(4,2),          -- Часы на изучение
    
    -- Метаданные
    created_at TIMESTAMP DEFAULT NOW(),
    
    FOREIGN KEY (textbook_id) REFERENCES textbooks(textbook_id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES textbook_structure(id) ON DELETE CASCADE,
    FOREIGN KEY (mapped_skill_id) REFERENCES knowledge_hierarchy(id),
    FOREIGN KEY (mapped_topic_id) REFERENCES knowledge_hierarchy(id),
    FOREIGN KEY (mapped_cluster_id) REFERENCES knowledge_hierarchy(id)
);

CREATE INDEX idx_ts_textbook ON textbook_structure(textbook_id);
CREATE INDEX idx_ts_parent ON textbook_structure(parent_id);
CREATE INDEX idx_ts_mapped_skill ON textbook_structure(mapped_skill_id);
CREATE INDEX idx_ts_sort ON textbook_structure(textbook_id, sort_order);

-- ============================================================================
-- 3. TEXTBOOK_TASKS - Привязка задач к учебникам
-- ============================================================================
-- Одна задача может быть в нескольких учебниках (но с разными номерами)

CREATE TABLE textbook_tasks (
    id SERIAL PRIMARY KEY,
    textbook_id UUID NOT NULL,
    task_id VARCHAR(30) NOT NULL,
    structure_id INTEGER,                  -- К какому разделу относится
    
    -- Как задача выглядит в книге
    exercise_number VARCHAR(30),           -- "245а", "12.5(б)"
    page_number INTEGER,
    section_reference VARCHAR(100),        -- "§12"
    
    -- Контекст из книги
    book_context TEXT,                     -- "После примера 5"
    original_text TEXT,                    -- Оригинальный текст из книги
    
    -- Оцифровка
    ocr_confidence DECIMAL(3,2),           -- Уверенность OCR
    manually_verified BOOLEAN DEFAULT FALSE,
    
    -- Статус
    created_at TIMESTAMP DEFAULT NOW(),
    
    FOREIGN KEY (textbook_id) REFERENCES textbooks(textbook_id) ON DELETE CASCADE,
    FOREIGN KEY (task_id) REFERENCES tasks_master(id) ON DELETE CASCADE,
    FOREIGN KEY (structure_id) REFERENCES textbook_structure(id),
    
    -- Уникальность: одна задача один раз в учебнике
    UNIQUE(textbook_id, task_id)
);

CREATE INDEX idx_tbt_textbook ON textbook_tasks(textbook_id);
CREATE INDEX idx_tbt_task ON textbook_tasks(task_id);
CREATE INDEX idx_tbt_structure ON textbook_tasks(structure_id);
CREATE INDEX idx_tbt_page ON textbook_tasks(textbook_id, page_number);

-- ============================================================================
-- 4. TEXTBOOK_SYNONYMS - Синонимы названий тем
-- ============================================================================
-- Разные учебники называют одно и то же по-разному

CREATE TABLE topic_synonyms (
    id SERIAL PRIMARY KEY,
    skill_id VARCHAR(30) NOT NULL,         -- Ссылка на knowledge_hierarchy
    
    synonym_text VARCHAR(300) NOT NULL,    -- "Квадратные уравнения"
    source_textbook_id UUID,               -- Из какого учебника
    language VARCHAR(10) DEFAULT 'ru',
    
    -- Статус
    is_primary BOOLEAN DEFAULT FALSE,      -- Основное название
    verified BOOLEAN DEFAULT FALSE,
    
    created_at TIMESTAMP DEFAULT NOW(),
    
    FOREIGN KEY (skill_id) REFERENCES knowledge_hierarchy(id),
    FOREIGN KEY (source_textbook_id) REFERENCES textbooks(textbook_id)
);

CREATE INDEX idx_syn_skill ON topic_synonyms(skill_id);
CREATE INDEX idx_syn_text ON topic_synonyms USING gin(to_tsvector('russian', synonym_text));

-- ============================================================================
-- 5. DIGITIZATION_LOG - Лог оцифровки
-- ============================================================================
-- Отслеживание процесса оцифровки учебников

CREATE TABLE digitization_log (
    id SERIAL PRIMARY KEY,
    textbook_id UUID NOT NULL,
    
    -- Что обработали
    page_number INTEGER,
    page_image_url TEXT,
    
    -- Результаты этапов
    ocr_result JSONB,                      -- Результат Mathpix
    classification_result JSONB,           -- Результат Gemini
    validation_result JSONB,               -- Результат SymPy
    
    -- Извлеченные задачи
    tasks_extracted INTEGER DEFAULT 0,
    tasks_approved INTEGER DEFAULT 0,
    tasks_rejected INTEGER DEFAULT 0,
    
    -- Ошибки
    errors JSONB,
    
    -- Статус
    status VARCHAR(20) DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'manual_review')),
    
    -- Время
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    processing_time_ms INTEGER,
    
    created_at TIMESTAMP DEFAULT NOW(),
    
    FOREIGN KEY (textbook_id) REFERENCES textbooks(textbook_id) ON DELETE CASCADE
);

CREATE INDEX idx_dl_textbook ON digitization_log(textbook_id);
CREATE INDEX idx_dl_status ON digitization_log(status);
CREATE INDEX idx_dl_page ON digitization_log(textbook_id, page_number);

-- ============================================================================
-- ПРИМЕРЫ ДАННЫХ
-- ============================================================================
-- Стейл seed-данные (UUID a0eebc99-*) удалены 22.02.2026.
-- Актуальные данные учебников загружаются из:
--   database/007a_grade5_textbook.sql (UUID a1b2c3d4-*, 10 глав, 47 §§)
--   database/008_seed_data.sql (метаданные 4 учебников)
-- ============================================================================

-- ============================================================================
-- ПРЕДСТАВЛЕНИЯ (VIEWS) для удобства
-- ============================================================================

-- Полная структура учебника с иерархией
CREATE OR REPLACE VIEW v_textbook_full_structure AS
WITH RECURSIVE structure_tree AS (
    -- Базовый случай: корневые элементы
    SELECT 
        ts.id,
        ts.textbook_id,
        ts.parent_id,
        ts.node_type,
        ts.display_number,
        ts.display_name,
        ts.page_start,
        ts.sort_order,
        ts.mapped_skill_id,
        ts.mapped_topic_id,
        1 as depth,
        ts.display_number as path
    FROM textbook_structure ts
    WHERE ts.parent_id IS NULL
    
    UNION ALL
    
    -- Рекурсивный случай: дочерние элементы
    SELECT 
        ts.id,
        ts.textbook_id,
        ts.parent_id,
        ts.node_type,
        ts.display_number,
        ts.display_name,
        ts.page_start,
        ts.sort_order,
        ts.mapped_skill_id,
        ts.mapped_topic_id,
        st.depth + 1,
        st.path || ' > ' || ts.display_number
    FROM textbook_structure ts
    JOIN structure_tree st ON ts.parent_id = st.id
)
SELECT 
    st.*,
    t.title as textbook_title,
    kh_skill.name_ru as skill_name,
    kh_topic.name_ru as topic_name
FROM structure_tree st
JOIN textbooks t ON st.textbook_id = t.textbook_id
LEFT JOIN knowledge_hierarchy kh_skill ON st.mapped_skill_id = kh_skill.id
LEFT JOIN knowledge_hierarchy kh_topic ON st.mapped_topic_id = kh_topic.id
ORDER BY st.textbook_id, st.path;

-- Статистика оцифровки по учебникам
CREATE OR REPLACE VIEW v_digitization_stats AS
SELECT 
    t.textbook_id,
    t.title,
    t.class_level,
    t.total_pages,
    t.digitization_status,
    t.digitization_progress,
    COUNT(DISTINCT dl.page_number) as pages_processed,
    SUM(dl.tasks_extracted) as total_tasks_extracted,
    SUM(dl.tasks_approved) as total_tasks_approved,
    COUNT(CASE WHEN dl.status = 'failed' THEN 1 END) as failed_pages,
    COUNT(CASE WHEN dl.status = 'manual_review' THEN 1 END) as review_pending
FROM textbooks t
LEFT JOIN digitization_log dl ON t.textbook_id = dl.textbook_id
GROUP BY t.textbook_id, t.title, t.class_level, t.total_pages, t.digitization_status, t.digitization_progress;

COMMIT;
