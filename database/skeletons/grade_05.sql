-- ============================================================================
-- ALGO V1 — ПОЛНАЯ ИЕРАРХИЯ 5 КЛАСС (из PDF методиста, 2025)
-- Sardor Bozorov — L1→L4 скелет навыков
-- 2 раздела, 10 тем, 47 пунктов, 126 атомарных навыков
-- ============================================================================

-- Очистка существующих данных 5 класса (если есть)
DELETE FROM skill_prerequisites WHERE skill_id LIKE 'G5_%' OR prerequisite_id LIKE 'G5_%';
DELETE FROM skill_benchmarks WHERE skill_id LIKE 'G5_%';
DELETE FROM tasks_master WHERE skill_id LIKE 'G5_%';
DELETE FROM knowledge_hierarchy WHERE id LIKE 'G5_%';

-- ============================================================================
-- L1: РАЗДЕЛЫ (Domains) — 2 раздела
-- ============================================================================
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order)
VALUES
    ('G5_NAT',  'L1', NULL, 'Natural Numbers',   'Натуральные числа', 'Раздел I. Натуральные числа',  5, 5, 10, 1),
    ('G5_FRAC', 'L1', NULL, 'Fractional Numbers', 'Дробные числа',     'Раздел II. Дробные числа',    5, 5, 10, 2);

-- ============================================================================
-- L2: ТЕМЫ (Clusters) — 10 тем
-- ============================================================================
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order)
VALUES
    -- Раздел I: Натуральные числа
    ('G5_T01', 'L2', 'G5_NAT',  'Natural Numbers and Scales',           'Натуральные числа и шкалы',                       'Тема 1',  5, 5, 10, 1),
    ('G5_T02', 'L2', 'G5_NAT',  'Addition and Subtraction',             'Сложение и вычитание натуральных чисел',           'Тема 2',  5, 5, 10, 2),
    ('G5_T03', 'L2', 'G5_NAT',  'Multiplication and Division',          'Умножение и деление натуральных чисел',            'Тема 3',  5, 5, 10, 3),
    ('G5_T04', 'L2', 'G5_NAT',  'Geometric Shapes',                     'Геометрические фигуры',                            'Тема 4',  5, 5,  9, 4),
    ('G5_T05', 'L2', 'G5_NAT',  'Areas and Volumes',                    'Площади и объёмы',                                 'Тема 5',  5, 5,  9, 5),
    -- Раздел II: Дробные числа
    ('G5_T06', 'L2', 'G5_FRAC', 'Common Fractions',                     'Обыкновенные дроби',                               'Тема 6',  5, 5, 10, 6),
    ('G5_T07', 'L2', 'G5_FRAC', 'Decimal Fractions Add/Sub',            'Десятичные дроби. Сложение и вычитание',            'Тема 7',  5, 5, 10, 7),
    ('G5_T08', 'L2', 'G5_FRAC', 'Decimal Fractions Mul/Div',            'Умножение и деление десятичных дробей',             'Тема 8',  5, 5, 10, 8),
    ('G5_T09', 'L2', 'G5_FRAC', 'Data Analysis',                        'Анализ данных',                                    'Тема 9',  5, 5,  8, 9),
    ('G5_T10', 'L2', 'G5_FRAC', 'Word Problems',                        'Текстовые задачи',                                 'Тема 10', 5, 5,  9, 10);

