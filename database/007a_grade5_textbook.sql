-- ==========================================================================
-- ALGO V1 — Учебник «Математика 5 класс» + структура + привязка задач
-- Генератор: seed_textbook_grade5.py
-- ==========================================================================

DELETE FROM textbook_tasks WHERE textbook_id = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890';
DELETE FROM textbook_structure WHERE textbook_id = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890';
DELETE FROM textbooks WHERE textbook_id = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890';

INSERT INTO textbooks (textbook_id, isbn, title, subtitle, edition,
  authors, publisher, subject, class_level, country, language,
  total_pages, digitization_status, digitization_progress, tasks_extracted, is_active)
VALUES (
  'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
  '978-5-09-10001-1',
  'Математика. 5 класс',
  'Учебник для общеобразовательных организаций',
  '37-е изд.',
  ARRAY['Виленкин Н.Я.', 'Жохов В.И.', 'Чесноков А.С.', 'Шварцбурд С.И.'],
  'Мнемозина', 'mathematics', 5, 'RU', 'ru',
  280, 'completed', 100.00, 0, TRUE
);

-- ── Структура учебника ──
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_cluster_id, tasks_count)
VALUES (1000, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', NULL, 'chapter', 'Глава 1', 'Натуральные числа и шкалы', 1, 'G5_T01', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1001, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1000, 'section', '§1', 'Обозначение натуральных чисел', 1, 'G5_P01', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1002, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1000, 'section', '§2', 'Отрезок. Длина отрезка. Треугольник', 2, 'G5_P02', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1003, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1000, 'section', '§3', 'Плоскость. Прямая. Луч', 3, 'G5_P03', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1004, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1000, 'section', '§4', 'Шкалы и координаты', 4, 'G5_P03', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1005, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1000, 'section', '§5', 'Сравнение натуральных чисел', 5, 'G5_P04', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_cluster_id, tasks_count)
VALUES (1006, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', NULL, 'chapter', 'Глава 2', 'Сложение и вычитание натуральных чисел', 2, 'G5_T02', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1007, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1006, 'section', '§6', 'Сложение натуральных чисел', 1, 'G5_P06', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1008, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1006, 'section', '§7', 'Вычитание', 2, 'G5_P06', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1009, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1006, 'section', '§8', 'Числовые и буквенные выражения', 3, 'G5_P07', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1010, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1006, 'section', '§9', 'Буквенная запись свойств сложения и вычитания', 4, 'G5_P07', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1011, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1006, 'section', '§10', 'Уравнение', 5, 'G5_P08', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_cluster_id, tasks_count)
VALUES (1012, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', NULL, 'chapter', 'Глава 3', 'Умножение и деление натуральных чисел', 3, 'G5_T03', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1013, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1012, 'section', '§11', 'Умножение натуральных чисел', 1, 'G5_P09', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1014, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1012, 'section', '§12', 'Деление', 2, 'G5_P10', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1015, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1012, 'section', '§13', 'Деление с остатком', 3, 'G5_P11', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1016, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1012, 'section', '§14', 'Упрощение выражений', 4, 'G5_P12', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1017, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1012, 'section', '§15', 'Порядок выполнения действий', 5, 'G5_P13', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1018, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1012, 'section', '§16', 'Степень числа. Квадрат и куб', 6, 'G5_P14', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_cluster_id, tasks_count)
VALUES (1019, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', NULL, 'chapter', 'Глава 4', 'Площади и объёмы', 4, 'G5_T05', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1020, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1019, 'section', '§17', 'Формулы', 1, 'G5_P18', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1021, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1019, 'section', '§18', 'Площадь. Формула площади прямоугольника', 2, 'G5_P19', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1022, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1019, 'section', '§19', 'Единицы измерения площадей', 3, 'G5_P21', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1023, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1019, 'section', '§20', 'Прямоугольный параллелепипед', 4, 'G5_P22', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1024, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1019, 'section', '§21', 'Объёмы. Объём прямоугольного параллелепипеда', 5, 'G5_P23', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_cluster_id, tasks_count)
VALUES (1025, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', NULL, 'chapter', 'Глава 5', 'Обыкновенные дроби', 5, 'G5_T06', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1026, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1025, 'section', '§22', 'Окружность и круг', 1, 'G5_P24', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1027, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1025, 'section', '§23', 'Доли. Обыкновенные дроби', 2, 'G5_P24', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1028, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1025, 'section', '§24', 'Сравнение дробей', 3, 'G5_P25', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1029, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1025, 'section', '§25', 'Правильные и неправильные дроби', 4, 'G5_P26', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1030, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1025, 'section', '§26', 'Сложение и вычитание дробей с одинаковыми знаменателями', 5, 'G5_P27', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1031, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1025, 'section', '§27', 'Деление и дроби', 6, 'G5_P28', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1032, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1025, 'section', '§28', 'Смешанные числа', 7, 'G5_P29', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1033, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1025, 'section', '§29', 'Сложение и вычитание смешанных чисел', 8, 'G5_P30', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_cluster_id, tasks_count)
VALUES (1034, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', NULL, 'chapter', 'Глава 6', 'Десятичные дроби. Сложение и вычитание', 6, 'G5_T07', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1035, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1034, 'section', '§30', 'Десятичная запись дробных чисел', 1, 'G5_P31', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1036, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1034, 'section', '§31', 'Сравнение десятичных дробей', 2, 'G5_P32', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1037, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1034, 'section', '§32', 'Сложение и вычитание десятичных дробей', 3, 'G5_P33', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1038, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1034, 'section', '§33', 'Приближённые значения. Округление', 4, 'G5_P34', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_cluster_id, tasks_count)
VALUES (1039, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', NULL, 'chapter', 'Глава 7', 'Умножение и деление десятичных дробей', 7, 'G5_T08', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1040, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1039, 'section', '§34', 'Умножение десятичных дробей на натуральное число', 1, 'G5_P35', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1041, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1039, 'section', '§35', 'Деление десятичных дробей на натуральное число', 2, 'G5_P36', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1042, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1039, 'section', '§36', 'Умножение десятичных дробей', 3, 'G5_P37', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1043, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1039, 'section', '§37', 'Деление на десятичную дробь', 4, 'G5_P38', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_cluster_id, tasks_count)
VALUES (1044, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', NULL, 'chapter', 'Глава 8', 'Инструменты для вычислений и измерений', 8, 'G5_T04', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1045, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1044, 'section', '§38', 'Проценты', 1, 'G5_P39', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1046, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1044, 'section', '§39', 'Угол. Прямой и развёрнутый угол', 2, 'G5_P15', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1047, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1044, 'section', '§40', 'Измерение углов. Транспортир', 3, 'G5_P15', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1048, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1044, 'section', '§41', 'Круговые диаграммы', 4, 'G5_P41', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_cluster_id, tasks_count)
VALUES (1049, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', NULL, 'chapter', 'Глава 9', 'Статистика и вероятность', 9, 'G5_T09', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1050, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1049, 'section', '§42', 'Среднее арифметическое', 1, 'G5_P40', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_cluster_id, tasks_count)
VALUES (1051, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', NULL, 'chapter', 'Глава 10', 'Текстовые задачи', 10, 'G5_T10', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1052, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1051, 'section', '§43', 'Задачи на дроби', 1, 'G5_P42', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1053, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1051, 'section', '§44', 'Задачи на движение', 2, 'G5_P43', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1054, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1051, 'section', '§45', 'Задачи на движение двух тел', 3, 'G5_P44', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1055, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1051, 'section', '§46', 'Задачи на стоимость', 4, 'G5_P45', 0);
INSERT INTO textbook_structure (id, textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, tasks_count)
VALUES (1056, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', 1051, 'section', '§47', 'Задачи на работу', 5, 'G5_P46', 0);

-- ── Привязка задач к параграфам учебника ──
-- Привязываем все задачи (и ручные G5_TASK_*, и сгенерированные G5_GEN_*)
-- к соответствующим параграфам через skill_id
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1001, '§1.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S01_01', 'G5_S01_02', 'G5_S01_03', 'G5_S01_04') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1002, '§2.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S02_01', 'G5_S02_02', 'G5_S02_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1003, '§3.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S03_01', 'G5_S03_02', 'G5_S03_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1005, '§5.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S04_01', 'G5_S04_02', 'G5_S04_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1007, '§6.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S06_01', 'G5_S06_02') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1008, '§7.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S06_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1009, '§8.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S07_01', 'G5_S07_02', 'G5_S07_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1011, '§10.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S08_01', 'G5_S08_02', 'G5_S08_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1013, '§11.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S09_01', 'G5_S09_02', 'G5_S09_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1014, '§12.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S10_01', 'G5_S10_02', 'G5_S10_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1015, '§13.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S11_01', 'G5_S11_02') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1016, '§14.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S12_01', 'G5_S12_02') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1017, '§15.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S13_01', 'G5_S13_02') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1018, '§16.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S14_01', 'G5_S14_02') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1021, '§18.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S19_01', 'G5_S19_02', 'G5_S19_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1022, '§19.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S21_01', 'G5_S21_02') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1023, '§20.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S22_01', 'G5_S22_02') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1024, '§21.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S23_01', 'G5_S23_02', 'G5_S23_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1027, '§23.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S24_01', 'G5_S24_02', 'G5_S24_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1028, '§24.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S25_01', 'G5_S25_02', 'G5_S25_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1029, '§25.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S26_01', 'G5_S26_02') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1030, '§26.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S27_01', 'G5_S27_02') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1031, '§27.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S28_01', 'G5_S28_02') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1032, '§28.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S29_01', 'G5_S29_02') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1033, '§29.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S30_01', 'G5_S30_02', 'G5_S30_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1035, '§30.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S31_01', 'G5_S31_02', 'G5_S31_03', 'G5_S31_04') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1036, '§31.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S32_01', 'G5_S32_02', 'G5_S32_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1037, '§32.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S33_01', 'G5_S33_02', 'G5_S33_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1038, '§33.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S34_01', 'G5_S34_02', 'G5_S34_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1040, '§34.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S35_01', 'G5_S35_02') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1041, '§35.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S36_01', 'G5_S36_02') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1042, '§36.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S37_01', 'G5_S37_02') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1043, '§37.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S38_01', 'G5_S38_02') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1045, '§38.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S39_01', 'G5_S39_02', 'G5_S39_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1046, '§39.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S15_01', 'G5_S15_02', 'G5_S15_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1048, '§41.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S41_01', 'G5_S41_02', 'G5_S41_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1050, '§42.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S40_01', 'G5_S40_02', 'G5_S40_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1052, '§43.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S42_01', 'G5_S42_02', 'G5_S42_03') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1053, '§44.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S43_01', 'G5_S43_02') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1054, '§45.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S44_01', 'G5_S44_02') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1055, '§46.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S45_01', 'G5_S45_02') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;
INSERT INTO textbook_tasks (textbook_id, task_id, structure_id, exercise_number, manually_verified)
SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890', tm.id, 1056, '§47.' || ROW_NUMBER() OVER (ORDER BY tm.id), TRUE
FROM tasks_master tm WHERE tm.skill_id IN ('G5_S46_01', 'G5_S46_02') AND tm.is_active = TRUE
ON CONFLICT (textbook_id, task_id) DO NOTHING;

-- Обновляем tasks_count в секциях
UPDATE textbook_structure ts SET tasks_count = (
  SELECT COUNT(*) FROM textbook_tasks tt WHERE tt.structure_id = ts.id
) WHERE ts.textbook_id = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890' AND ts.node_type = 'section';

-- Обновляем tasks_extracted в учебнике
UPDATE textbooks SET tasks_extracted = (
  SELECT COUNT(*) FROM textbook_tasks WHERE textbook_id = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890'
) WHERE textbook_id = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890';
