-- ============================================================================
-- ALGO V1 — ПОЛНАЯ ИЕРАРХИЯ 6 КЛАСС (из PDF методиста, 2025)
-- Sardor Bozorov — L1→L4 скелет навыков
-- 2 раздела, 9 тем, 47 пунктов, 159 атомарных навыков
-- ============================================================================

\encoding UTF8

-- Очистка существующих данных 6 класса (если есть)
DELETE FROM skill_prerequisites WHERE skill_id LIKE 'G6_%' OR prerequisite_id LIKE 'G6_%';
DELETE FROM skill_benchmarks WHERE skill_id LIKE 'G6_%';
DELETE FROM tasks_master WHERE skill_id LIKE 'G6_%';
DELETE FROM knowledge_hierarchy WHERE id LIKE 'G6_%';

-- ============================================================================
-- L1: РАЗДЕЛЫ (Domains) — 2 раздела
-- ============================================================================
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order)
VALUES
    ('G6_FRAC', 'L1', NULL, 'Common Fractions',    'Обыкновенные дроби',   'Раздел I. Обыкновенные дроби',   6, 6, 10, 1),
    ('G6_RAT',  'L1', NULL, 'Rational Numbers',    'Рациональные числа',   'Раздел II. Рациональные числа',  6, 6, 10, 2);

-- ============================================================================
-- L2: ТЕМЫ (Clusters) — 9 тем
-- ============================================================================
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order)
VALUES
    -- Раздел I: Обыкновенные дроби
    ('G6_T01', 'L2', 'G6_FRAC', 'Divisibility',                      'Делимость чисел',                                        'Тема 1',  6, 6, 10, 1),
    ('G6_T02', 'L2', 'G6_FRAC', 'Add Sub Fractions Diff Denom',      'Сложение и вычитание дробей с разными знаменателями',     'Тема 2',  6, 6, 10, 2),
    ('G6_T03', 'L2', 'G6_FRAC', 'Multiply Divide Fractions',         'Умножение и деление обыкновенных дробей',                 'Тема 3',  6, 6, 10, 3),
    ('G6_T04', 'L2', 'G6_FRAC', 'Ratios and Proportions',            'Отношения и пропорции',                                   'Тема 4',  6, 6,  9, 4),
    -- Раздел II: Рациональные числа
    ('G6_T05', 'L2', 'G6_RAT',  'Positive and Negative Numbers',     'Положительные и отрицательные числа',                     'Тема 5',  6, 6, 10, 5),
    ('G6_T06', 'L2', 'G6_RAT',  'Add Sub Signed Numbers',            'Сложение и вычитание положительных и отрицательных чисел', 'Тема 6',  6, 6, 10, 6),
    ('G6_T07', 'L2', 'G6_RAT',  'Multiply Divide Signed Numbers',    'Умножение и деление положительных и отрицательных чисел',  'Тема 7',  6, 6, 10, 7),
    ('G6_T08', 'L2', 'G6_RAT',  'Solving Equations',                 'Решение уравнений',                                       'Тема 8',  6, 6, 10, 8),
    ('G6_T09', 'L2', 'G6_RAT',  'Coordinates on Plane',              'Координаты на плоскости',                                 'Тема 9',  6, 6,  9, 9);

-- ============================================================================
-- L3: ПУНКТЫ (Topics) — 47 пунктов
-- ============================================================================
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order)
VALUES
    -- Тема 1: Делимость чисел (пп. 1–7)
    ('G6_P01', 'L3', 'G6_T01', 'Divisors and Multiples',       'Делители и кратные',                      'Нахождение делителей и кратных',                    6, 6, 10,  1),
    ('G6_P02', 'L3', 'G6_T01', 'Divisibility by 10 5 2',       'Признаки делимости на 10, 5, 2',          'Анализ последних цифр',                             6, 6,  9,  2),
    ('G6_P03', 'L3', 'G6_T01', 'Divisibility by 9 and 3',      'Признаки делимости на 9 и 3',             'Анализ суммы цифр',                                 6, 6,  9,  3),
    ('G6_P04', 'L3', 'G6_T01', 'Prime and Composite Numbers',  'Простые и составные числа',               'Классификация чисел по делимости',                  6, 6,  9,  4),
    ('G6_P05', 'L3', 'G6_T01', 'Prime Factorization',          'Разложение на простые множители',         'Алгоритм разложения',                               6, 6, 10,  5),
    ('G6_P06', 'L3', 'G6_T01', 'GCD Coprime Numbers',          'НОД. Взаимно простые числа',              'Наибольший общий делитель',                         6, 6, 10,  6),
    ('G6_P07', 'L3', 'G6_T01', 'LCM',                          'Наименьшее общее кратное',                'НОК через разложение',                              6, 6, 10,  7),

    -- Тема 2: Сложение и вычитание дробей с разными знаменателями (пп. 8–12)
    ('G6_P08', 'L3', 'G6_T02', 'Fundamental Property of Fraction','Основное свойство дроби',              'Умножение и деление числ. и знам.',                 6, 6,  9,  8),
    ('G6_P09', 'L3', 'G6_T02', 'Reducing Fractions',            'Сокращение дробей',                      'Деление на НОД',                                    6, 6, 10,  9),
    ('G6_P10', 'L3', 'G6_T02', 'Common Denominator',            'Приведение дробей к общему знаменателю',  'Нахождение НОК знаменателей',                       6, 6, 10, 10),
    ('G6_P11', 'L3', 'G6_T02', 'Compare Add Sub Diff Denom',    'Сравнение, сложение и вычитание дробей с разными знаменателями','Операции с дробями',  6, 6, 10, 11),
    ('G6_P12', 'L3', 'G6_T02', 'Add Sub Mixed Numbers',         'Сложение и вычитание смешанных чисел',   'Операции со смешанными числами',                    6, 6, 10, 12),

    -- Тема 3: Умножение и деление обыкновенных дробей (пп. 13–19)
    ('G6_P13', 'L3', 'G6_T03', 'Multiply Fractions',            'Умножение дробей',                       'Формула a/b × c/d = ac/bd',                         6, 6, 10, 13),
    ('G6_P14', 'L3', 'G6_T03', 'Find Fraction of Number',       'Нахождение дроби от числа',              'Часть = число × дробь',                             6, 6,  9, 14),
    ('G6_P15', 'L3', 'G6_T03', 'Distributive Property',         'Применение распределительного свойства',  'a(b+c)=ab+ac для дробей',                           6, 6,  9, 15),
    ('G6_P16', 'L3', 'G6_T03', 'Reciprocal Numbers',            'Взаимно обратные числа',                  'Произведение = 1',                                  6, 6,  8, 16),
    ('G6_P17', 'L3', 'G6_T03', 'Divide Fractions',              'Деление дробей',                          'Умножение на обратную дробь',                       6, 6, 10, 17),
    ('G6_P18', 'L3', 'G6_T03', 'Find Number by Fraction',       'Нахождение числа по его дроби',           'Обратная задача на дроби',                          6, 6,  9, 18),
    ('G6_P19', 'L3', 'G6_T03', 'Fractional Expressions',        'Дробные выражения',                       'Сложные выражения с дробями',                       6, 6,  8, 19),

    -- Тема 4: Отношения и пропорции (пп. 20–25)
    ('G6_P20', 'L3', 'G6_T04', 'Ratios',                        'Отношения',                               'Запись и упрощение отношений',                      6, 6,  9, 20),
    ('G6_P21', 'L3', 'G6_T04', 'Proportions',                   'Пропорции',                               'Основное свойство пропорции',                       6, 6, 10, 21),
    ('G6_P22', 'L3', 'G6_T04', 'Direct Inverse Proportion',     'Прямая и обратная пропорциональные зависимости','Распознавание типа зависимости',              6, 6,  9, 22),
    ('G6_P23', 'L3', 'G6_T04', 'Scale',                         'Масштаб',                                 'Работа с картами и чертежами',                      6, 6,  8, 23),
    ('G6_P24', 'L3', 'G6_T04', 'Circle Circumference and Area', 'Длина окружности и площадь круга',        'Формулы C=2πr, S=πr²',                              6, 6,  9, 24),
    ('G6_P25', 'L3', 'G6_T04', 'Sphere',                        'Шар',                                     'Элементы шара: радиус, диаметр',                    6, 6,  7, 25),

    -- Тема 5: Положительные и отрицательные числа (пп. 26–30)
    ('G6_P26', 'L3', 'G6_T05', 'Coordinates on Line',           'Координаты на прямой',                    'Числовая прямая с отрицательными',                  6, 6, 10, 26),
    ('G6_P27', 'L3', 'G6_T05', 'Opposite Numbers',              'Противоположные числа',                   'a и −a, сумма = 0',                                 6, 6,  9, 27),
    ('G6_P28', 'L3', 'G6_T05', 'Absolute Value',                'Модуль числа',                            'Расстояние от числа до 0',                          6, 6, 10, 28),
    ('G6_P29', 'L3', 'G6_T05', 'Compare Numbers',               'Сравнение чисел',                         'Сравнение на числовой прямой',                      6, 6, 10, 29),
    ('G6_P30', 'L3', 'G6_T05', 'Change of Value',               'Изменение величин',                       'Нахождение изменения',                              6, 6,  8, 30),

    -- Тема 6: Сложение и вычитание +/− чисел (пп. 31–34)
    ('G6_P31', 'L3', 'G6_T06', 'Add Using Number Line',         'Сложение чисел с помощью координатной прямой','Движение по прямой',                            6, 6,  9, 31),
    ('G6_P32', 'L3', 'G6_T06', 'Add Negative Numbers',          'Сложение отрицательных чисел',            '(−a)+(−b)=−(a+b)',                                  6, 6, 10, 32),
    ('G6_P33', 'L3', 'G6_T06', 'Add Mixed Signs',               'Сложение чисел с разными знаками',        'Анализ модулей',                                    6, 6, 10, 33),
    ('G6_P34', 'L3', 'G6_T06', 'Subtraction Signed',            'Вычитание',                               'a−b=a+(−b)',                                        6, 6, 10, 34),

    -- Тема 7: Умножение и деление +/− чисел (пп. 35–38)
    ('G6_P35', 'L3', 'G6_T07', 'Multiply Signed Numbers',       'Умножение',                               'Правила знаков при умножении',                      6, 6, 10, 35),
    ('G6_P36', 'L3', 'G6_T07', 'Divide Signed Numbers',         'Деление',                                 'Правила знаков при делении',                        6, 6, 10, 36),
    ('G6_P37', 'L3', 'G6_T07', 'Rational Numbers Concept',      'Рациональные числа',                      'Определение и представление',                       6, 6,  9, 37),
    ('G6_P38', 'L3', 'G6_T07', 'Properties of Operations',      'Свойства действий с рациональными числами','Коммутативность, ассоциативность, дистрибутивность', 6, 6,  9, 38),

    -- Тема 8: Решение уравнений (пп. 39–42)
    ('G6_P39', 'L3', 'G6_T08', 'Expand Brackets',               'Раскрытие скобок',                        'a(b+c)=ab+ac, −a(b+c)=−ab−ac',                     6, 6, 10, 39),
    ('G6_P40', 'L3', 'G6_T08', 'Coefficient',                   'Коэффициент',                             'Числовой множитель при переменной',                 6, 6,  8, 40),
    ('G6_P41', 'L3', 'G6_T08', 'Like Terms',                    'Подобные слагаемые',                      'Приведение подобных',                               6, 6,  9, 41),
    ('G6_P42', 'L3', 'G6_T08', 'Solving Equations',             'Решение уравнений',                       'Перенос слагаемых и деление',                       6, 6, 10, 42),

    -- Тема 9: Координаты на плоскости (пп. 43–47)
    ('G6_P43', 'L3', 'G6_T09', 'Perpendicular Lines',           'Перпендикулярные прямые',                 'Угол 90°, обозначение a⊥b',                         6, 6,  8, 43),
    ('G6_P44', 'L3', 'G6_T09', 'Parallel Lines',                'Параллельные прямые',                     'Не пересекаются, обозначение a∥b',                  6, 6,  8, 44),
    ('G6_P45', 'L3', 'G6_T09', 'Coordinate Plane',              'Координатная плоскость',                  'Оси Ox, Oy, точка (x;y)',                           6, 6, 10, 45),
    ('G6_P46', 'L3', 'G6_T09', 'Bar Charts',                    'Столбчатые диаграммы',                    'Чтение и построение диаграмм',                      6, 6,  7, 46),
    ('G6_P47', 'L3', 'G6_T09', 'Graphs',                        'Графики',                                 'Чтение и построение графиков',                      6, 6,  8, 47);