-- ============================================================================
-- L3: ПУНКТЫ (Topics) — 47 пунктов
-- ============================================================================
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order)
VALUES
    -- Тема 1: Натуральные числа и шкалы (пп. 1–5)
    ('G5_P01', 'L3', 'G5_T01', 'Natural Numbers',              'Натуральные числа',              'Понятие натуральных чисел',            5, 5, 10,  1),
    ('G5_P02', 'L3', 'G5_T01', 'Segment Ray Line',             'Отрезок, луч, прямая',           'Геометрические объекты',               5, 5,  9,  2),
    ('G5_P03', 'L3', 'G5_T01', 'Scales and Number Line',       'Шкалы и числовая ось',           'Работа с числовой осью',               5, 5,  9,  3),
    ('G5_P04', 'L3', 'G5_T01', 'Comparing Natural Numbers',    'Сравнение натуральных чисел',    'Сравнение чисел',                      5, 5, 10,  4),
    ('G5_P05', 'L3', 'G5_T01', 'Rounding Natural Numbers',     'Округление натуральных чисел',   'Правила округления',                   5, 5,  9,  5),

    -- Тема 2: Сложение и вычитание (пп. 6–8)
    ('G5_P06', 'L3', 'G5_T02', 'Add Sub Natural Numbers',      'Сложение и вычитание натуральных чисел', 'Операции сложения и вычитания', 5, 5, 10,  6),
    ('G5_P07', 'L3', 'G5_T02', 'Numeric Algebraic Expressions','Числовые и буквенные выражения', 'Работа с выражениями',                 5, 5,  9,  7),
    ('G5_P08', 'L3', 'G5_T02', 'Equations',                    'Уравнения',                      'Решение простых уравнений',            5, 5, 10,  8),

    -- Тема 3: Умножение и деление (пп. 9–15)
    ('G5_P09', 'L3', 'G5_T03', 'Multiplication of Naturals',   'Умножение натуральных чисел',    'Умножение',                            5, 5, 10,  9),
    ('G5_P10', 'L3', 'G5_T03', 'Division of Naturals',         'Деление натуральных чисел',      'Деление',                              5, 5, 10, 10),
    ('G5_P11', 'L3', 'G5_T03', 'Division with Remainder',      'Деление с остатком',             'Деление с остатком',                   5, 5,  9, 11),
    ('G5_P12', 'L3', 'G5_T03', 'Simplifying Expressions',      'Упрощение выражений',            'Упрощение алгебраических выражений',   5, 5,  9, 12),
    ('G5_P13', 'L3', 'G5_T03', 'Order of Operations',          'Порядок выполнения действий',    'Приоритет операций',                   5, 5, 10, 13),
    ('G5_P14', 'L3', 'G5_T03', 'Square and Cube',              'Квадрат и куб числа',            'Степени 2 и 3',                        5, 5,  8, 14),
    ('G5_P15', 'L3', 'G5_T03', 'GCD and LCM',                  'НОК и НОД',                      'Наибольший общий делитель и наименьшее общее кратное', 5, 5, 10, 15),

    -- Тема 4: Геометрические фигуры (пп. 16–19)
    ('G5_P16', 'L3', 'G5_T04', 'Angles',                       'Углы',                            'Виды и измерение углов',              5, 5,  9, 16),
    ('G5_P17', 'L3', 'G5_T04', 'Parallel Perpendicular Lines', 'Параллельные и перпендикулярные прямые', 'Взаимное расположение прямых', 5, 5,  8, 17),
    ('G5_P18', 'L3', 'G5_T04', 'Polyline and Length',          'Ломаная и её длина',              'Работа с ломаными',                   5, 5,  8, 18),
    ('G5_P19', 'L3', 'G5_T04', 'Perimeter of Polygon',         'Периметр многоугольника',         'Вычисление периметра',                5, 5,  9, 19),

    -- Тема 5: Площади и объёмы (пп. 20–24)
    ('G5_P20', 'L3', 'G5_T05', 'Rectangle Area',               'Площадь прямоугольника',          'Формула площади',                     5, 5,  9, 20),
    ('G5_P21', 'L3', 'G5_T05', 'Composite Shape Area',         'Площадь составных фигур',         'Площадь по частям',                   5, 5,  8, 21),
    ('G5_P22', 'L3', 'G5_T05', 'Area Units',                   'Единицы измерения площади',       'Перевод единиц',                      5, 5,  8, 22),
    ('G5_P23', 'L3', 'G5_T05', 'Parallelepiped and Cube',      'Прямоугольный параллелепипед и куб','Объёмные фигуры',                   5, 5,  8, 23),
    ('G5_P24', 'L3', 'G5_T05', 'Volume',                       'Объём куба и параллелепипеда',    'Формула объёма',                       5, 5,  9, 24),

    -- Тема 6: Обыкновенные дроби (пп. 25–31)
    ('G5_P25', 'L3', 'G5_T06', 'Parts and Common Fractions',   'Доли и обыкновенные дроби',       'Понятие дроби',                       5, 5, 10, 25),
    ('G5_P26', 'L3', 'G5_T06', 'Comparing Fractions',          'Сравнение дробей',                'Сравнение обыкновенных дробей',       5, 5,  9, 26),
    ('G5_P27', 'L3', 'G5_T06', 'Proper Improper Fractions',    'Правильные и неправильные дроби', 'Классификация дробей',                5, 5,  9, 27),
    ('G5_P28', 'L3', 'G5_T06', 'Add Sub Fractions',            'Сложение и вычитание дробей',     'Операции с дробями',                  5, 5, 10, 28),
    ('G5_P29', 'L3', 'G5_T06', 'Division and Fractions',       'Деление и дроби',                 'Связь деления и дробей',              5, 5,  8, 29),
    ('G5_P30', 'L3', 'G5_T06', 'Mixed Numbers',                'Смешанные числа',                 'Работа со смешанными числами',        5, 5,  9, 30),
    ('G5_P31', 'L3', 'G5_T06', 'Add Sub Mixed Numbers',        'Сложение и вычитание смешанных чисел','Операции со смешанными числами', 5, 5,  9, 31),

    -- Тема 7: Десятичные дроби. Сложение и вычитание (пп. 32–35)
    ('G5_P32', 'L3', 'G5_T07', 'Decimal Fractions',            'Десятичные дроби',                'Понятие десятичной дроби',             5, 5, 10, 32),
    ('G5_P33', 'L3', 'G5_T07', 'Comparing Decimals',           'Сравнение десятичных дробей',     'Сравнение десятичных',                 5, 5,  9, 33),
    ('G5_P34', 'L3', 'G5_T07', 'Add Sub Decimals',             'Сложение и вычитание десятичных дробей','Операции с десятичными',         5, 5, 10, 34),
    ('G5_P35', 'L3', 'G5_T07', 'Rounding Decimals',            'Приближённое значение и округление десятичных','Округление',             5, 5,  8, 35),

    -- Тема 8: Умножение и деление десятичных (пп. 36–39)
    ('G5_P36', 'L3', 'G5_T08', 'Multiply Decimal by Natural',  'Умножение десятичной на натуральное','Умножение',                        5, 5,  9, 36),
    ('G5_P37', 'L3', 'G5_T08', 'Divide Decimal by Natural',    'Деление десятичных на натуральные','Деление',                             5, 5,  9, 37),
    ('G5_P38', 'L3', 'G5_T08', 'Multiply Decimals',            'Умножение десятичных дробей',     'Умножение десятичных',                 5, 5,  9, 38),
    ('G5_P39', 'L3', 'G5_T08', 'Divide Decimals',              'Деление десятичных дробей',       'Деление десятичных',                   5, 5,  9, 39),

    -- Тема 9: Анализ данных (пп. 40–42)
    ('G5_P40', 'L3', 'G5_T09', 'Percentages',                  'Проценты',                        'Понятие процента',                    5, 5, 10, 40),
    ('G5_P41', 'L3', 'G5_T09', 'Arithmetic Mean',              'Среднее арифметическое ряда данных','Статистические понятия',             5, 5,  8, 41),
    ('G5_P42', 'L3', 'G5_T09', 'Data Series Analysis',         'Ряды данных и их анализ',         'Размах, медиана, мода',                5, 5,  7, 42),

    -- Тема 10: Текстовые задачи (пп. 43–47)
    ('G5_P43', 'L3', 'G5_T10', 'Problems with Fractions',      'Задачи на дроби',                 'Задачи на части',                     5, 5,  9, 43),
    ('G5_P44', 'L3', 'G5_T10', 'Motion Problems',              'Задачи на движение',              'Путь, скорость, время',               5, 5,  9, 44),
    ('G5_P45', 'L3', 'G5_T10', 'Two Body Motion',              'Задачи на движение двух тел',     'Навстречу, в одном направлении',      5, 5,  8, 45),
    ('G5_P46', 'L3', 'G5_T10', 'Economic Problems',            'Задачи с экономическим содержанием','Цена, количество, стоимость',       5, 5,  8, 46),
    ('G5_P47', 'L3', 'G5_T10', 'Work Problems',                'Задачи на выполненную работу',    'Производительность',                   5, 5,  8, 47);

-- ============================================================================
-- L4: АТОМАРНЫЕ НАВЫКИ (126 навыков)
-- ============================================================================

