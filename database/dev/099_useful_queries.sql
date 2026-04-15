-- ============================================================================
-- ALGO V1 - Полезные SQL запросы
-- ============================================================================

-- ============================================================================
-- 1. ЗАПРОСЫ ДЛЯ ДИАГНОСТИКИ (Модуль 1)
-- ============================================================================

-- 1.1 Получить критические L4 навыки для класса
SELECT 
    kh.id,
    kh.name_ru,
    kh.importance,
    kh.cognitive_type,
    kh.typical_time_seconds,
    sb.avg_solve_time_seconds,
    sb.success_rate_grade_8 as expected_success
FROM knowledge_hierarchy kh
LEFT JOIN skill_benchmarks sb ON kh.id = sb.skill_id
WHERE kh.level = 'L4'
  AND kh.class_level_start <= 8
  AND kh.class_level_end >= 8
  AND kh.is_active = TRUE
  AND kh.importance >= 8
ORDER BY kh.importance DESC, kh.sequence_order;

-- 1.2 Получить граф prerequisites для навыка (включая транзитивные)
WITH RECURSIVE prereq_tree AS (
    -- Базовый случай: прямые prerequisites
    SELECT 
        sp.skill_id,
        sp.prerequisite_id,
        sp.dependency_type,
        sp.criticality,
        1 as depth,
        ARRAY[sp.prerequisite_id] as path
    FROM skill_prerequisites sp
    WHERE sp.skill_id = 'ALG_EQ_QUAD_SOLVE'
    
    UNION ALL
    
    -- Рекурсивный случай: prerequisites prerequisites
    SELECT 
        pt.skill_id,
        sp.prerequisite_id,
        sp.dependency_type,
        sp.criticality,
        pt.depth + 1,
        pt.path || sp.prerequisite_id
    FROM prereq_tree pt
    JOIN skill_prerequisites sp ON pt.prerequisite_id = sp.skill_id
    WHERE NOT sp.prerequisite_id = ANY(pt.path)  -- Избегаем циклов
      AND pt.depth < 5  -- Ограничение глубины
)
SELECT DISTINCT
    pt.prerequisite_id,
    kh.name_ru,
    pt.dependency_type,
    pt.criticality,
    pt.depth
FROM prereq_tree pt
JOIN knowledge_hierarchy kh ON pt.prerequisite_id = kh.id
ORDER BY pt.depth, pt.criticality DESC;

-- 1.3 Найти навыки с пробелами (по статистике)
SELECT 
    kh.id,
    kh.name_ru,
    sb.total_attempts,
    sb.total_correct,
    ROUND(sb.total_correct::DECIMAL / NULLIF(sb.total_attempts, 0) * 100, 2) as success_rate,
    sb.success_rate_grade_8,
    COUNT(sp.skill_id) as blocks_count
FROM knowledge_hierarchy kh
JOIN skill_benchmarks sb ON kh.id = sb.skill_id
LEFT JOIN skill_prerequisites sp ON kh.id = sp.prerequisite_id
WHERE kh.level = 'L4'
  AND (sb.success_rate_grade_8 < 50 OR sb.total_correct::DECIMAL / NULLIF(sb.total_attempts, 0) < 0.5)
GROUP BY kh.id, kh.name_ru, sb.total_attempts, sb.total_correct, sb.success_rate_grade_8
ORDER BY blocks_count DESC, success_rate ASC;

-- 1.4 Выбрать задачу для навыка и сложности
SELECT 
    tm.id,
    tm.question_text,
    tm.question_latex,
    tm.difficulty,
    tm.correct_answer,
    tm.times_used,
    tm.avg_time_seconds
FROM tasks_master tm
WHERE tm.skill_id = 'ALG_EQ_QUAD_DISCRIM'
  AND tm.difficulty = 'B'
  AND tm.is_active = TRUE
  AND tm.verification_status = 'verified'
ORDER BY tm.times_used ASC, RANDOM()
LIMIT 1;

-- ============================================================================
-- 2. ЗАПРОСЫ ДЛЯ УЧИТЕЛЯ (Модуль 2)
-- ============================================================================

-- 2.1 Получить структуру учебника для выбора тем
SELECT 
    ts.id,
    ts.node_type,
    ts.display_number,
    ts.display_name,
    ts.page_start,
    ts.tasks_count,
    kh.name_ru as mapped_skill_name,
    REPEAT('  ', ts.depth - 1) || ts.display_name as indented_name