-- ============================================================================
-- L4: АТОМАРНЫЕ НАВЫКИ (159 навыков)
-- ============================================================================

-- ─── П.1: Делители и кратные (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S01_01', 'L4', 'G6_P01', 'Find divisors',               'Нахождение делителей числа',       'Перечислить все делители',                  6, 6, 10, 1, 'apply', 30),
    ('G6_S01_02', 'L4', 'G6_P01', 'Find multiples',              'Нахождение кратных числа',          'Запись первых N кратных',                   6, 6,  9, 2, 'apply', 20),
    ('G6_S01_03', 'L4', 'G6_P01', 'Check divisibility',          'Проверка делимости',                'Деление без остатка',                       6, 6,  9, 3, 'apply', 15);

-- ─── П.2: Признаки делимости на 10, 5, 2 (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S02_01', 'L4', 'G6_P02', 'Divisibility by 10',          'Делимость на 10',                   'Последняя цифра 0',                        6, 6, 9, 1, 'recall', 10),
    ('G6_S02_02', 'L4', 'G6_P02', 'Divisibility by 5',           'Делимость на 5',                    'Последняя цифра 0 или 5',                  6, 6, 9, 2, 'recall', 10),
    ('G6_S02_03', 'L4', 'G6_P02', 'Divisibility by 2',           'Делимость на 2',                    'Последняя цифра чётная',                   6, 6, 9, 3, 'recall', 10),
    ('G6_S02_04', 'L4', 'G6_P02', 'Apply multiple criteria',     'Применение нескольких признаков',   'Анализ последней цифры',                   6, 6, 8, 4, 'apply', 15);

-- ─── П.3: Признаки делимости на 9 и 3 (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S03_01', 'L4', 'G6_P03', 'Divisibility by 3',           'Делимость на 3',                   'Сумма цифр делится на 3',                   6, 6, 9, 1, 'apply', 15),
    ('G6_S03_02', 'L4', 'G6_P03', 'Divisibility by 9',           'Делимость на 9',                   'Сумма цифр делится на 9',                   6, 6, 9, 2, 'apply', 15),
    ('G6_S03_03', 'L4', 'G6_P03', 'Large number check',          'Работа с большой записью',          'Быстрый подсчёт суммы цифр',               6, 6, 8, 3, 'apply', 20);

-- ─── П.4: Простые и составные числа (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S04_01', 'L4', 'G6_P04', 'Identify prime number',       'Определение простого числа',        'Ровно 2 делителя',                          6, 6, 9, 1, 'recall', 10),
    ('G6_S04_02', 'L4', 'G6_P04', 'Identify composite number',   'Определение составного числа',      'Поиск делителей кроме 1 и n',               6, 6, 9, 2, 'recall', 15),
    ('G6_S04_03', 'L4', 'G6_P04', 'Classify number set',         'Классификация набора чисел',        'Анализ делителей набора',                   6, 6, 8, 3, 'analyze', 20);

-- ─── П.5: Разложение на простые множители (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S05_01', 'L4', 'G6_P05', 'Factor prime number',         'Разложение простого числа',         '13 = 13',                                   6, 6, 8, 1, 'recall', 5),
    ('G6_S05_02', 'L4', 'G6_P05', 'Factor composite number',     'Разложение составного числа',       '24 = 2×2×2×3',                              6, 6, 10, 2, 'apply', 35),
    ('G6_S05_03', 'L4', 'G6_P05', 'Write as prime powers',       'Запись в виде степеней',            '36 = 2²×3²',                                6, 6, 9, 3, 'apply', 25);

-- ─── П.6: НОД. Взаимно простые числа (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S06_01', 'L4', 'G6_P06', 'GCD by listing divisors',     'НОД по делителям',                  'Выбор наибольшего общего',                  6, 6, 9, 1, 'apply', 30),
    ('G6_S06_02', 'L4', 'G6_P06', 'GCD by factorization',        'НОД через разложение',              'По наименьшим степеням',                    6, 6, 10, 2, 'apply', 35),
    ('G6_S06_03', 'L4', 'G6_P06', 'Coprime numbers',             'Взаимно простые числа',             'Проверка НОД = 1',                          6, 6, 8, 3, 'analyze', 20);

-- ─── П.7: НОК (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S07_01', 'L4', 'G6_P07', 'LCM by listing multiples',    'НОК по кратным',                   'Поиск наименьшего общего',                  6, 6, 9, 1, 'apply', 30),
    ('G6_S07_02', 'L4', 'G6_P07', 'LCM by factorization',        'НОК через разложение',              'По наибольшим степеням',                    6, 6, 10, 2, 'apply', 40),
    ('G6_S07_03', 'L4', 'G6_P07', 'GCD LCM formula',             'Связь НОД и НОК',                   'НОД(a,b)×НОК(a,b)=a×b',                    6, 6, 9, 3, 'apply', 30);

-- ─── П.8: Основное свойство дроби (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S08_01', 'L4', 'G6_P08', 'Multiply numer and denom',    'Умножение числителя и знаменателя', 'a/b = (a×k)/(b×k)',                         6, 6, 9, 1, 'apply', 15),
    ('G6_S08_02', 'L4', 'G6_P08', 'Divide numer and denom',      'Деление числителя и знаменателя',   'Обратное преобразование',                   6, 6, 9, 2, 'apply', 15),
    ('G6_S08_03', 'L4', 'G6_P08', 'Check fraction equality',     'Проверка равенства дробей',         'Применение основного свойства',             6, 6, 8, 3, 'apply', 20);

-- ─── П.9: Сокращение дробей (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S09_01', 'L4', 'G6_P09', 'Reduce by common divisor',    'Сокращение на общий делитель',      'Деление на НОД',                            6, 6, 10, 1, 'apply', 20),
    ('G6_S09_02', 'L4', 'G6_P09', 'Full reduction',              'Полное сокращение',                 'Нахождение НОД и деление',                  6, 6, 10, 2, 'apply', 25),
    ('G6_S09_03', 'L4', 'G6_P09', 'Check irreducible fraction',  'Проверка несократимой дроби',       'Понимание взаимно простых',                 6, 6, 8, 3, 'analyze', 15);

-- ─── П.10: Приведение дробей к общему знаменателю (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S10_01', 'L4', 'G6_P10', 'Find common denominator',     'Нахождение общего знаменателя',     'НОК знаменателей',                          6, 6, 10, 1, 'apply', 20),
    ('G6_S10_02', 'L4', 'G6_P10', 'Convert fractions',           'Приведение дробей',                 'Применение основного свойства',             6, 6, 10, 2, 'apply', 25),
    ('G6_S10_03', 'L4', 'G6_P10', 'Prepare for addition',        'Подготовка к сложению',             'Приведение к знаменателю НОК',              6, 6, 9, 3, 'apply', 25),
    ('G6_S10_04', 'L4', 'G6_P10', 'Choose convenient denom',     'Выбор удобного знаменателя',        'Анализ кратных',                            6, 6, 8, 4, 'analyze', 30);

-- ─── П.11: Сравнение, сложение и вычитание дробей с разными знам. (6 навыков) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S11_01', 'L4', 'G6_P11', 'Compare via common denom',    'Сравнение через общий знаменатель', 'Нахождение НОК и сравнение',                6, 6, 9, 1, 'apply', 25),
    ('G6_S11_02', 'L4', 'G6_P11', 'Compare cross multiply',      'Сравнение перекрёстным умножением', 'ad vs bc',                                  6, 6, 8, 2, 'apply', 20),
    ('G6_S11_03', 'L4', 'G6_P11', 'Add fractions diff denom',    'Сложение дробей с разными знам.',   'Приведение и сложение',                     6, 6, 10, 3, 'apply', 30),
    ('G6_S11_04', 'L4', 'G6_P11', 'Add with simplification',     'Сложение с сокращением',            'Упрощение результата',                      6, 6, 9, 4, 'apply', 35),
    ('G6_S11_05', 'L4', 'G6_P11', 'Subtract fractions diff denom','Вычитание дробей с разными знам.', 'Приведение и вычитание',                    6, 6, 10, 5, 'apply', 30),
    ('G6_S11_06', 'L4', 'G6_P11', 'Subtract negative result',    'Получение отрицательного результата','Работа с отрицательной дробью',             6, 6, 8, 6, 'apply', 35);

-- ─── П.12: Сложение и вычитание смешанных чисел (5 навыков) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S12_01', 'L4', 'G6_P12', 'Add mixed no carry',          'Сложение без перехода',             'Сложение по частям',                        6, 6, 9, 1, 'apply', 25),
    ('G6_S12_02', 'L4', 'G6_P12', 'Add mixed with carry',        'Сложение с переходом через целое',  'Выделение целой части',                     6, 6, 9, 2, 'apply', 35),
    ('G6_S12_03', 'L4', 'G6_P12', 'Subtract mixed no borrow',    'Вычитание без заимствования',       'Вычитание по частям',                       6, 6, 9, 3, 'apply', 25),
    ('G6_S12_04', 'L4', 'G6_P12', 'Subtract mixed with borrow',  'Вычитание с заимствованием',        'Преобразование перед вычитанием',           6, 6, 8, 4, 'apply', 40),
    ('G6_S12_05', 'L4', 'G6_P12', 'Mixed with diff denominators','Смешанные с разными знаменателями', 'Приведение дробных частей',                 6, 6, 8, 5, 'apply', 45);

-- ─── П.13: Умножение дробей (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S13_01', 'L4', 'G6_P13', 'Multiply simple fractions',   'Умножение простых дробей',          'Числитель×числитель, знам.×знам.',          6, 6, 10, 1, 'apply', 15),
    ('G6_S13_02', 'L4', 'G6_P13', 'Reduce before multiply',      'Сокращение перед умножением',       'Крест-накрест сокращение',                  6, 6, 9, 2, 'apply', 25),
    ('G6_S13_03', 'L4', 'G6_P13', 'Multiply mixed numbers',      'Умножение смешанных чисел',         'Перевод в неправильную дробь',              6, 6, 9, 3, 'apply', 35);

-- ─── П.14: Нахождение дроби от числа (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S14_01', 'L4', 'G6_P14', 'Find part of integer',        'Нахождение части от целого',        'Умножение числа на дробь',                  6, 6, 9, 1, 'apply', 20),
    ('G6_S14_02', 'L4', 'G6_P14', 'Find part of fractional',     'Нахождение части от дробного числа','Дробь от дроби',                            6, 6, 8, 2, 'apply', 25),
    ('G6_S14_03', 'L4', 'G6_P14', 'Word problem on part',        'Текстовая задача на часть',         'Перевод условия в выражение',               6, 6, 8, 3, 'analyze', 35);

-- ─── П.15: Применение распределительного свойства (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S15_01', 'L4', 'G6_P15', 'Expand brackets with frac',   'Раскрытие скобок с дробью',         'a(b+c)=ab+ac',                              6, 6, 9, 1, 'apply', 25),
    ('G6_S15_02', 'L4', 'G6_P15', 'Distribute to sum of fracs',  'Распределение к сумме дробей',      'Поэтапное применение',                      6, 6, 8, 2, 'apply', 30),
    ('G6_S15_03', 'L4', 'G6_P15', 'Factor out common',           'Вынесение общего множителя',        'Обратное применение свойства',              6, 6, 8, 3, 'analyze', 30);

-- ─── П.16: Взаимно обратные числа (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S16_01', 'L4', 'G6_P16', 'Find reciprocal',             'Нахождение обратного числа',        'Переворот дроби',                           6, 6, 9, 1, 'apply', 10),
    ('G6_S16_02', 'L4', 'G6_P16', 'Check reciprocal',            'Проверка обратности',               'Произведение = 1',                          6, 6, 8, 2, 'apply', 15);

-- ─── П.17: Деление дробей (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S17_01', 'L4', 'G6_P17', 'Divide fractions',            'Деление дробей',                   'Умножение на обратную',                     6, 6, 10, 1, 'apply', 25),
    ('G6_S17_02', 'L4', 'G6_P17', 'Divide mixed numbers',        'Деление смешанных чисел',           'Перевод в неправильные дроби',              6, 6, 9, 2, 'apply', 35),
    ('G6_S17_03', 'L4', 'G6_P17', 'Divide by natural number',    'Деление на натуральное число',      'Представление n как n/1',                   6, 6, 9, 3, 'apply', 20);

-- ─── П.18: Нахождение числа по его дроби (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S18_01', 'L4', 'G6_P18', 'Find number by its fraction', 'Нахождение числа по его части',     'Составление выражения',                     6, 6, 9, 1, 'analyze', 35),
    ('G6_S18_02', 'L4', 'G6_P18', 'Verify result',               'Проверка результата',               'Обратная проверка',                         6, 6, 8, 2, 'apply', 20);

-- ─── П.19: Дробные выражения (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S19_01', 'L4', 'G6_P19', 'Evaluate fraction expression','Вычисление дробного выражения',     'Порядок действий с дробями',                6, 6, 8, 1, 'apply', 40),
    ('G6_S19_02', 'L4', 'G6_P19', 'Simplify fraction expression','Упрощение дробного выражения',      'Поэтапное применение формул',               6, 6, 8, 2, 'apply', 45);

-- ─── П.20: Отношения (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S20_01', 'L4', 'G6_P20', 'Write ratio',                 'Запись отношения',                  'a:b или a/b',                               6, 6, 9, 1, 'recall', 10),
    ('G6_S20_02', 'L4', 'G6_P20', 'Find ratio of values',        'Нахождение отношения величин',      'Деление a:b',                               6, 6, 9, 2, 'apply', 15),
    ('G6_S20_03', 'L4', 'G6_P20', 'Simplify ratio',              'Упрощение отношения',               'Деление на общий делитель',                 6, 6, 8, 3, 'apply', 20),
    ('G6_S20_04', 'L4', 'G6_P20', 'Word problem on ratio',       'Текстовая задача на отношение',     'Перевод условия в отношение',               6, 6, 8, 4, 'analyze', 30);

-- ─── П.21: Пропорции (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S21_01', 'L4', 'G6_P21', 'Check proportion',            'Проверка пропорции',                'Проверка ad=bc',                            6, 6, 9, 1, 'apply', 20),
    ('G6_S21_02', 'L4', 'G6_P21', 'Find unknown in proportion',  'Нахождение неизвестного члена',     'Применение ad=bc',                          6, 6, 10, 2, 'apply', 30),
    ('G6_S21_03', 'L4', 'G6_P21', 'Compose proportion',          'Составление пропорции по задаче',   'Построение равенства отношений',            6, 6, 9, 3, 'analyze', 35);

-- ─── П.22: Прямая и обратная пропорциональные зависимости (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S22_01', 'L4', 'G6_P22', 'Recognize direct proportion', 'Распознавание прямой зависимости',  'y=kx',                                      6, 6, 9, 1, 'analyze', 20),
    ('G6_S22_02', 'L4', 'G6_P22', 'Recognize inverse proportion','Распознавание обратной зависимости','y=k/x',                                     6, 6, 9, 2, 'analyze', 20),
    ('G6_S22_03', 'L4', 'G6_P22', 'Solve dependency problem',    'Решение задачи на зависимость',     'Применение зависимости',                    6, 6, 8, 3, 'apply', 35);

-- ─── П.23: Масштаб (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S23_01', 'L4', 'G6_P23', 'Read scale',                  'Чтение масштаба',                   'Понимание записи 1:100',                    6, 6, 8, 1, 'recall', 10),
    ('G6_S23_02', 'L4', 'G6_P23', 'Find real distance',          'Нахождение реального расстояния',   'Применение пропорции',                      6, 6, 8, 2, 'apply', 25),
    ('G6_S23_03', 'L4', 'G6_P23', 'Find map distance',           'Нахождение расстояния на карте',    'Составление пропорции',                     6, 6, 8, 3, 'apply', 25);

-- ─── П.24: Длина окружности и площадь круга (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S24_01', 'L4', 'G6_P24', 'Circumference formula',       'Нахождение длины окружности',      'C=2πr',                                     6, 6, 9, 1, 'apply', 20),
    ('G6_S24_02', 'L4', 'G6_P24', 'Circle area formula',         'Нахождение площади круга',          'S=πr²',                                     6, 6, 9, 2, 'apply', 25),
    ('G6_S24_03', 'L4', 'G6_P24', 'Inverse circle problem',      'Обратная задача',                   'Нахождение r по S или C',                   6, 6, 8, 3, 'analyze', 30);

-- ─── П.25: Шар (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S25_01', 'L4', 'G6_P25', 'Find diameter',               'Нахождение диаметра',               'd=2r',                                      6, 6, 7, 1, 'apply', 10),
    ('G6_S25_02', 'L4', 'G6_P25', 'Sphere vs circle',            'Понимание геометрической модели',   'Различие плоской и пространственной',       6, 6, 7, 2, 'recall', 10),
    ('G6_S25_03', 'L4', 'G6_P25', 'Practical sphere problem',    'Работа с практической задачей',     'd → r',                                     6, 6, 7, 3, 'apply', 15);

-- ─── П.26: Координаты на прямой (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S26_01', 'L4', 'G6_P26', 'Determine coordinate',        'Определение координаты точки',      'Понимание направления',                     6, 6, 10, 1, 'apply', 15),
    ('G6_S26_02', 'L4', 'G6_P26', 'Plot point on line',          'Построение точки на прямой',        'Отметка −4 на прямой',                      6, 6, 9, 2, 'apply', 15),
    ('G6_S26_03', 'L4', 'G6_P26', 'Distance between points',     'Расстояние между точками',          '|a−b|',                                     6, 6, 9, 3, 'apply', 20),
    ('G6_S26_04', 'L4', 'G6_P26', 'Analyze position',            'Анализ положения числа',            'Сравнение отрицательных',                   6, 6, 8, 4, 'analyze', 15);

-- ─── П.27: Противоположные числа (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S27_01', 'L4', 'G6_P27', 'Find opposite number',        'Нахождение противоположного числа', 'Смена знака',                               6, 6, 9, 1, 'recall', 5),
    ('G6_S27_02', 'L4', 'G6_P27', 'Check opposite pair',         'Проверка противоположности',        'Проверка суммы = 0',                        6, 6, 8, 2, 'apply', 10),
    ('G6_S27_03', 'L4', 'G6_P27', 'Use opposite property',       'Использование свойства',            'a + (−a) = 0',                              6, 6, 8, 3, 'apply', 10);

-- ─── П.28: Модуль числа (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S28_01', 'L4', 'G6_P28', 'Abs of positive',             'Модуль положительного числа',       '|6|=6',                                     6, 6, 9, 1, 'recall', 5),
    ('G6_S28_02', 'L4', 'G6_P28', 'Abs of negative',             'Модуль отрицательного числа',       '|−9|=9',                                    6, 6, 10, 2, 'apply', 10),
    ('G6_S28_03', 'L4', 'G6_P28', 'Expression with abs',         'Решение выражений с модулем',       '|−4|+|3|',                                  6, 6, 9, 3, 'apply', 15);

-- ─── П.29: Сравнение чисел (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S29_01', 'L4', 'G6_P29', 'Compare pos and neg',         'Сравнение положительного и отрицательного','Понимание расположения',             6, 6, 9, 1, 'recall', 5),
    ('G6_S29_02', 'L4', 'G6_P29', 'Compare two negatives',       'Сравнение отрицательных чисел',     'Ближе к нулю = больше',                     6, 6, 9, 2, 'apply', 10),
    ('G6_S29_03', 'L4', 'G6_P29', 'Compare using abs value',     'Сравнение с использованием модуля', 'Сравнение модулей',                         6, 6, 8, 3, 'apply', 15),
    ('G6_S29_04', 'L4', 'G6_P29', 'Order numbers ascending',     'Расположение в порядке возрастания','Полная сортировка',                         6, 6, 8, 4, 'analyze', 25);

-- ─── П.30: Изменение величин (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S30_01', 'L4', 'G6_P30', 'Find change of value',        'Нахождение изменения величины',     'Новое − старое',                            6, 6, 8, 1, 'apply', 20),
    ('G6_S30_02', 'L4', 'G6_P30', 'Interpret result',             'Интерпретация результата',          'Понимание уменьшения',                      6, 6, 8, 2, 'analyze', 15),
    ('G6_S30_03', 'L4', 'G6_P30', 'Sequential changes',           'Последовательные изменения',        'Сложение нескольких изменений',             6, 6, 7, 3, 'apply', 25);

-- ─── П.31: Сложение чисел на координатной прямой (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S31_01', 'L4', 'G6_P31', 'Add move right',              'Движение вправо при сложении',      'Прибавление положительного',                6, 6, 9, 1, 'apply', 10),
    ('G6_S31_02', 'L4', 'G6_P31', 'Add move left',               'Движение влево при сложении',       'Прибавление отрицательного',                6, 6, 9, 2, 'apply', 15),
    ('G6_S31_03', 'L4', 'G6_P31', 'Cross zero',                  'Переход через ноль',                '−3 + 6',                                   6, 6, 9, 3, 'apply', 15),
    ('G6_S31_04', 'L4', 'G6_P31', 'Both negative on line',       'Два отрицательных числа',           '−2 + (−4)',                                 6, 6, 8, 4, 'apply', 15);

-- ─── П.32: Сложение отрицательных чисел (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S32_01', 'L4', 'G6_P32', 'Sum two negatives',           'Сумма двух отрицательных',          '(−a)+(−b)=−(a+b)',                          6, 6, 10, 1, 'apply', 15),
    ('G6_S32_02', 'L4', 'G6_P32', 'Sum large negatives',         'Сложение больших чисел',            'Аккуратность вычислений',                   6, 6, 9, 2, 'apply', 20),
    ('G6_S32_03', 'L4', 'G6_P32', 'Check via absolute value',    'Проверка через модуль',             'Анализ модулей',                            6, 6, 8, 3, 'analyze', 15);

-- ─── П.33: Сложение чисел с разными знаками (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S33_01', 'L4', 'G6_P33', 'Positive abs larger',         'Положительное > по модулю',         '7 + (−4)',                                  6, 6, 10, 1, 'apply', 15),
    ('G6_S33_02', 'L4', 'G6_P33', 'Negative abs larger',         'Отрицательное > по модулю',         '5 + (−9)',                                  6, 6, 10, 2, 'apply', 15),
    ('G6_S33_03', 'L4', 'G6_P33', 'Equal abs values',            'Сложение с переходом через ноль',   '−6 + 6 = 0',                               6, 6, 9, 3, 'apply', 10),
    ('G6_S33_04', 'L4', 'G6_P33', 'Complex addition',            'Сложное выражение',                 '−8 + 3 + (−2)',                             6, 6, 8, 4, 'apply', 25);

-- ─── П.34: Вычитание (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S34_01', 'L4', 'G6_P34', 'Replace sub with add',        'Замена вычитания сложением',        'a−b = a+(−b)',                              6, 6, 10, 1, 'apply', 10),
    ('G6_S34_02', 'L4', 'G6_P34', 'Subtract positive',           'Вычитание положительного числа',    '4 − 7',                                    6, 6, 10, 2, 'apply', 15),
    ('G6_S34_03', 'L4', 'G6_P34', 'Subtract negative',           'Вычитание отрицательного числа',    '−6 − (−2)',                                 6, 6, 9, 3, 'apply', 15),
    ('G6_S34_04', 'L4', 'G6_P34', 'Expressions with brackets',   'Решение выражений со скобками',     '−3 − (5−8)',                                6, 6, 8, 4, 'apply', 25);

-- ─── П.35: Умножение (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S35_01', 'L4', 'G6_P35', 'Multiply two positives',      'Умножение двух положительных',      'Базовое умножение',                         6, 6, 9, 1, 'apply', 10),
    ('G6_S35_02', 'L4', 'G6_P35', 'Multiply two negatives',      'Умножение двух отрицательных',      'Правило знаков: минус×минус=плюс',          6, 6, 10, 2, 'apply', 10),
    ('G6_S35_03', 'L4', 'G6_P35', 'Multiply mixed signs',        'Умножение чисел с разными знаками', 'Определение знака результата',              6, 6, 10, 3, 'apply', 15),
    ('G6_S35_04', 'L4', 'G6_P35', 'Multiple factors',            'Несколько множителей',              'Последовательное применение',               6, 6, 8, 4, 'apply', 25);

-- ─── П.36: Деление (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S36_01', 'L4', 'G6_P36', 'Divide two negatives',        'Деление двух отрицательных',        'Правило знаков: минус:минус=плюс',          6, 6, 9, 1, 'apply', 10),
    ('G6_S36_02', 'L4', 'G6_P36', 'Divide mixed signs',          'Деление чисел с разными знаками',   'Определение знака результата',              6, 6, 10, 2, 'apply', 15),
    ('G6_S36_03', 'L4', 'G6_P36', 'Check division by multiply',  'Проверка деления умножением',       'Обратная операция',                         6, 6, 8, 3, 'apply', 15),
    ('G6_S36_04', 'L4', 'G6_P36', 'Chained division',            'Деление выражения',                 'Порядок действий',                          6, 6, 8, 4, 'apply', 25);

-- ─── П.37: Рациональные числа (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S37_01', 'L4', 'G6_P37', 'Define rational number',      'Определение рационального числа',   'Число вида a/b, b≠0',                       6, 6, 9, 1, 'recall', 10),
    ('G6_S37_02', 'L4', 'G6_P37', 'Represent as fraction',       'Представление числа дробью',        '5=5/1',                                     6, 6, 8, 2, 'apply', 10),
    ('G6_S37_03', 'L4', 'G6_P37', 'Is zero rational',            'Принадлежность числа',              '0=0/1',                                     6, 6, 8, 3, 'analyze', 10),
    ('G6_S37_04', 'L4', 'G6_P37', 'Rational in expression',      'Рациональные числа в выражении',    '−3/4 + 2',                                  6, 6, 8, 4, 'apply', 25);

-- ─── П.38: Свойства действий с рациональными числами (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S38_01', 'L4', 'G6_P38', 'Commutative property',        'Переместительное свойство',         'a+b=b+a, a×b=b×a',                         6, 6, 8, 1, 'recall', 10),
    ('G6_S38_02', 'L4', 'G6_P38', 'Associative property',        'Сочетательное свойство',            '(a+b)+c=a+(b+c)',                           6, 6, 8, 2, 'apply', 15),
    ('G6_S38_03', 'L4', 'G6_P38', 'Distributive property',       'Распределительное свойство',        'a(b+c)=ab+ac',                              6, 6, 9, 3, 'apply', 20),
    ('G6_S38_04', 'L4', 'G6_P38', 'Simplify complex expression', 'Упрощение сложного выражения',      'Порядок действий',                          6, 6, 8, 4, 'apply', 30);

-- ─── П.39: Раскрытие скобок (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S39_01', 'L4', 'G6_P39', 'Expand with plus',            'Раскрытие скобок с плюсом',         '3(x+4)',                                    6, 6, 10, 1, 'apply', 15),
    ('G6_S39_02', 'L4', 'G6_P39', 'Expand with minus',           'Раскрытие скобок с минусом',        '−2(x−5)',                                   6, 6, 10, 2, 'apply', 20);

-- ─── П.40: Коэффициент (2 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S40_01', 'L4', 'G6_P40', 'Identify coefficient',        'Определение коэффициента',          'Число перед переменной',                    6, 6, 8, 1, 'recall', 5),
    ('G6_S40_02', 'L4', 'G6_P40', 'Negative coefficient',        'Отрицательный коэффициент',         'Учёт знака',                                6, 6, 8, 2, 'apply', 10);

-- ─── П.41: Подобные слагаемые (3 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S41_01', 'L4', 'G6_P41', 'Combine like terms',          'Приведение подобных слагаемых',     'Сложение коэффициентов',                    6, 6, 10, 1, 'apply', 15),
    ('G6_S41_02', 'L4', 'G6_P41', 'Like terms diff signs',       'Подобные с разными знаками',        'Вычитание коэффициентов',                   6, 6, 9, 2, 'apply', 15),
    ('G6_S41_03', 'L4', 'G6_P41', 'Simplify expression',         'Упрощение выражения',               'Разделение постоянных и переменных',        6, 6, 9, 3, 'apply', 20);

-- ─── П.42: Решение уравнений (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S42_01', 'L4', 'G6_P42', 'Simple equation',             'Простое уравнение',                 'x+5=9',                                    6, 6, 10, 1, 'apply', 15),
    ('G6_S42_02', 'L4', 'G6_P42', 'Equation with negative',      'Уравнение с отрицательным числом',  'x−4=−2',                                   6, 6, 9, 2, 'apply', 20),
    ('G6_S42_03', 'L4', 'G6_P42', 'Equation with coefficient',   'Уравнение с коэффициентом',         '3x=12',                                    6, 6, 10, 3, 'apply', 20),
    ('G6_S42_04', 'L4', 'G6_P42', 'Equation with brackets',      'Уравнение со скобками',             '2(x−3)=8',                                 6, 6, 9, 4, 'apply', 30);

-- ─── П.43: Перпендикулярные прямые (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S43_01', 'L4', 'G6_P43', 'Define perpendicular',        'Определение перпендикулярных',      'Угол 90°',                                  6, 6, 8, 1, 'recall', 10),
    ('G6_S43_02', 'L4', 'G6_P43', 'Check with tool',             'Проверка с помощью инструмента',    'Чертёжный треугольник',                     6, 6, 7, 2, 'apply', 20),
    ('G6_S43_03', 'L4', 'G6_P43', 'Construct perpendicular',     'Построение перпендикуляра',         'Аккуратное построение',                     6, 6, 7, 3, 'apply', 30),
    ('G6_S43_04', 'L4', 'G6_P43', 'Analyze drawing',             'Анализ чертежа',                    'Понимание системы координат',               6, 6, 7, 4, 'analyze', 20);

-- ─── П.44: Параллельные прямые (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S44_01', 'L4', 'G6_P44', 'Define parallel',             'Определение параллельных',          'Не пересекаются',                           6, 6, 8, 1, 'recall', 10),
    ('G6_S44_02', 'L4', 'G6_P44', 'Construct parallel',          'Построение параллельной прямой',    'Линейка и угольник',                        6, 6, 7, 2, 'apply', 25),
    ('G6_S44_03', 'L4', 'G6_P44', 'Analyze coordinate axes',     'Анализ координатных осей',          'Понимание взаимного расположения',          6, 6, 7, 3, 'analyze', 15),
    ('G6_S44_04', 'L4', 'G6_P44', 'Rectangle parallel sides',    'Применение в задаче',               'Пары параллельных сторон',                  6, 6, 7, 4, 'analyze', 20);

-- ─── П.45: Координатная плоскость (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S45_01', 'L4', 'G6_P45', 'Read point coordinates',      'Чтение координат точки',            'Порядок (x;y)',                             6, 6, 10, 1, 'apply', 10),
    ('G6_S45_02', 'L4', 'G6_P45', 'Plot point by coordinates',   'Построение точки по координатам',   'Движение по осям',                          6, 6, 10, 2, 'apply', 20),
    ('G6_S45_03', 'L4', 'G6_P45', 'Determine quadrant',          'Определение четверти',              'Анализ знаков координат',                   6, 6, 9, 3, 'analyze', 15),
    ('G6_S45_04', 'L4', 'G6_P45', 'Distance to axis',            'Расстояние до оси',                'Использование |y| или |x|',                 6, 6, 8, 4, 'apply', 20);

-- ─── П.46: Столбчатые диаграммы (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S46_01', 'L4', 'G6_P46', 'Read chart data',             'Чтение данных диаграммы',           'Анализ столбцов',                           6, 6, 7, 1, 'apply', 15),
    ('G6_S46_02', 'L4', 'G6_P46', 'Build bar chart',             'Построение столбчатой диаграммы',   'Выбор масштаба',                            6, 6, 7, 2, 'apply', 45),
    ('G6_S46_03', 'L4', 'G6_P46', 'Compare chart values',        'Сравнение данных по диаграмме',     'Вычисление разницы',                        6, 6, 7, 3, 'analyze', 15),
    ('G6_S46_04', 'L4', 'G6_P46', 'Analyze chart data',          'Анализ данных',                     'Интерпретация информации',                  6, 6, 7, 4, 'evaluate', 20);

-- ─── П.47: Графики (4 навыка) ───
INSERT INTO knowledge_hierarchy (id, level, parent_id, name, name_ru, description, class_level_start, class_level_end, importance, sequence_order, cognitive_type, typical_time_seconds)
VALUES
    ('G6_S47_01', 'L4', 'G6_P47', 'Read graph value',            'Чтение графика',                   'Определение y при x=2',                     6, 6, 8, 1, 'apply', 15),
    ('G6_S47_02', 'L4', 'G6_P47', 'Build value table',           'Построение таблицы значений',       'Вычисление значений',                       6, 6, 8, 2, 'apply', 25),
    ('G6_S47_03', 'L4', 'G6_P47', 'Plot linear graph',           'Построение графика линейной зависимости','Нанесение точек и соединение',         6, 6, 8, 3, 'apply', 40),
    ('G6_S47_04', 'L4', 'G6_P47', 'Analyze graph direction',     'Анализ графика',                    'Определение возрастания/убывания',          6, 6, 7, 4, 'analyze', 20);

-- ============================================================================
-- SKILL PREREQUISITES — Граф зависимостей (6 класс)
-- ============================================================================
INSERT INTO skill_prerequisites (skill_id, prerequisite_id, dependency_type, criticality, relationship_description, weight, discovery_source)
VALUES
    -- Делимость
    ('G6_S01_03', 'G6_S01_01', 'hard', 8, 'Проверка делимости после делителей', 1.0, 'expert'),
    ('G6_S02_04', 'G6_S02_01', 'hard', 7, 'Несколько признаков после каждого отдельного', 0.9, 'expert'),
    ('G6_S02_04', 'G6_S02_02', 'hard', 7, 'Несколько признаков после каждого отдельного', 0.9, 'expert'),
    ('G6_S02_04', 'G6_S02_03', 'hard', 7, 'Несколько признаков после каждого отдельного', 0.9, 'expert'),
    ('G6_S03_01', 'G6_S01_01', 'soft', 6, 'Признак делимости после понятия делителей', 0.7, 'expert'),
    ('G6_S03_03', 'G6_S03_01', 'hard', 7, 'Большие числа после базового признака', 0.9, 'expert'),
    ('G6_S04_02', 'G6_S04_01', 'hard', 8, 'Составное после простого', 1.0, 'expert'),
    ('G6_S04_03', 'G6_S04_01', 'hard', 7, 'Классификация после определений', 0.9, 'expert'),
    ('G6_S04_03', 'G6_S04_02', 'hard', 7, 'Классификация после определений', 0.9, 'expert'),

    -- Разложение → НОД → НОК
    ('G6_S05_02', 'G6_S04_01', 'hard', 9, 'Разложение требует знания простых чисел', 1.0, 'expert'),
    ('G6_S05_03', 'G6_S05_02', 'hard', 8, 'Степени после разложения', 1.0, 'expert'),
    ('G6_S06_01', 'G6_S01_01', 'hard', 8, 'НОД требует нахождения делителей', 1.0, 'expert'),
    ('G6_S06_02', 'G6_S05_02', 'hard', 9, 'НОД через разложение после разложения', 1.0, 'expert'),
    ('G6_S06_03', 'G6_S06_01', 'hard', 7, 'Взаимно простые после понятия НОД', 0.8, 'expert'),
    ('G6_S07_01', 'G6_S01_02', 'hard', 8, 'НОК требует кратных', 1.0, 'expert'),
    ('G6_S07_02', 'G6_S05_02', 'hard', 9, 'НОК через разложение после разложения', 1.0, 'expert'),
    ('G6_S07_03', 'G6_S06_02', 'hard', 8, 'Связь НОД и НОК после НОД', 1.0, 'expert'),
    ('G6_S07_03', 'G6_S07_02', 'hard', 8, 'Связь НОД и НОК после НОК', 1.0, 'expert'),

    -- Дроби
    ('G6_S08_02', 'G6_S08_01', 'hard', 8, 'Деление после умножения', 1.0, 'expert'),
    ('G6_S08_03', 'G6_S08_01', 'hard', 7, 'Проверка равенства после свойства', 0.9, 'expert'),
    ('G6_S09_01', 'G6_S06_01', 'hard', 8, 'Сокращение через НОД', 1.0, 'expert'),
    ('G6_S09_02', 'G6_S09_01', 'hard', 8, 'Полное сокращение после частичного', 1.0, 'expert'),
    ('G6_S09_02', 'G6_S06_02', 'hard', 8, 'Полное сокращение через НОД', 0.9, 'expert'),
    ('G6_S09_03', 'G6_S06_03', 'hard', 7, 'Несократимость через взаимную простоту', 0.8, 'expert'),
    ('G6_S10_01', 'G6_S07_02', 'hard', 9, 'Общий знаменатель через НОК', 1.0, 'expert'),
    ('G6_S10_02', 'G6_S10_01', 'hard', 9, 'Приведение после нахождения знаменателя', 1.0, 'expert'),
    ('G6_S10_02', 'G6_S08_01', 'hard', 8, 'Приведение через основное свойство', 0.9, 'expert'),
    ('G6_S10_03', 'G6_S10_02', 'hard', 8, 'Подготовка к сложению после приведения', 1.0, 'expert'),
    ('G6_S10_04', 'G6_S10_01', 'hard', 7, 'Выбор удобного после нахождения', 0.8, 'expert'),

    -- Сравнение, сложение, вычитание дробей
    ('G6_S11_01', 'G6_S10_02', 'hard', 9, 'Сравнение через приведение', 1.0, 'expert'),
    ('G6_S11_03', 'G6_S10_03', 'hard', 9, 'Сложение после подготовки', 1.0, 'expert'),
    ('G6_S11_04', 'G6_S11_03', 'hard', 8, 'С сокращением после сложения', 1.0, 'expert'),
    ('G6_S11_04', 'G6_S09_01', 'hard', 7, 'Сокращение результата', 0.8, 'expert'),
    ('G6_S11_05', 'G6_S10_03', 'hard', 9, 'Вычитание после подготовки', 1.0, 'expert'),
    ('G6_S11_06', 'G6_S11_05', 'hard', 7, 'Отрицательный результат после вычитания', 0.8, 'expert'),

    -- Смешанные числа
    ('G6_S12_02', 'G6_S12_01', 'hard', 8, 'С переходом после простого сложения', 1.0, 'expert'),
    ('G6_S12_04', 'G6_S12_03', 'hard', 8, 'С заимствованием после простого вычитания', 1.0, 'expert'),
    ('G6_S12_05', 'G6_S12_02', 'hard', 8, 'Разные знаменатели после одинаковых', 1.0, 'expert'),
    ('G6_S12_05', 'G6_S10_02', 'hard', 9, 'Требует приведения к общему знаменателю', 1.0, 'expert'),

    -- Умножение и деление дробей
    ('G6_S13_02', 'G6_S13_01', 'hard', 8, 'Сокращение после базового умножения', 1.0, 'expert'),
    ('G6_S13_03', 'G6_S13_01', 'hard', 8, 'Смешанные после простых дробей', 1.0, 'expert'),
    ('G6_S14_01', 'G6_S13_01', 'hard', 9, 'Дробь от числа через умножение', 1.0, 'expert'),
    ('G6_S14_02', 'G6_S14_01', 'hard', 8, 'Дробное число после целого', 1.0, 'expert'),
    ('G6_S14_03', 'G6_S14_01', 'hard', 7, 'Задача после вычисления', 0.8, 'expert'),
    ('G6_S15_01', 'G6_S13_01', 'hard', 8, 'Распределительное свойство через умножение', 1.0, 'expert'),
    ('G6_S15_02', 'G6_S15_01', 'hard', 7, 'К сумме дробей после базового', 0.9, 'expert'),
    ('G6_S15_03', 'G6_S15_01', 'hard', 7, 'Обратное после прямого', 0.8, 'expert'),
    ('G6_S16_01', 'G6_S13_01', 'hard', 7, 'Обратное число через умножение', 0.8, 'expert'),
    ('G6_S17_01', 'G6_S16_01', 'hard', 9, 'Деление через обратное число', 1.0, 'expert'),
    ('G6_S17_01', 'G6_S13_01', 'hard', 9, 'Деление через умножение', 1.0, 'expert'),
    ('G6_S17_02', 'G6_S17_01', 'hard', 8, 'Смешанные после простых', 1.0, 'expert'),
    ('G6_S17_03', 'G6_S17_01', 'hard', 7, 'На натуральное после на дробь', 0.8, 'expert'),
    ('G6_S18_01', 'G6_S17_01', 'hard', 8, 'Обратная задача через деление', 1.0, 'expert'),
    ('G6_S18_02', 'G6_S18_01', 'hard', 7, 'Проверка после нахождения', 0.8, 'expert'),
    ('G6_S19_01', 'G6_S13_01', 'hard', 8, 'Дробные выражения используют умножение', 0.9, 'expert'),
    ('G6_S19_01', 'G6_S17_01', 'hard', 8, 'Дробные выражения используют деление', 0.9, 'expert'),
    ('G6_S19_02', 'G6_S19_01', 'hard', 8, 'Упрощение после вычисления', 1.0, 'expert'),

    -- Отношения и пропорции
    ('G6_S20_02', 'G6_S20_01', 'hard', 8, 'Нахождение после записи', 1.0, 'expert'),
    ('G6_S20_03', 'G6_S20_02', 'hard', 7, 'Упрощение после нахождения', 0.9, 'expert'),
    ('G6_S20_04', 'G6_S20_02', 'hard', 7, 'Задача после базовых умений', 0.8, 'expert'),
    ('G6_S21_01', 'G6_S20_01', 'hard', 8, 'Пропорция — равенство двух отношений', 1.0, 'expert'),
    ('G6_S21_02', 'G6_S21_01', 'hard', 9, 'Нахождение x после проверки', 1.0, 'expert'),
    ('G6_S21_03', 'G6_S21_02', 'hard', 8, 'Составление после решения', 0.9, 'expert'),
    ('G6_S22_01', 'G6_S21_01', 'hard', 7, 'Прямая зависимость после пропорций', 0.8, 'expert'),
    ('G6_S22_02', 'G6_S22_01', 'hard', 7, 'Обратная после прямой', 0.9, 'expert'),
    ('G6_S22_03', 'G6_S22_02', 'hard', 8, 'Решение после распознавания', 1.0, 'expert'),
    ('G6_S23_02', 'G6_S23_01', 'hard', 8, 'Нахождение после чтения', 1.0, 'expert'),
    ('G6_S23_02', 'G6_S21_02', 'hard', 7, 'Масштаб через пропорции', 0.8, 'expert'),
    ('G6_S23_03', 'G6_S23_02', 'hard', 7, 'Обратная задача после прямой', 0.9, 'expert'),
    ('G6_S24_02', 'G6_S24_01', 'hard', 8, 'Площадь после окружности', 1.0, 'expert'),
    ('G6_S24_03', 'G6_S24_02', 'hard', 8, 'Обратная задача после прямой', 1.0, 'expert'),
    ('G6_S25_03', 'G6_S25_01', 'hard', 7, 'Практика после формулы', 0.8, 'expert'),

    -- Положительные и отрицательные числа
    ('G6_S26_02', 'G6_S26_01', 'hard', 8, 'Построение после определения', 1.0, 'expert'),
    ('G6_S26_03', 'G6_S26_01', 'hard', 8, 'Расстояние после координат', 1.0, 'expert'),
    ('G6_S26_04', 'G6_S26_01', 'hard', 7, 'Анализ после базовых умений', 0.8, 'expert'),
    ('G6_S27_02', 'G6_S27_01', 'hard', 7, 'Проверка после нахождения', 0.9, 'expert'),
    ('G6_S27_03', 'G6_S27_02', 'hard', 7, 'Свойство после проверки', 0.8, 'expert'),
    ('G6_S28_02', 'G6_S28_01', 'hard', 8, 'Отрицательное после положительного', 1.0, 'expert'),
    ('G6_S28_03', 'G6_S28_02', 'hard', 8, 'Выражения после базового модуля', 1.0, 'expert'),
    ('G6_S29_02', 'G6_S29_01', 'hard', 8, 'Два отрицательных после основного', 1.0, 'expert'),
    ('G6_S29_03', 'G6_S28_02', 'hard', 7, 'Сравнение через модуль', 0.8, 'expert'),
    ('G6_S29_04', 'G6_S29_02', 'hard', 8, 'Сортировка после сравнения', 1.0, 'expert'),
    ('G6_S30_01', 'G6_S26_01', 'hard', 7, 'Изменение на прямой', 0.8, 'expert'),
    ('G6_S30_02', 'G6_S30_01', 'hard', 7, 'Интерпретация после вычисления', 0.9, 'expert'),
    ('G6_S30_03', 'G6_S30_01', 'hard', 7, 'Последовательные после одного', 0.8, 'expert'),

    -- Сложение и вычитание +/−
    ('G6_S31_02', 'G6_S31_01', 'hard', 8, 'Влево после вправо', 1.0, 'expert'),
    ('G6_S31_03', 'G6_S31_02', 'hard', 8, 'Через ноль после движения', 1.0, 'expert'),
    ('G6_S31_04', 'G6_S31_02', 'hard', 7, 'Два отрицательных после базового', 0.9, 'expert'),
    ('G6_S32_01', 'G6_S31_04', 'hard', 8, 'Формула после прямой', 1.0, 'expert'),
    ('G6_S32_02', 'G6_S32_01', 'hard', 7, 'Большие числа после базовых', 0.9, 'expert'),
    ('G6_S32_03', 'G6_S32_01', 'hard', 7, 'Проверка через модуль после суммирования', 0.8, 'expert'),
    ('G6_S33_01', 'G6_S31_03', 'hard', 9, 'Разные знаки после прямой', 1.0, 'expert'),
    ('G6_S33_01', 'G6_S28_02', 'hard', 8, 'Разные знаки через модуль', 0.9, 'expert'),
    ('G6_S33_02', 'G6_S33_01', 'hard', 8, 'Отрицательный больше после положительного', 1.0, 'expert'),
    ('G6_S33_03', 'G6_S33_01', 'hard', 7, 'Равные модули после базового', 0.8, 'expert'),
    ('G6_S33_04', 'G6_S33_02', 'hard', 7, 'Сложное после базового', 0.8, 'expert'),
    ('G6_S34_01', 'G6_S27_01', 'hard', 8, 'Вычитание через противоположное', 1.0, 'expert'),
    ('G6_S34_02', 'G6_S34_01', 'hard', 8, 'Положительное после правила', 1.0, 'expert'),
    ('G6_S34_03', 'G6_S34_01', 'hard', 8, 'Отрицательное после правила', 1.0, 'expert'),
    ('G6_S34_04', 'G6_S34_03', 'hard', 8, 'Со скобками после базового', 1.0, 'expert'),

    -- Умножение и деление +/−
    ('G6_S35_02', 'G6_S35_01', 'hard', 8, 'Два отрицательных после положительных', 1.0, 'expert'),
    ('G6_S35_03', 'G6_S35_02', 'hard', 8, 'Разные знаки после одинаковых', 1.0, 'expert'),
    ('G6_S35_04', 'G6_S35_03', 'hard', 7, 'Несколько множителей после пар', 0.8, 'expert'),
    ('G6_S36_01', 'G6_S35_02', 'hard', 8, 'Деление аналогично умножению', 1.0, 'expert'),
    ('G6_S36_02', 'G6_S36_01', 'hard', 8, 'Разные знаки после одинаковых', 1.0, 'expert'),
    ('G6_S36_03', 'G6_S36_02', 'hard', 7, 'Проверка после деления', 0.8, 'expert'),
    ('G6_S36_04', 'G6_S36_02', 'hard', 7, 'Цепочка после базового', 0.8, 'expert'),
    ('G6_S37_02', 'G6_S37_01', 'hard', 7, 'Представление после определения', 0.9, 'expert'),
    ('G6_S37_04', 'G6_S37_02', 'hard', 7, 'Выражение после представления', 0.8, 'expert'),
    ('G6_S38_02', 'G6_S38_01', 'hard', 7, 'Сочетательное после переместительного', 0.9, 'expert'),
    ('G6_S38_03', 'G6_S38_02', 'hard', 7, 'Распределительное после сочетательного', 0.9, 'expert'),
    ('G6_S38_04', 'G6_S38_03', 'hard', 8, 'Сложное после свойств', 1.0, 'expert'),

    -- Уравнения
    ('G6_S39_02', 'G6_S39_01', 'hard', 8, 'С минусом после плюса', 1.0, 'expert'),
    ('G6_S39_02', 'G6_S38_03', 'hard', 7, 'Раскрытие использует распределительное', 0.8, 'expert'),
    ('G6_S40_02', 'G6_S40_01', 'hard', 7, 'Отрицательный после положительного', 0.9, 'expert'),
    ('G6_S41_01', 'G6_S40_01', 'hard', 8, 'Подобные слагаемые после коэффициента', 1.0, 'expert'),
    ('G6_S41_02', 'G6_S41_01', 'hard', 8, 'С разными знаками после базового', 1.0, 'expert'),
    ('G6_S41_03', 'G6_S41_02', 'hard', 7, 'Упрощение после подобных', 0.9, 'expert'),
    ('G6_S42_02', 'G6_S42_01', 'hard', 8, 'С отрицательным после простого', 1.0, 'expert'),
    ('G6_S42_03', 'G6_S42_01', 'hard', 8, 'С коэффициентом после простого', 1.0, 'expert'),
    ('G6_S42_04', 'G6_S42_03', 'hard', 8, 'Со скобками после коэффициента', 1.0, 'expert'),
    ('G6_S42_04', 'G6_S39_01', 'hard', 8, 'Уравнение со скобками требует раскрытия', 1.0, 'expert'),

    -- Координатная плоскость
    ('G6_S45_02', 'G6_S45_01', 'hard', 8, 'Построение после чтения', 1.0, 'expert'),
    ('G6_S45_03', 'G6_S45_01', 'hard', 7, 'Четверть после координат', 0.9, 'expert'),
    ('G6_S45_04', 'G6_S45_03', 'hard', 7, 'Расстояние после четверти', 0.8, 'expert'),
    ('G6_S45_04', 'G6_S28_02', 'hard', 7, 'Расстояние через модуль', 0.8, 'expert'),

    -- Графики
    ('G6_S47_02', 'G6_S47_01', 'hard', 8, 'Таблица после чтения', 1.0, 'expert'),
    ('G6_S47_03', 'G6_S47_02', 'hard', 8, 'Построение после таблицы', 1.0, 'expert'),
    ('G6_S47_03', 'G6_S45_02', 'hard', 8, 'График требует построения точек', 1.0, 'expert'),
    ('G6_S47_04', 'G6_S47_03', 'hard', 7, 'Анализ после построения', 0.8, 'expert'),

    -- Межтемные зависимости (5 класс → 6 класс)
    ('G6_S01_01', 'G5_S10_02', 'hard', 8, 'Делители требуют деления (5 класс)', 1.0, 'expert'),
    ('G6_S08_01', 'G5_S25_01', 'hard', 8, 'Основное свойство дроби (от 5 класса)', 1.0, 'expert'),
    ('G6_S11_03', 'G5_S28_04', 'hard', 7, 'Сложение дробей (от 5 класса)', 0.8, 'expert'),
    ('G6_S26_01', 'G5_S03_02', 'hard', 7, 'Координаты на прямой (от 5 класса)', 0.8, 'expert');

-- ============================================================================
-- ЗАДАЧИ для 6 класса (примеры)
-- ============================================================================
INSERT INTO tasks_master (id, skill_id, question_text, question_latex, answer_type, correct_answer, difficulty, difficulty_score, irt_discrimination, irt_difficulty, irt_guessing, verification_status, verified_at, source_type)
VALUES
    ('G6_TASK_001', 'G6_S01_01', 'Найдите все делители числа 18', NULL, 'open_text', '1, 2, 3, 6, 9, 18', 'A', 0.2, 1.0, -1.2, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_002', 'G6_S01_02', 'Запишите первые пять кратных числа 6', NULL, 'open_text', '6, 12, 18, 24, 30', 'A', 0.15, 0.9, -1.5, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_003', 'G6_S02_01', 'Делится ли 450 на 10?', NULL, 'open_text', 'Да', 'A', 0.1, 1.0, -2.0, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_004', 'G6_S03_01', 'Делится ли 123 на 3?', NULL, 'open_text', 'Да, т.к. 1+2+3=6 делится на 3', 'B', 0.35, 1.1, -0.2, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_005', 'G6_S04_01', 'Является ли 17 простым числом?', NULL, 'open_text', 'Да', 'A', 0.15, 1.0, -1.5, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_006', 'G6_S05_02', 'Разложите 24 на простые множители', NULL, 'expression', '2×2×2×3', 'B', 0.4, 1.2, 0.0, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_007', 'G6_S06_02', 'Найдите НОД(24, 36)', '\\text{НОД}(24,36)', 'exact_number', '12', 'B', 0.45, 1.2, 0.2, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_008', 'G6_S07_02', 'Найдите НОК(12, 18)', '\\text{НОК}(12,18)', 'exact_number', '36', 'B', 0.5, 1.3, 0.3, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_009', 'G6_S09_01', 'Сократите дробь 8/12', '\\frac{8}{12}', 'fraction', '2/3', 'B', 0.35, 1.1, -0.2, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_010', 'G6_S11_03', 'Вычислите: 1/4 + 1/6', '\\frac{1}{4}+\\frac{1}{6}', 'fraction', '5/12', 'B', 0.5, 1.3, 0.3, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_011', 'G6_S13_01', 'Вычислите: 2/3 × 3/5', '\\frac{2}{3}\\times\\frac{3}{5}', 'fraction', '2/5', 'A', 0.2, 1.0, -1.0, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_012', 'G6_S17_01', 'Вычислите: 2/3 : 4/5', '\\frac{2}{3}:\\frac{4}{5}', 'fraction', '5/6', 'B', 0.5, 1.3, 0.3, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_013', 'G6_S21_02', 'Найдите x из пропорции 3/5 = x/10', '\\frac{3}{5}=\\frac{x}{10}', 'exact_number', '6', 'B', 0.5, 1.2, 0.2, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_014', 'G6_S24_01', 'Найдите длину окружности при r = 5', 'C=2\\pi r', 'decimal', '31.4', 'B', 0.45, 1.1, 0.0, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_015', 'G6_S26_01', 'Укажите координату точки на 3 деления левее нуля', NULL, 'exact_number', '-3', 'A', 0.15, 1.0, -1.5, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_016', 'G6_S28_02', 'Найдите |−9|', '|-9|', 'exact_number', '9', 'A', 0.1, 1.0, -2.0, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_017', 'G6_S29_02', 'Что больше: −3 или −7?', NULL, 'exact_number', '-3', 'B', 0.35, 1.1, -0.2, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_018', 'G6_S32_01', 'Вычислите: −5 + (−3)', '-5+(-3)', 'exact_number', '-8', 'A', 0.2, 1.0, -1.0, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_019', 'G6_S33_01', 'Вычислите: 7 + (−4)', '7+(-4)', 'exact_number', '3', 'B', 0.35, 1.1, -0.2, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_020', 'G6_S34_01', 'Вычислите: 5 − (−3)', '5-(-3)', 'exact_number', '8', 'A', 0.2, 1.0, -1.0, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_021', 'G6_S35_02', 'Вычислите: (−5) × (−3)', '(-5)\\times(-3)', 'exact_number', '15', 'A', 0.2, 1.0, -1.0, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_022', 'G6_S35_03', 'Вычислите: 7 × (−4)', '7\\times(-4)', 'exact_number', '-28', 'B', 0.35, 1.1, -0.2, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_023', 'G6_S36_02', 'Вычислите: 15 : (−5)', '15:(-5)', 'exact_number', '-3', 'B', 0.35, 1.1, -0.2, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_024', 'G6_S39_01', 'Раскройте скобки: 3(x + 4)', '3(x+4)', 'expression', '3x + 12', 'B', 0.4, 1.2, 0.0, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_025', 'G6_S41_01', 'Упростите: 4x + 2x', '4x+2x', 'expression', '6x', 'A', 0.15, 1.0, -1.5, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_026', 'G6_S42_01', 'Решите уравнение: x + 5 = 9', 'x+5=9', 'exact_number', '4', 'A', 0.2, 1.0, -1.2, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_027', 'G6_S42_03', 'Решите уравнение: 3x = 12', '3x=12', 'exact_number', '4', 'B', 0.35, 1.1, -0.2, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_028', 'G6_S42_04', 'Решите уравнение: 2(x − 3) = 8', '2(x-3)=8', 'exact_number', '7', 'C', 0.6, 1.4, 0.7, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_029', 'G6_S45_01', 'Назовите координаты точки (3; −2)', NULL, 'open_text', 'x=3, y=−2', 'A', 0.15, 1.0, -1.5, 0.0, 'verified', NOW(), 'custom'),
    ('G6_TASK_030', 'G6_S45_03', 'В какой четверти находится точка (2; −5)?', NULL, 'open_text', 'IV четверть', 'B', 0.4, 1.2, 0.0, 0.0, 'verified', NOW(), 'custom');

-- Инициализация benchmarks для 6 класса
INSERT INTO skill_benchmarks (skill_id, total_attempts, total_correct, avg_solve_time_seconds)
SELECT id, 0, 0, typical_time_seconds
FROM knowledge_hierarchy
WHERE id LIKE 'G6_%' AND level = 'L4'
ON CONFLICT (skill_id) DO NOTHING;

-- ============================================================================
-- ИТОГО
-- ============================================================================
SELECT 'Grade 6 skills loaded!' as status;
SELECT 'L1 (Domains): '  || COUNT(*) FROM knowledge_hierarchy WHERE id LIKE 'G6_%' AND level = 'L1';
SELECT 'L2 (Clusters): ' || COUNT(*) FROM knowledge_hierarchy WHERE id LIKE 'G6_%' AND level = 'L2';
SELECT 'L3 (Topics): '   || COUNT(*) FROM knowledge_hierarchy WHERE id LIKE 'G6_%' AND level = 'L3';
SELECT 'L4 (Skills): '   || COUNT(*) FROM knowledge_hierarchy WHERE id LIKE 'G6_%' AND level = 'L4';
SELECT 'Prerequisites: ' || COUNT(*) FROM skill_prerequisites WHERE skill_id LIKE 'G6_%';
SELECT 'Tasks: '          || COUNT(*) FROM tasks_master WHERE id LIKE 'G6_TASK_%';