-- ─── П.1: Натуральные числа (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S01_01', 'L4', 'G5_P01', 'Identify natural numbers',    'Определение натуральных чисел',  'Какие числа являются натуральными',          5, 5, 10, 1, 'recall', 15),
    ('G5_S01_02', 'L4', 'G5_P01', 'Read and write naturals',     'Запись и чтение натуральных чисел','Чтение и запись чисел словами',             5, 5,  9, 2, 'apply', 20),
    ('G5_S01_03', 'L4', 'G5_P01', 'Place value decomposition',   'Разрядный состав числа',          'Разложение по разрядам',                   5, 5,  9, 3, 'apply', 25),
    ('G5_S01_04', 'L4', 'G5_P01', 'Write number from words',     'Запись числа цифрами',            'Запись числа из словесного описания',      5, 5,  8, 4, 'apply', 20);

-- ─── П.2: Отрезок, луч, прямая (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S02_01', 'L4', 'G5_P02', 'Distinguish geometric objects','Различие геометрических объектов','Распознавание прямая/луч/отрезок',       5, 5, 9, 1, 'recall', 15),
    ('G5_S02_02', 'L4', 'G5_P02', 'Segment notation',            'Обозначение отрезка',             'Корректная запись обозначений',            5, 5, 8, 2, 'recall', 10),
    ('G5_S02_03', 'L4', 'G5_P02', 'Compare lengths',             'Сравнение по длине',              'Сравнение длин отрезков',                 5, 5, 8, 3, 'apply', 20);

-- ─── П.3: Шкалы и числовая ось (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S03_01', 'L4', 'G5_P03', 'Understand scale',            'Понимание шкалы',                'Что показывает одно деление',              5, 5, 8, 1, 'recall', 15),
    ('G5_S03_02', 'L4', 'G5_P03', 'Plot number on axis',         'Нахождение числа на оси',        'Отметка числа на числовой оси',            5, 5, 9, 2, 'apply', 20),
    ('G5_S03_03', 'L4', 'G5_P03', 'Read coordinate of point',    'Определение координаты точки',   'Чтение графической информации',            5, 5, 8, 3, 'apply', 20);

-- ─── П.4: Сравнение натуральных чисел (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S04_01', 'L4', 'G5_P04', 'Compare by notation',         'Сравнение по записи',            'Сравнение чисел по разрядам',              5, 5, 10, 1, 'apply', 10),
    ('G5_S04_02', 'L4', 'G5_P04', 'Use comparison signs',        'Использование знаков <, >, =',   'Символьная запись сравнения',               5, 5,  9, 2, 'apply', 10),
    ('G5_S04_03', 'L4', 'G5_P04', 'Compare multi-digit numbers', 'Сравнение многозначных чисел',   'Анализ разрядов',                          5, 5,  9, 3, 'apply', 15);

-- ─── П.5: Округление натуральных чисел (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S05_01', 'L4', 'G5_P05', 'Round to tens',               'Округление до десятков',         'Применение правила округления',            5, 5, 9, 1, 'apply', 15),
    ('G5_S05_02', 'L4', 'G5_P05', 'Round to hundreds',           'Округление до сотен',            'Анализ соседних разрядов',                 5, 5, 9, 2, 'apply', 15),
    ('G5_S05_03', 'L4', 'G5_P05', 'Identify rounding digit',     'Выбор разряда округления',       'Интерпретация результата округления',      5, 5, 8, 3, 'analyze', 20);

-- ─── П.6: Сложение и вычитание натуральных чисел (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S06_01', 'L4', 'G5_P06', 'Addition without carry',      'Сложение без перехода через разряд','Базовые вычисления',                  5, 5, 10, 1, 'apply', 15),
    ('G5_S06_02', 'L4', 'G5_P06', 'Addition with carry',         'Сложение с переходом',            'Работа с переносом',                      5, 5, 10, 2, 'apply', 25),
    ('G5_S06_03', 'L4', 'G5_P06', 'Subtraction with borrowing',  'Вычитание с занятием',            'Корректное заимствование',                5, 5,  9, 3, 'apply', 30);

-- ─── П.7: Числовые и буквенные выражения (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S07_01', 'L4', 'G5_P07', 'Identify expression type',    'Определение выражения',          'Что является числовым выражением',         5, 5, 8, 1, 'recall', 10),
    ('G5_S07_02', 'L4', 'G5_P07', 'Substitute variable value',   'Подстановка значения буквы',     'Найти значение при a=N',                   5, 5, 9, 2, 'apply', 20),
    ('G5_S07_03', 'L4', 'G5_P07', 'Write algebraic expression',  'Запись буквенного выражения',    'Составление выражения по условию',         5, 5, 9, 3, 'apply', 25);

-- ─── П.8: Уравнения (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S08_01', 'L4', 'G5_P08', 'Understand equation concept',  'Понимание уравнения',           'Что называется уравнением',                5, 5, 9, 1, 'recall', 10),
    ('G5_S08_02', 'L4', 'G5_P08', 'Solve simple equation',        'Решение простого уравнения',    'Поиск неизвестного',                       5, 5,10, 2, 'apply', 30),
    ('G5_S08_03', 'L4', 'G5_P08', 'Check equation solution',      'Проверка решения',              'Проверка подстановкой',                    5, 5, 9, 3, 'apply', 20);

-- ─── П.9: Умножение натуральных чисел (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S09_01', 'L4', 'G5_P09', 'Multiplication table',        'Табличное умножение',            'Знание таблицы умножения',                 5, 5, 10, 1, 'recall',  5),
    ('G5_S09_02', 'L4', 'G5_P09', 'Multi-digit multiplication',  'Умножение многозначных чисел',   'Алгоритм умножения в столбик',             5, 5, 10, 2, 'apply', 45),
    ('G5_S09_03', 'L4', 'G5_P09', 'Multiplication properties',   'Свойства умножения',             'Переместительное и сочетательное',         5, 5,  8, 3, 'apply', 20);

-- ─── П.10: Деление натуральных чисел (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S10_01', 'L4', 'G5_P10', 'Understanding division',      'Понимание деления',              'Смысл операции деления',                   5, 5, 9, 1, 'recall', 10),
    ('G5_S10_02', 'L4', 'G5_P10', 'Division without remainder',  'Деление без остатка',            'Вычисление частного',                      5, 5,10, 2, 'apply', 30),
    ('G5_S10_03', 'L4', 'G5_P10', 'Check division by multiply',  'Проверка деления умножением',    'Обратная операция',                        5, 5, 8, 3, 'apply', 15);

-- ─── П.11: Деление с остатком (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S11_01', 'L4', 'G5_P11', 'Determine remainder',         'Определение остатка',            'Понимание остатка от деления',             5, 5, 9, 1, 'apply', 20),
    ('G5_S11_02', 'L4', 'G5_P11', 'Write division with rem',     'Запись результата',              'Корректная запись a=b·c+d',                5, 5, 8, 2, 'apply', 20);

-- ─── П.12: Упрощение выражений (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S12_01', 'L4', 'G5_P12', 'Combine like terms',          'Сложение подобных слагаемых',    'Работа с подобными (3a+5a=8a)',            5, 5, 9, 1, 'apply', 20),
    ('G5_S12_02', 'L4', 'G5_P12', 'Simplify expression',         'Упрощение выражения',            'Алгебраические преобразования',            5, 5, 9, 2, 'apply', 30);

-- ─── П.13: Порядок выполнения действий (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S13_01', 'L4', 'G5_P13', 'Understand operation priority','Понимание приоритетов',          'Что выполняется первым',                   5, 5, 10, 1, 'recall', 10),
    ('G5_S13_02', 'L4', 'G5_P13', 'Calculate with brackets',     'Вычисление со скобками',         'Следование алгоритму порядка',             5, 5, 10, 2, 'apply', 30);

-- ─── П.14: Квадрат и куб числа (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S14_01', 'L4', 'G5_P14', 'Square concept',              'Понятие квадрата',               'Что означает n²',                          5, 5, 8, 1, 'recall', 10),
    ('G5_S14_02', 'L4', 'G5_P14', 'Calculate square and cube',   'Вычисление квадрата и куба',     'Вычисление n² и n³',                       5, 5, 8, 2, 'apply', 20);

-- ─── П.15: НОК и НОД (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S15_01', 'L4', 'G5_P15', 'Find GCD',                    'Нахождение НОД',                 'НОД через разложение на множители',        5, 5, 10, 1, 'apply', 40),
    ('G5_S15_02', 'L4', 'G5_P15', 'Find LCM',                    'Нахождение НОК',                 'НОК через разложение на множители',        5, 5, 10, 2, 'apply', 45),
    ('G5_S15_03', 'L4', 'G5_P15', 'GCD LCM relation formula',    'Связь НОД и НОК',                'Формула НОК(a,b)·НОД(a,b)=a·b',           5, 5,  9, 3, 'apply', 30);

-- ─── П.16: Углы (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S16_01', 'L4', 'G5_P16', 'Recognize angle',             'Распознавание угла',             'Что называется углом',                     5, 5, 9, 1, 'recall', 10),
    ('G5_S16_02', 'L4', 'G5_P16', 'Angle types',                 'Виды углов',                     'Острый, прямой, тупой, развёрнутый',       5, 5, 9, 2, 'recall', 10),
    ('G5_S16_03', 'L4', 'G5_P16', 'Measure angles',              'Измерение углов',                'Работа с транспортиром',                   5, 5, 8, 3, 'apply', 30);

-- ─── П.17: Параллельные и перпендикулярные прямые (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S17_01', 'L4', 'G5_P17', 'Recognize parallel lines',    'Распознавание параллельных',     'Понимание параллельности',                 5, 5, 8, 1, 'recall', 10),
    ('G5_S17_02', 'L4', 'G5_P17', 'Recognize perpendicular',     'Распознавание перпендикулярных', 'Распознавание прямого угла',               5, 5, 8, 2, 'recall', 10),
    ('G5_S17_03', 'L4', 'G5_P17', 'Notation for line relations', 'Обозначение отношений прямых',   'Математическая запись',                    5, 5, 7, 3, 'apply', 15);

-- ─── П.18: Ломаная и её длина (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S18_01', 'L4', 'G5_P18', 'Polyline concept',            'Понятие ломаной',                'Определение ломаной',                      5, 5, 8, 1, 'recall', 10),
    ('G5_S18_02', 'L4', 'G5_P18', 'Calculate polyline length',   'Подсчёт длины ломаной',          'Сложение длин звеньев',                    5, 5, 8, 2, 'apply', 20),
    ('G5_S18_03', 'L4', 'G5_P18', 'Work with scale',             'Работа с масштабом',              'Перевод длины по масштабу',                5, 5, 7, 3, 'apply', 30);

-- ─── П.19: Периметр многоугольника (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S19_01', 'L4', 'G5_P19', 'Perimeter concept',           'Понимание периметра',            'Что называется периметром',                5, 5, 9, 1, 'recall', 10),
    ('G5_S19_02', 'L4', 'G5_P19', 'Rectangle perimeter',         'Периметр прямоугольника',        'Формула P=2(a+b)',                         5, 5, 9, 2, 'apply', 15),
    ('G5_S19_03', 'L4', 'G5_P19', 'Polygon perimeter',           'Периметр произвольного многоугольника','Сумма всех сторон',                  5, 5, 8, 3, 'apply', 20);

-- ─── П.20: Площадь прямоугольника (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S20_01', 'L4', 'G5_P20', 'Area concept',                'Понятие площади',                'Что показывает площадь',                   5, 5, 9, 1, 'recall', 10),
    ('G5_S20_02', 'L4', 'G5_P20', 'Rectangle area formula',      'Формула площади прямоугольника', 'S=a×b',                                    5, 5, 9, 2, 'apply', 15),
    ('G5_S20_03', 'L4', 'G5_P20', 'Inverse area problems',       'Обратные задачи на площадь',     'Найти сторону по площади',                 5, 5, 8, 3, 'analyze', 30);

-- ─── П.21: Площадь составных фигур (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S21_01', 'L4', 'G5_P21', 'Decompose composite shape',   'Разбиение фигуры',              'Разбиение на прямоугольники',              5, 5, 8, 1, 'analyze', 25),
    ('G5_S21_02', 'L4', 'G5_P21', 'Sum partial areas',           'Нахождение площади по частям',   'Сложение площадей',                        5, 5, 8, 2, 'apply', 30),
    ('G5_S21_03', 'L4', 'G5_P21', 'Choose solution strategy',    'Выбор способа решения',          'Оптимальный метод',                        5, 5, 7, 3, 'evaluate', 30);

-- ─── П.22: Единицы измерения площади (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S22_01', 'L4', 'G5_P22', 'Know area units',             'Знание единиц площади',         'см², дм², м², км²',                        5, 5, 8, 1, 'recall', 10),
    ('G5_S22_02', 'L4', 'G5_P22', 'Convert area units',          'Перевод единиц площади',        '3 дм² = 300 см²',                          5, 5, 8, 2, 'apply', 20),
    ('G5_S22_03', 'L4', 'G5_P22', 'Choose appropriate unit',     'Выбор корректной единицы',       'Практическое применение',                  5, 5, 7, 3, 'evaluate', 15);

-- ─── П.23: Прямоугольный параллелепипед и куб (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S23_01', 'L4', 'G5_P23', 'Recognize 3D shapes',         'Распознавание тел',              'Куб, параллелепипед',                      5, 5, 8, 1, 'recall', 10),
    ('G5_S23_02', 'L4', 'G5_P23', 'Count edges faces vertices',  'Элементы тела',                  'Рёбра, грани, вершины',                    5, 5, 7, 2, 'recall', 15);

-- ─── П.24: Объём куба и параллелепипеда (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S24_01', 'L4', 'G5_P24', 'Volume concept',              'Понятие объёма',                 'Что показывает объём',                     5, 5, 8, 1, 'recall', 10),
    ('G5_S24_02', 'L4', 'G5_P24', 'Parallelepiped volume',       'Формула объёма параллелепипеда', 'V=a×b×c',                                  5, 5, 9, 2, 'apply', 20),
    ('G5_S24_03', 'L4', 'G5_P24', 'Cube volume formula',         'Объём куба',                     'V=a³',                                     5, 5, 8, 3, 'apply', 20);

-- ─── П.25: Доли и обыкновенные дроби (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S25_01', 'L4', 'G5_P25', 'Fraction concept',            'Понятие доли',                   'Что такое доля фигуры',                    5, 5, 10, 1, 'recall', 10),
    ('G5_S25_02', 'L4', 'G5_P25', 'Write fraction from picture', 'Запись дроби',                   'Связь рисунка и записи',                   5, 5,  9, 2, 'apply', 15),
    ('G5_S25_03', 'L4', 'G5_P25', 'Read fractions',              'Чтение дробей',                  'Корректное чтение дробей',                 5, 5,  8, 3, 'recall', 10);

-- ─── П.26: Сравнение дробей (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S26_01', 'L4', 'G5_P26', 'Compare same denominator',    'Сравнение с одинаковыми знаменателями','Анализ числителя',                  5, 5, 9, 1, 'apply', 10),
    ('G5_S26_02', 'L4', 'G5_P26', 'Compare same numerator',      'Сравнение с одинаковыми числителями',  'Анализ знаменателя',                5, 5, 8, 2, 'apply', 15),
    ('G5_S26_03', 'L4', 'G5_P26', 'Compare fraction with 1',     'Сравнение с единицей',                 'Аналитическое сравнение',           5, 5, 8, 3, 'analyze', 15);

-- ─── П.27: Правильные и неправильные дроби (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S27_01', 'L4', 'G5_P27', 'Classify proper improper',    'Распознавание дробей',           'Правильная или неправильная',               5, 5, 9, 1, 'recall', 10),
    ('G5_S27_02', 'L4', 'G5_P27', 'Analyze numer vs denom',      'Сравнение числителя и знаменателя','Анализ записи дроби',                    5, 5, 8, 2, 'apply', 10);

-- ─── П.28: Сложение и вычитание дробей (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S28_01', 'L4', 'G5_P28', 'Add fractions same denom',    'Сложение дробей с одинаковыми знаменателями','Применение правила',          5, 5, 10, 1, 'apply', 15),
    ('G5_S28_02', 'L4', 'G5_P28', 'Subtract fractions',          'Вычитание дробей',               'Работа с дробями',                         5, 5,  9, 2, 'apply', 15),
    ('G5_S28_03', 'L4', 'G5_P28', 'Check fraction result',       'Проверка результата',             'Самоконтроль',                             5, 5,  7, 3, 'apply', 15),
    ('G5_S28_04', 'L4', 'G5_P28', 'Add fractions diff denom',    'Сложение дробей с разными знаменателями','Приведение к общему знаменателю', 5, 5, 10, 4, 'apply', 40);

-- ─── П.29: Деление и дроби (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S29_01', 'L4', 'G5_P29', 'Fraction as division',        'Представление дроби как деления','Дробь = деление',                          5, 5, 9, 1, 'recall', 10),
    ('G5_S29_02', 'L4', 'G5_P29', 'Write division as fraction',  'Запись деления в виде дроби',    'Запись результата дробью',                  5, 5, 8, 2, 'apply', 10);

-- ─── П.30: Смешанные числа (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S30_01', 'L4', 'G5_P30', 'Mixed number concept',        'Понимание смешанного числа',     'Что означает смешанное число',              5, 5, 9, 1, 'recall', 10),
    ('G5_S30_02', 'L4', 'G5_P30', 'Convert to improper fraction', 'Перевод в неправильную дробь',  'Алгоритм перевода',                         5, 5, 9, 2, 'apply', 25);

-- ─── П.31: Сложение и вычитание смешанных чисел (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S31_01', 'L4', 'G5_P31', 'Add mixed no carry',          'Сложение без перехода',          'Сложение целых и дробных частей',           5, 5, 8, 1, 'apply', 25),
    ('G5_S31_02', 'L4', 'G5_P31', 'Add sub mixed diff denom',    'Сложение/вычитание с разными знам.','Приведение к общему знаменателю',       5, 5, 8, 2, 'apply', 45),
    ('G5_S31_03', 'L4', 'G5_P31', 'Add sub mixed with carry',    'С переходом через целое',        'Перенос единицы',                           5, 5, 7, 3, 'apply', 45);

-- ─── П.32: Десятичные дроби (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S32_01', 'L4', 'G5_P32', 'Decimal concept',             'Понимание десятичной дроби',     'Что означает десятичная дробь',              5, 5, 10, 1, 'recall', 10),
    ('G5_S32_02', 'L4', 'G5_P32', 'Read decimals',               'Чтение десятичных дробей',       'Корректное чтение',                         5, 5,  9, 2, 'recall', 10),
    ('G5_S32_03', 'L4', 'G5_P32', 'Write decimals',              'Запись десятичной дроби',        'Перевод слов в запись',                      5, 5,  9, 3, 'apply', 15),
    ('G5_S32_04', 'L4', 'G5_P32', 'Convert decimal to fraction', 'Перевод десятичной в обыкновенную','Запись через обыкновенную дробь',          5, 5,  8, 4, 'apply', 20);

-- ─── П.33: Сравнение десятичных дробей (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S33_01', 'L4', 'G5_P33', 'Compare integer parts',       'Сравнение целых частей',         'Простое сравнение',                         5, 5, 9, 1, 'apply', 10),
    ('G5_S33_02', 'L4', 'G5_P33', 'Compare decimal parts',       'Сравнение дробной части',        'Поразрядное сравнение',                     5, 5, 9, 2, 'apply', 15),
    ('G5_S33_03', 'L4', 'G5_P33', 'Use number line for decimals','Использование числовой оси',     'Геометрическое сравнение',                  5, 5, 7, 3, 'apply', 20);

-- ─── П.34: Сложение и вычитание десятичных дробей (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S34_01', 'L4', 'G5_P34', 'Add decimals same digits',    'Сложение с одинаковым числом знаков','Базовое сложение десятичных',            5, 5, 9, 1, 'apply', 15),
    ('G5_S34_02', 'L4', 'G5_P34', 'Add decimals diff digits',    'Сложение с разным числом знаков','Дописывание нулей',                         5, 5, 9, 2, 'apply', 20),
    ('G5_S34_03', 'L4', 'G5_P34', 'Subtract decimals',           'Вычитание десятичных дробей',    'Вычитание столбиком',                        5, 5, 9, 3, 'apply', 25);

-- ─── П.35: Округление десятичных дробей (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S35_01', 'L4', 'G5_P35', 'Round to tenths',             'Округление до десятых',          'Применение правила',                         5, 5, 8, 1, 'apply', 15),
    ('G5_S35_02', 'L4', 'G5_P35', 'Round to hundredths',         'Округление до сотых',            'Поразрядный анализ',                         5, 5, 8, 2, 'apply', 15),
    ('G5_S35_03', 'L4', 'G5_P35', 'Approximate calculations',    'Приближённые вычисления',        'Оценка результата',                         5, 5, 7, 3, 'apply', 20);

-- ─── П.36: Умножение десятичной на натуральное (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S36_01', 'L4', 'G5_P36', 'Multiply decimal basic',      'Умножение без переноса запятой', 'Базовое умножение',                          5, 5, 9, 1, 'apply', 15),
    ('G5_S36_02', 'L4', 'G5_P36', 'Multiply decimal with carry', 'Перенос запятой',                'Контроль положения запятой',                  5, 5, 9, 2, 'apply', 25);

-- ─── П.37: Деление десятичных на натуральные (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S37_01', 'L4', 'G5_P37', 'Divide decimal exact',        'Деление нацело',                'Точное деление',                             5, 5, 9, 1, 'apply', 20),
    ('G5_S37_02', 'L4', 'G5_P37', 'Divide decimal continued',    'Деление с продолжением',        'Продолжение деления',                        5, 5, 8, 2, 'apply', 30);

-- ─── П.38: Умножение десятичных дробей (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S38_01', 'L4', 'G5_P38', 'Multiply decimals ignore pt', 'Умножение без учёта запятой',    'Умножение как целых',                        5, 5, 9, 1, 'apply', 25),
    ('G5_S38_02', 'L4', 'G5_P38', 'Place decimal in product',    'Определение положения запятой',  'Контроль точности',                          5, 5, 9, 2, 'apply', 15);

-- ─── П.39: Деление десятичных дробей (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S39_01', 'L4', 'G5_P39', 'Divide by 10 100 1000',       'Деление на 10, 100, 1000',      'Сдвиг запятой',                               5, 5, 9, 1, 'apply', 10),
    ('G5_S39_02', 'L4', 'G5_P39', 'Divide decimal by decimal',   'Деление дроби на дробь',        'Сведение к делению на целое',                 5, 5, 8, 2, 'apply', 35);

-- ─── П.40: Проценты (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S40_01', 'L4', 'G5_P40', 'Percent concept',             'Понимание процента',            'Что означает 1%',                             5, 5, 10, 1, 'recall', 10),
    ('G5_S40_02', 'L4', 'G5_P40', 'Find percent of number',      'Нахождение процента от числа',  'Нахождение % от числа',                       5, 5, 10, 2, 'apply', 25),
    ('G5_S40_03', 'L4', 'G5_P40', 'Find number by percent',      'Нахождение числа по проценту',  'Обратная задача',                             5, 5,  9, 3, 'analyze', 35);

-- ─── П.41: Среднее арифметическое (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S41_01', 'L4', 'G5_P41', 'Mean concept',                'Понимание среднего арифметического','Определение',                              5, 5, 8, 1, 'recall', 10),
    ('G5_S41_02', 'L4', 'G5_P41', 'Calculate mean',              'Вычисление среднего',            'Сложить и разделить',                         5, 5, 8, 2, 'apply', 20);

-- ─── П.42: Ряды данных и их анализ (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S42_01', 'L4', 'G5_P42', 'Find range',                  'Определение размаха',            'Макс − Мин',                                5, 5, 7, 1, 'apply', 15),
    ('G5_S42_02', 'L4', 'G5_P42', 'Find median',                 'Нахождение медианы',             'Середина ряда',                              5, 5, 7, 2, 'apply', 20),
    ('G5_S42_03', 'L4', 'G5_P42', 'Find mode',                   'Определение моды',               'Самый частый элемент',                       5, 5, 7, 3, 'apply', 15);

-- ─── П.43: Задачи на дроби (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S43_01', 'L4', 'G5_P43', 'Find fraction of quantity',   'Нахождение дроби от величины',  'Применение к задачам',                        5, 5, 9, 1, 'apply', 25),
    ('G5_S43_02', 'L4', 'G5_P43', 'Find quantity by fraction',   'Нахождение величины по дроби',  'Обратная задача',                             5, 5, 8, 2, 'analyze', 35),
    ('G5_S43_03', 'L4', 'G5_P43', 'Find number by fraction val', 'Нахождение числа по дроби',     'Расширенная обратная задача',                 5, 5, 8, 3, 'analyze', 35);

-- ─── П.44: Задачи на движение (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S44_01', 'L4', 'G5_P44', 'Speed time distance concept', 'Формула S=V×t',                 'Связь пути, скорости и времени',              5, 5, 9, 1, 'recall', 10),
    ('G5_S44_02', 'L4', 'G5_P44', 'Find distance',               'Нахождение пути',               'Применение формулы',                          5, 5, 9, 2, 'apply', 25);

-- ─── П.45: Задачи на движение двух тел (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S45_01', 'L4', 'G5_P45', 'Motion towards each other',   'Движение навстречу',            'Скорость сближения',                          5, 5, 8, 1, 'analyze', 45),
    ('G5_S45_02', 'L4', 'G5_P45', 'Motion same direction',       'Движение в одном направлении',  'Скорость удаления',                           5, 5, 8, 2, 'analyze', 45);

-- ─── П.46: Задачи с экономическим содержанием (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S46_01', 'L4', 'G5_P46', 'Price quantity cost',         'Цена, количество, стоимость',   'Экономическая модель',                        5, 5, 8, 1, 'apply', 25),
    ('G5_S46_02', 'L4', 'G5_P46', 'Discounts and markups',       'Скидки и наценки',              'Работа с процентами в экономике',             5, 5, 7, 2, 'analyze', 35);

-- ─── П.47: Задачи на выполненную работу (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G5_S47_01', 'L4', 'G5_P47', 'Productivity concept',        'Понимание производительности',  'A=p×t',                                       5, 5, 8, 1, 'recall', 10),
    ('G5_S47_02', 'L4', 'G5_P47', 'Joint work problem',          'Совместная работа',             'Сложение производительностей',                5, 5, 7, 2, 'analyze', 45);

-- ============================================================================
-- SKILL PREREQUISITES — Граф зависимостей (5 класс)
-- ============================================================================
INSERT INTO skill_prerequisites (skill_id, prerequisite_id, dependency_type, criticality, relationship_description, weight, discovery_source)
VALUES
    -- Числа → Операции
    ('G5_S06_01', 'G5_S01_03', 'hard', 8, 'Сложение требует понимания разрядов', 1.0, 'expert'),
    ('G5_S06_02', 'G5_S06_01', 'hard', 9, 'Сложение с переходом после простого', 1.0, 'expert'),
    ('G5_S06_03', 'G5_S06_02', 'hard', 8, 'Вычитание с займом после сложения с переходом', 0.9, 'expert'),

    -- Сравнение
    ('G5_S04_03', 'G5_S04_01', 'hard', 8, 'Многозначные после простого сравнения', 1.0, 'expert'),
    ('G5_S04_02', 'G5_S04_01', 'hard', 7, 'Знаки сравнения после понимания сравнения', 0.9, 'expert'),

    -- Округление
    ('G5_S05_01', 'G5_S01_03', 'hard', 7, 'Округление требует понимания разрядов', 0.8, 'expert'),
    ('G5_S05_02', 'G5_S05_01', 'hard', 8, 'До сотен после до десятков', 1.0, 'expert'),
    ('G5_S05_03', 'G5_S05_02', 'soft', 6, 'Интерпретация после умения округлять', 0.7, 'expert'),

    -- Выражения → Уравнения
    ('G5_S07_02', 'G5_S06_01', 'hard', 7, 'Подстановка требует сложения', 0.8, 'expert'),
    ('G5_S07_03', 'G5_S07_01', 'hard', 7, 'Запись выражения после понимания', 0.9, 'expert'),
    ('G5_S08_02', 'G5_S07_02', 'hard', 9, 'Уравнения после подстановки', 1.0, 'expert'),
    ('G5_S08_03', 'G5_S08_02', 'hard', 8, 'Проверка после решения', 0.9, 'expert'),

    -- Умножение
    ('G5_S09_02', 'G5_S09_01', 'hard', 10, 'Многозначное после таблицы', 1.0, 'expert'),
    ('G5_S09_02', 'G5_S06_02', 'hard', 8, 'Умножение в столбик требует сложения с переходом', 0.9, 'expert'),
    ('G5_S09_03', 'G5_S09_01', 'soft', 6, 'Свойства после знания таблицы', 0.7, 'expert'),

    -- Деление
    ('G5_S10_02', 'G5_S09_01', 'hard', 10, 'Деление обратно умножению', 1.0, 'expert'),
    ('G5_S10_03', 'G5_S10_02', 'hard', 7, 'Проверка после деления', 0.8, 'expert'),
    ('G5_S11_01', 'G5_S10_02', 'hard', 9, 'Деление с остатком после обычного', 1.0, 'expert'),
    ('G5_S11_02', 'G5_S11_01', 'hard', 8, 'Запись после понимания остатка', 0.9, 'expert'),

    -- Упрощение
    ('G5_S12_01', 'G5_S06_01', 'hard', 7, 'Подобные требуют сложения', 0.8, 'expert'),
    ('G5_S12_02', 'G5_S12_01', 'hard', 8, 'Упрощение после подобных', 1.0, 'expert'),

    -- Порядок действий
    ('G5_S13_02', 'G5_S13_01', 'hard', 9, 'Вычисление после знания приоритетов', 1.0, 'expert'),
    ('G5_S13_02', 'G5_S09_02', 'hard', 8, 'Порядок действий требует умножения', 0.9, 'expert'),
    ('G5_S13_02', 'G5_S06_02', 'hard', 8, 'Порядок действий требует сложения', 0.9, 'expert'),

    -- Степени
    ('G5_S14_02', 'G5_S14_01', 'hard', 8, 'Вычисление после понимания', 1.0, 'expert'),
    ('G5_S14_02', 'G5_S09_01', 'hard', 9, 'Степень требует умножения', 1.0, 'expert'),

    -- НОК и НОД
    ('G5_S15_01', 'G5_S10_02', 'hard', 9, 'НОД требует деления', 1.0, 'expert'),
    ('G5_S15_01', 'G5_S09_02', 'hard', 8, 'НОД требует умножения для разложения', 0.9, 'expert'),
    ('G5_S15_02', 'G5_S15_01', 'hard', 9, 'НОК после НОД', 1.0, 'expert'),
    ('G5_S15_03', 'G5_S15_01', 'hard', 7, 'Формула связи после нахождения НОД', 0.8, 'expert'),
    ('G5_S15_03', 'G5_S15_02', 'hard', 7, 'Формула связи после нахождения НОК', 0.8, 'expert'),

    -- Периметр и площадь
    ('G5_S19_02', 'G5_S19_01', 'hard', 8, 'Формула после понятия', 1.0, 'expert'),
    ('G5_S19_02', 'G5_S06_01', 'hard', 7, 'Периметр требует сложения', 0.8, 'expert'),
    ('G5_S20_02', 'G5_S20_01', 'hard', 8, 'Формула после понятия', 1.0, 'expert'),
    ('G5_S20_02', 'G5_S09_02', 'hard', 8, 'Площадь требует умножения', 0.9, 'expert'),
    ('G5_S20_03', 'G5_S20_02', 'hard', 8, 'Обратная задача после прямой', 1.0, 'expert'),
    ('G5_S20_03', 'G5_S10_02', 'hard', 8, 'Обратная задача требует деления', 0.9, 'expert'),
    ('G5_S21_02', 'G5_S20_02', 'hard', 9, 'Составные фигуры после прямоугольника', 1.0, 'expert'),
    ('G5_S21_01', 'G5_S20_02', 'hard', 8, 'Разбиение требует знания площади', 0.9, 'expert'),

    -- Объём
    ('G5_S24_02', 'G5_S24_01', 'hard', 8, 'Формула после понятия объёма', 1.0, 'expert'),
    ('G5_S24_02', 'G5_S09_02', 'hard', 8, 'Объём требует умножения', 0.9, 'expert'),
    ('G5_S24_03', 'G5_S24_02', 'hard', 7, 'Куб как частный случай', 0.8, 'expert'),
    ('G5_S24_03', 'G5_S14_02', 'soft', 5, 'Связь V=a³ со степенью', 0.6, 'expert'),

    -- Дроби
    ('G5_S25_02', 'G5_S25_01', 'hard', 9, 'Запись дроби после понятия', 1.0, 'expert'),
    ('G5_S26_01', 'G5_S25_01', 'hard', 8, 'Сравнение после понятия дроби', 1.0, 'expert'),
    ('G5_S26_02', 'G5_S26_01', 'hard', 7, 'Одинаковые числители после знаменателей', 0.9, 'expert'),
    ('G5_S26_03', 'G5_S26_01', 'hard', 7, 'Сравнение с 1 после базового', 0.8, 'expert'),
    ('G5_S27_01', 'G5_S25_01', 'hard', 8, 'Классификация после понятия', 0.9, 'expert'),
    ('G5_S28_01', 'G5_S25_01', 'hard', 9, 'Сложение дробей после понятия', 1.0, 'expert'),
    ('G5_S28_02', 'G5_S28_01', 'hard', 8, 'Вычитание после сложения', 1.0, 'expert'),
    ('G5_S28_04', 'G5_S28_01', 'hard', 9, 'С разными знаменателями после одинаковых', 1.0, 'expert'),
    ('G5_S28_04', 'G5_S15_02', 'hard', 8, 'Общий знаменатель через НОК', 1.0, 'expert'),
    ('G5_S29_01', 'G5_S25_01', 'hard', 7, 'Связь дроби и деления после понятия', 0.8, 'expert'),
    ('G5_S29_01', 'G5_S10_02', 'hard', 7, 'Связь с делением', 0.8, 'expert'),
    ('G5_S30_01', 'G5_S27_01', 'hard', 8, 'Смешанные после неправильных дробей', 1.0, 'expert'),
    ('G5_S30_02', 'G5_S30_01', 'hard', 9, 'Перевод после понимания', 1.0, 'expert'),
    ('G5_S31_01', 'G5_S30_01', 'hard', 8, 'Операции после понятия смешанных', 1.0, 'expert'),
    ('G5_S31_01', 'G5_S28_01', 'hard', 8, 'Сложение смешанных после обычных дробей', 0.9, 'expert'),
    ('G5_S31_02', 'G5_S31_01', 'hard', 8, 'Разные знаменатели после одинаковых', 1.0, 'expert'),
    ('G5_S31_02', 'G5_S28_04', 'hard', 9, 'Требует приведения к общему знаменателю', 1.0, 'expert'),
    ('G5_S31_03', 'G5_S31_02', 'hard', 7, 'С переходом после базового', 0.9, 'expert'),

    -- Десятичные дроби
    ('G5_S32_03', 'G5_S32_01', 'hard', 8, 'Запись после понятия', 1.0, 'expert'),
    ('G5_S32_04', 'G5_S32_01', 'hard', 7, 'Перевод после понятия', 0.9, 'expert'),
    ('G5_S32_04', 'G5_S25_01', 'hard', 7, 'Перевод требует знания обыкновенных дробей', 0.8, 'expert'),
    ('G5_S33_01', 'G5_S32_01', 'hard', 8, 'Сравнение после понятия десятичных', 1.0, 'expert'),
    ('G5_S33_02', 'G5_S33_01', 'hard', 8, 'Дробная часть после целой', 1.0, 'expert'),
    ('G5_S34_01', 'G5_S32_01', 'hard', 9, 'Сложение после понятия', 1.0, 'expert'),
    ('G5_S34_01', 'G5_S06_02', 'hard', 8, 'Сложение десятичных требует сложения целых', 0.9, 'expert'),
    ('G5_S34_02', 'G5_S34_01', 'hard', 8, 'Разное число знаков после одинакового', 1.0, 'expert'),
    ('G5_S34_03', 'G5_S34_01', 'hard', 8, 'Вычитание после сложения', 0.9, 'expert'),
    ('G5_S34_03', 'G5_S06_03', 'hard', 7, 'Вычитание десятичных требует вычитания целых', 0.8, 'expert'),

    -- Округление десятичных
    ('G5_S35_01', 'G5_S32_01', 'hard', 8, 'Округление после понятия', 1.0, 'expert'),
    ('G5_S35_01', 'G5_S05_01', 'soft', 6, 'Общий принцип округления', 0.7, 'expert'),
    ('G5_S35_02', 'G5_S35_01', 'hard', 7, 'До сотых после десятых', 0.9, 'expert'),

    -- Умножение/деление десятичных
    ('G5_S36_01', 'G5_S32_01', 'hard', 8, 'Умножение после понятия десятичных', 1.0, 'expert'),
    ('G5_S36_01', 'G5_S09_02', 'hard', 9, 'Умножение десятичных требует умножения целых', 1.0, 'expert'),
    ('G5_S36_02', 'G5_S36_01', 'hard', 8, 'С переносом после базового', 1.0, 'expert'),
    ('G5_S37_01', 'G5_S32_01', 'hard', 8, 'Деление после понятия десятичных', 1.0, 'expert'),
    ('G5_S37_01', 'G5_S10_02', 'hard', 9, 'Деление десятичных требует деления целых', 1.0, 'expert'),
    ('G5_S37_02', 'G5_S37_01', 'hard', 8, 'С продолжением после нацело', 1.0, 'expert'),
    ('G5_S38_01', 'G5_S36_02', 'hard', 9, 'Десятичное на десятичное после на натуральное', 1.0, 'expert'),
    ('G5_S38_02', 'G5_S38_01', 'hard', 8, 'Положение запятой после умножения', 1.0, 'expert'),
    ('G5_S39_01', 'G5_S32_01', 'hard', 7, 'Деление на 10/100 после понятия', 0.9, 'expert'),
    ('G5_S39_02', 'G5_S37_01', 'hard', 9, 'Дробь на дробь после базового деления', 1.0, 'expert'),

    -- Проценты
    ('G5_S40_02', 'G5_S40_01', 'hard', 9, 'Процент от числа после понятия', 1.0, 'expert'),
    ('G5_S40_02', 'G5_S38_01', 'hard', 7, 'Процент требует умножения десятичных', 0.8, 'expert'),
    ('G5_S40_03', 'G5_S40_02', 'hard', 8, 'Обратная задача после прямой', 1.0, 'expert'),
    ('G5_S40_03', 'G5_S39_02', 'hard', 7, 'Обратная задача требует деления', 0.8, 'expert'),

    -- Среднее и статистика
    ('G5_S41_02', 'G5_S41_01', 'hard', 8, 'Вычисление после понятия', 1.0, 'expert'),
    ('G5_S41_02', 'G5_S06_02', 'hard', 7, 'Среднее требует сложения', 0.8, 'expert'),
    ('G5_S41_02', 'G5_S10_02', 'hard', 7, 'Среднее требует деления', 0.8, 'expert'),

    -- Текстовые задачи
    ('G5_S43_01', 'G5_S25_01', 'hard', 8, 'Задачи на дроби после понятия', 1.0, 'expert'),
    ('G5_S43_01', 'G5_S09_02', 'hard', 7, 'Дробь от числа требует умножения', 0.8, 'expert'),
    ('G5_S43_02', 'G5_S43_01', 'hard', 8, 'Обратная после прямой', 1.0, 'expert'),
    ('G5_S43_02', 'G5_S10_02', 'hard', 7, 'Обратная задача требует деления', 0.8, 'expert'),
    ('G5_S44_02', 'G5_S44_01', 'hard', 8, 'Нахождение пути после формулы', 1.0, 'expert'),
    ('G5_S44_02', 'G5_S09_02', 'hard', 7, 'S=V×t требует умножения', 0.8, 'expert'),
    ('G5_S45_01', 'G5_S44_02', 'hard', 8, 'Два тела после одного', 1.0, 'expert'),
    ('G5_S45_02', 'G5_S45_01', 'hard', 7, 'В одном направлении после навстречу', 0.9, 'expert'),
    ('G5_S46_01', 'G5_S09_02', 'hard', 7, 'Стоимость требует умножения', 0.8, 'expert'),
    ('G5_S46_02', 'G5_S40_02', 'hard', 8, 'Скидки требуют процентов', 1.0, 'expert'),
    ('G5_S47_02', 'G5_S47_01', 'hard', 8, 'Совместная после понятия', 1.0, 'expert'),
    ('G5_S47_02', 'G5_S10_02', 'hard', 7, 'Работа требует деления', 0.8, 'expert'),
    ('G5_S47_02', 'G5_S28_04', 'hard', 7, 'Совместная работа требует дробей', 0.8, 'expert');

-- ============================================================================
-- ЗАДАЧИ для 5 класса (примеры)
-- ============================================================================
INSERT INTO tasks_master (id, skill_id, question_text, question_latex, answer_type, correct_answer, difficulty, difficulty_score, irt_discrimination, irt_difficulty, irt_guessing, verification_status, verified_at, source_type)
VALUES
    ('G5_TASK_001', 'G5_S01_01', 'Какие числа из списка 0, 1, 3, 7, −2 являются натуральными?', NULL, 'open_text', '1, 3, 7', 'A', 0.15, 1.0, -1.5, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_002', 'G5_S01_02', 'Прочитайте число 4 507 и запишите его словами', NULL, 'open_text', 'четыре тысячи пятьсот семь', 'A', 0.15, 0.9, -1.5, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_003', 'G5_S01_03', 'Разложите число 3 482 по разрядам', NULL, 'open_text', '3000+400+80+2', 'A', 0.2, 1.0, -1.2, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_004', 'G5_S01_04', 'Запишите цифрами: два миллиона шестьсот сорок пять тысяч', NULL, 'exact_number', '2645000', 'A', 0.25, 1.1, -1.0, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_005', 'G5_S06_01', 'Вычислите: 234 + 145', '234 + 145', 'exact_number', '379', 'A', 0.1, 1.0, -2.0, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_006', 'G5_S06_02', 'Вычислите: 587 + 368', '587 + 368', 'exact_number', '955', 'A', 0.2, 1.1, -1.2, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_007', 'G5_S06_03', 'Вычислите: 702 − 468', '702 - 468', 'exact_number', '234', 'B', 0.4, 1.2, 0.0, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_008', 'G5_S08_02', 'Решите уравнение: x + 9 = 12', 'x + 9 = 12', 'exact_number', '3', 'A', 0.25, 1.0, -1.0, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_009', 'G5_S09_01', 'Вычислите: 7 × 8', '7 \\times 8', 'exact_number', '56', 'A', 0.1, 1.3, -2.0, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_010', 'G5_S09_02', 'Вычислите: 34 × 6', '34 \\times 6', 'exact_number', '204', 'A', 0.25, 1.1, -1.0, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_011', 'G5_S10_02', 'Вычислите: 348 : 6', '348 : 6', 'exact_number', '58', 'A', 0.3, 1.1, -0.5, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_012', 'G5_S11_01', 'Найдите остаток: 17 : 5', '17 : 5', 'exact_number', '2', 'A', 0.25, 1.0, -1.0, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_013', 'G5_S12_01', 'Упростите: 3a + 5a', '3a + 5a', 'expression', '8a', 'A', 0.2, 1.0, -1.5, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_014', 'G5_S13_02', 'Вычислите: (8 + 4) × 3', '(8 + 4) \\times 3', 'exact_number', '36', 'A', 0.25, 1.1, -1.0, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_015', 'G5_S14_02', 'Найдите 4² и 3³', '4^2 \\text{ и } 3^3', 'multiple_values', '16, 27', 'A', 0.25, 1.0, -0.8, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_016', 'G5_S15_01', 'Найдите НОД(24, 36)', '\\text{НОД}(24, 36)', 'exact_number', '12', 'B', 0.45, 1.2, 0.2, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_017', 'G5_S15_02', 'Найдите НОК(12, 18)', '\\text{НОК}(12, 18)', 'exact_number', '36', 'B', 0.5, 1.3, 0.3, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_018', 'G5_S15_03', 'Найдите НОД(a,b) и НОК(a,b), если a=15, b=20. Проверьте: НОД·НОК=a·b', NULL, 'open_text', 'НОД=5, НОК=60, 5·60=300=15·20', 'C', 0.7, 1.4, 0.8, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_019', 'G5_S26_01', 'Какая дробь больше: 3/7 или 5/7?', '\\frac{3}{7} \\text{ или } \\frac{5}{7}', 'open_text', '5/7', 'A', 0.15, 1.0, -1.5, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_020', 'G5_S28_01', 'Вычислите: 3/8 + 2/8', '\\frac{3}{8} + \\frac{2}{8}', 'fraction', '5/8', 'A', 0.2, 1.0, -1.2, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_021', 'G5_S28_04', 'Вычислите: 1/2 + 1/3', '\\frac{1}{2} + \\frac{1}{3}', 'fraction', '5/6', 'B', 0.55, 1.3, 0.5, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_022', 'G5_S30_02', 'Преобразуйте 3 2/5 в неправильную дробь', '3\\frac{2}{5}', 'fraction', '17/5', 'B', 0.45, 1.2, 0.2, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_023', 'G5_S34_01', 'Вычислите: 3,4 + 2,5', '3{,}4 + 2{,}5', 'decimal', '5.9', 'A', 0.2, 1.0, -1.2, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_024', 'G5_S34_02', 'Вычислите: 4,27 + 1,8', '4{,}27 + 1{,}8', 'decimal', '6.07', 'B', 0.4, 1.2, 0.0, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_025', 'G5_S34_03', 'Вычислите: 6,2 − 3,85', '6{,}2 - 3{,}85', 'decimal', '2.35', 'B', 0.5, 1.3, 0.3, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_026', 'G5_S36_01', 'Вычислите: 0,4 × 3', '0{,}4 \\times 3', 'decimal', '1.2', 'A', 0.2, 1.0, -1.2, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_027', 'G5_S38_01', 'Вычислите: 1,2 × 0,3', '1{,}2 \\times 0{,}3', 'decimal', '0.36', 'B', 0.5, 1.3, 0.3, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_028', 'G5_S39_01', 'Вычислите: 3,45 : 10', '3{,}45 : 10', 'decimal', '0.345', 'A', 0.15, 1.0, -1.5, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_029', 'G5_S39_02', 'Вычислите: 4,5 : 0,3', '4{,}5 : 0{,}3', 'exact_number', '15', 'C', 0.7, 1.5, 1.0, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_030', 'G5_S40_02', 'Найдите 20% от 50', '20\\% \\text{ от } 50', 'exact_number', '10', 'B', 0.4, 1.1, 0.0, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_031', 'G5_S40_03', '30% числа равны 15. Найдите число', '30\\% = 15', 'exact_number', '50', 'C', 0.65, 1.4, 0.8, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_032', 'G5_S41_02', 'Найдите среднее арифметическое: 4, 6, 8', NULL, 'exact_number', '6', 'A', 0.2, 1.0, -1.0, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_033', 'G5_S43_01', 'Найдите 2/5 от 30', '\\frac{2}{5} \\text{ от } 30', 'exact_number', '12', 'B', 0.4, 1.2, 0.2, 0.0, 'verified', NOW(), 'custom'),
    ('G5_TASK_034', 'G5_S44_02', 'Найдите путь: v=6 км/ч, t=4 ч', 'S = V \\times t', 'exact_number', '24', 'B', 0.35, 1.0, -0.2, 0.0, 'verified', NOW(), 'custom');

-- Инициализация benchmarks для 5 класса
INSERT INTO skill_benchmarks (skill_id, total_attempts, total_correct, avg_solve_time_seconds)
SELECT id, 0, 0, typical_time_seconds
FROM knowledge_hierarchy
WHERE id LIKE 'G5_%' AND level = 'L4'
ON CONFLICT (skill_id) DO NOTHING;

-- ============================================================================
-- ИТОГО
-- ============================================================================
SELECT 'Grade 5 skills loaded!' as status;
SELECT 'L1 (Domains): '  || COUNT(*) FROM knowledge_hierarchy WHERE id LIKE 'G5_%' AND level = 'L1';
SELECT 'L2 (Clusters): ' || COUNT(*) FROM knowledge_hierarchy WHERE id LIKE 'G5_%' AND level = 'L2';
SELECT 'L3 (Topics): '   || COUNT(*) FROM knowledge_hierarchy WHERE id LIKE 'G5_%' AND level = 'L3';
SELECT 'L4 (Skills): '   || COUNT(*) FROM knowledge_hierarchy WHERE id LIKE 'G5_%' AND level = 'L4';
SELECT 'Prerequisites: ' || COUNT(*) FROM skill_prerequisites WHERE skill_id LIKE 'G5_%';
SELECT 'Tasks: '          || COUNT(*) FROM tasks_master WHERE id LIKE 'G5_TASK_%';