FROM (
    WITH RECURSIVE structure_tree AS (
        SELECT id, textbook_id, parent_id, node_type, display_number, display_name, 
               page_start, tasks_count, mapped_skill_id, sort_order, 1 as depth
        FROM textbook_structure
        WHERE textbook_id = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890'
          AND parent_id IS NULL
        
        UNION ALL
        
        SELECT ts.id, ts.textbook_id, ts.parent_id, ts.node_type, ts.display_number, 
               ts.display_name, ts.page_start, ts.tasks_count, ts.mapped_skill_id, 
               ts.sort_order, st.depth + 1
        FROM textbook_structure ts
        JOIN structure_tree st ON ts.parent_id = st.id
    )
    SELECT * FROM structure_tree
) ts
LEFT JOIN knowledge_hierarchy kh ON ts.mapped_skill_id = kh.id
ORDER BY ts.sort_order;

-- 2.2 Получить задачи из выбранных параграфов учебника
SELECT 
    tm.id,
    tm.question_text,
    tm.question_latex,
    tm.difficulty,
    tt.exercise_number,
    tt.page_number,
    ts.display_name as section_name
FROM tasks_master tm
JOIN textbook_tasks tt ON tm.id = tt.task_id
JOIN textbook_structure ts ON tt.structure_id = ts.id
WHERE tt.textbook_id = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890'
  AND ts.display_number IN ('§1', '§2', '§3')
  AND tm.is_active = TRUE
ORDER BY ts.sort_order, tt.page_number, tt.exercise_number;

-- 2.3 Сгенерировать тест: выбрать задачи с балансом сложности
WITH section_tasks AS (
    SELECT 
        tm.*,
        tt.exercise_number,
        tt.page_number,
        ROW_NUMBER() OVER (PARTITION BY tm.skill_id, tm.difficulty ORDER BY RANDOM()) as rn
    FROM tasks_master tm
    JOIN textbook_tasks tt ON tm.id = tt.task_id
    WHERE tt.textbook_id = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890'
      AND tm.is_active = TRUE
      AND tm.verification_status = 'verified'
)
SELECT * FROM (
    -- 30% легких (A)
    SELECT * FROM section_tasks WHERE difficulty = 'A' AND rn <= 3
    UNION ALL
    -- 50% средних (B)
    SELECT * FROM section_tasks WHERE difficulty = 'B' AND rn <= 5
    UNION ALL
    -- 20% сложных (C)
    SELECT * FROM section_tasks WHERE difficulty = 'C' AND rn <= 2
) balanced_test
ORDER BY difficulty, RANDOM();

-- ============================================================================
-- 3. ЗАПРОСЫ ДЛЯ КОНВЕЙЕРА ОЦИФРОВКИ
-- ============================================================================

-- 3.1 Статистика оцифровки по учебникам
SELECT 
    t.title,
    t.class_level,
    t.digitization_status,
    t.digitization_progress,
    t.tasks_extracted,
    COUNT(DISTINCT dl.page_number) as pages_processed,
    SUM(CASE WHEN dl.status = 'completed' THEN 1 ELSE 0 END) as pages_success,
    SUM(CASE WHEN dl.status = 'failed' THEN 1 ELSE 0 END) as pages_failed,
    SUM(CASE WHEN dl.status = 'manual_review' THEN 1 ELSE 0 END) as pages_review
FROM textbooks t
LEFT JOIN digitization_log dl ON t.textbook_id = dl.textbook_id
GROUP BY t.textbook_id, t.title, t.class_level, t.digitization_status, 
         t.digitization_progress, t.tasks_extracted
ORDER BY t.class_level, t.title;

-- 3.2 Задачи, требующие ручной проверки
SELECT 
    tm.id,
    tm.question_text,
    tm.skill_id,
    kh.name_ru as skill_name,
    tm.difficulty,
    tm.verification_status,
    tm.source_reference,
    CASE 
        WHEN tm.verification_status = 'conflict' THEN 'Конфликт SymPy vs учебник'
        WHEN tm.difficulty = 'C' THEN 'Level C требует проверки'
        ELSE 'Низкая уверенность'
    END as review_reason
FROM tasks_master tm
JOIN knowledge_hierarchy kh ON tm.skill_id = kh.id
WHERE tm.verification_status IN ('pending', 'conflict')
   OR tm.difficulty = 'C'
ORDER BY 
    CASE tm.verification_status WHEN 'conflict' THEN 0 ELSE 1 END,
    tm.created_at;

-- 3.3 Качество шаблонов
SELECT 
    tt.id,
    tt.template_latex,
    tt.quality_score,
    tt.tested_combinations,
    tt.successful_combinations,
    kh.name_ru as skill_name,
    tm.question_text as original_task
FROM task_templates tt
JOIN knowledge_hierarchy kh ON tt.skill_id = kh.id
JOIN tasks_master tm ON tt.original_task_id = tm.id
WHERE tt.quality_score < 0.85
ORDER BY tt.quality_score ASC;

-- ============================================================================
-- 4. АНАЛИТИЧЕСКИЕ ЗАПРОСЫ
-- ============================================================================

