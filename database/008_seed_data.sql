-- ============================================================================
-- ALGO V1 - SEED DATA (Reference / Textbook metadata only)
-- Демо-данные для тестирования системы
--
-- ВНИМАНИЕ: Параллельная иерархия ARITH/ALG/GEOM УДАЛЕНА.
-- Вся предметная иерархия (L1→L4) теперь в database/skeletons/grade_XX.sql
-- Этот файл содержит только:
--   1. Учебники (textbooks)
--   2. Структуру учебника (textbook_structure)
--
-- ВАЖНО: Файл переименован из 004 → 008, чтобы выполняться ПОСЛЕ
-- 006_orm_sync_migration.sql (добавляет IRT-колонки, weight, discovery_source)
-- ============================================================================

-- ============================================================================
-- 1. TEXTBOOKS - Учебники
-- ============================================================================

-- ПРИМЕЧАНИЕ: 002_presentation_schema.sql уже вставляет учебники с другими UUID.
-- Эти записи используют собственные UUID для привязки textbook_structure ниже.
INSERT INTO textbooks (textbook_id, isbn, title, subtitle, edition, authors, publisher, subject, class_level, country, language, digitization_status, digitization_progress, tasks_extracted)
VALUES 
    ('a1b2c3d4-e5f6-7890-abcd-ef1234567890', '978-5-346-01234-5', 'Математика 5 класс', 'Учебник для общеобразовательных учреждений', '2023', ARRAY['Виленкин Н.Я.', 'Жохов В.И.', 'Чесноков А.С.'], 'Мнемозина', 'mathematics', 5, 'RU', 'ru', 'verified', 100.00, 1500),
    ('b2c3d4e5-f6a7-8901-bcde-f12345678901', '978-5-346-01235-2', 'Математика 6 класс', 'Учебник для общеобразовательных учреждений', '2023', ARRAY['Виленкин Н.Я.', 'Жохов В.И.', 'Чесноков А.С.'], 'Мнемозина', 'mathematics', 6, 'RU', 'ru', 'verified', 100.00, 1600),
    ('c3d4e5f6-a7b8-9012-cdef-123456789012', '978-5-09-012345-6', 'Алгебра 7 класс', 'Учебник', '2023', ARRAY['Макарычев Ю.Н.', 'Миндюк Н.Г.', 'Нешков К.И.'], 'Просвещение', 'mathematics', 7, 'RU', 'ru', 'in_progress', 75.50, 850),
    ('d4e5f6a7-b8c9-0123-defa-234567890123', '978-5-09-012346-3', 'Алгебра 8 класс', 'Учебник', '2023', ARRAY['Макарычев Ю.Н.', 'Миндюк Н.Г.', 'Нешков К.И.'], 'Просвещение', 'mathematics', 8, 'RU', 'ru', 'pending', 0.00, 0)
ON CONFLICT (textbook_id) DO NOTHING;

-- ============================================================================
-- 2. TEXTBOOK STRUCTURE - Структура учебника 5 класс
-- Маппинг на G5_ скелет из database/skeletons/grade_05.sql
-- ============================================================================
INSERT INTO textbook_structure (textbook_id, parent_id, node_type, display_number, display_name, sort_order, mapped_topic_id, mapping_confidence)
VALUES 
    ('a1b2c3d4-e5f6-7890-abcd-ef1234567890', NULL, 'chapter', 'Раздел I', 'Натуральные числа', 1, 'G5_NAT', 0.98),
    ('a1b2c3d4-e5f6-7890-abcd-ef1234567890', NULL, 'chapter', 'Раздел II', 'Дробные числа', 2, 'G5_FRAC', 0.98)
ON CONFLICT DO NOTHING;

-- ============================================================================
-- DONE
-- ============================================================================

SELECT 'Seed data loaded successfully!' as status;
SELECT 'Textbooks: ' || COUNT(*) as count FROM textbooks;