-- 4.1 Топ-10 самых проблемных навыков
SELECT 
    kh.id,
    kh.name_ru,
    kh.class_level_start,
    sb.total_attempts,
    ROUND(sb.total_correct::DECIMAL / NULLIF(sb.total_attempts, 0) * 100, 2) as success_rate,
    ARRAY_AGG(DISTINCT sp.skill_id) as blocked_skills
FROM knowledge_hierarchy kh
JOIN skill_benchmarks sb ON kh.id = sb.skill_id
LEFT JOIN skill_prerequisites sp ON kh.id = sp.prerequisite_id
WHERE kh.level = 'L4'
  AND sb.total_attempts >= 100
GROUP BY kh.id, kh.name_ru, kh.class_level_start, sb.total_attempts, sb.total_correct
ORDER BY success_rate ASC
LIMIT 10;

-- 4.2 Покрытие навыков задачами
SELECT 
    kh.id,
    kh.name_ru,
    kh.level,
    COUNT(tm.id) as total_tasks,
    SUM(CASE WHEN tm.difficulty = 'A' THEN 1 ELSE 0 END) as tasks_a,
    SUM(CASE WHEN tm.difficulty = 'B' THEN 1 ELSE 0 END) as tasks_b,
    SUM(CASE WHEN tm.difficulty = 'C' THEN 1 ELSE 0 END) as tasks_c,
    CASE 
        WHEN COUNT(tm.id) = 0 THEN 'Нет задач!'
        WHEN SUM(CASE WHEN tm.difficulty = 'A' THEN 1 ELSE 0 END) = 0 THEN 'Нет Level A'
        WHEN SUM(CASE WHEN tm.difficulty = 'B' THEN 1 ELSE 0 END) = 0 THEN 'Нет Level B'
        WHEN COUNT(tm.id) < 5 THEN 'Мало задач'
        ELSE 'OK'
    END as coverage_status
FROM knowledge_hierarchy kh
LEFT JOIN tasks_master tm ON kh.id = tm.skill_id AND tm.is_active = TRUE
WHERE kh.level = 'L4'
GROUP BY kh.id, kh.name_ru, kh.level
ORDER BY total_tasks ASC;

-- 4.3 Среднее время решения по сложности
SELECT 
    tm.difficulty,
    kh.name_ru as skill_name,
    ROUND(AVG(tm.avg_time_seconds), 2) as avg_time,
    ROUND(STDDEV(tm.avg_time_seconds), 2) as stddev_time,
    COUNT(*) as tasks_count
FROM tasks_master tm
JOIN knowledge_hierarchy kh ON tm.skill_id = kh.id
WHERE tm.avg_time_seconds IS NOT NULL
  AND tm.times_used >= 10
GROUP BY tm.difficulty, kh.name_ru
ORDER BY tm.difficulty, avg_time DESC;

-- ============================================================================
-- 5. СЛУЖЕБНЫЕ ЗАПРОСЫ
-- ============================================================================

-- 5.1 Проверка целостности графа prerequisites (нет циклов)
WITH RECURSIVE cycle_check AS (
    SELECT 
        skill_id,
        prerequisite_id,
        ARRAY[skill_id] as path,
        FALSE as has_cycle
    FROM skill_prerequisites
    
    UNION ALL
    
    SELECT 
        cc.skill_id,
        sp.prerequisite_id,
        cc.path || sp.prerequisite_id,
        sp.prerequisite_id = ANY(cc.path)
    FROM cycle_check cc
    JOIN skill_prerequisites sp ON cc.prerequisite_id = sp.skill_id
    WHERE NOT sp.prerequisite_id = ANY(cc.path)
      AND array_length(cc.path, 1) < 10
)
SELECT DISTINCT skill_id, path
FROM cycle_check
WHERE has_cycle = TRUE;

-- 5.2 Навыки без prerequisites (корневые)
SELECT kh.id, kh.name_ru, kh.class_level_start
FROM knowledge_hierarchy kh
LEFT JOIN skill_prerequisites sp ON kh.id = sp.skill_id
WHERE kh.level = 'L4'
  AND sp.skill_id IS NULL
ORDER BY kh.class_level_start, kh.sequence_order;

-- 5.3 Обновить статистику benchmarks
UPDATE skill_benchmarks sb
SET 
    total_attempts = subq.attempts,
    total_correct = subq.correct,
    avg_solve_time_seconds = subq.avg_time,
    last_calculated_at = NOW()
FROM (
    SELECT 
        tm.skill_id,
        SUM(tm.times_used) as attempts,
        SUM(tm.times_correct) as correct,
        AVG(tm.avg_time_seconds) as avg_time
    FROM tasks_master tm
    WHERE tm.is_active = TRUE
    GROUP BY tm.skill_id
) subq
WHERE sb.skill_id = subq.skill_id;
