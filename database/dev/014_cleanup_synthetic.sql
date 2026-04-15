-- =================================================================
-- ALGO V1 — Cleanup ALL Synthetic Grade 5 Data
-- Run this BEFORE loading real digitized data
-- =================================================================

BEGIN;

-- 1. Answer events (synthetic students)
DELETE FROM answer_events WHERE client_version = 'synthetic-1.0';

-- 2. Student skill states (synthetic students)
DELETE FROM student_skill_states WHERE student_id IN ('00000000-0000-4000-a000-000000000001', '00000000-0000-4000-a000-000000000002', '00000000-0000-4000-a000-000000000003', '00000000-0000-4000-a000-000000000004', '00000000-0000-4000-a000-000000000005', '00000000-0000-4000-a000-000000000006', '00000000-0000-4000-a000-000000000007', '00000000-0000-4000-a000-000000000008', '00000000-0000-4000-a000-000000000009', '00000000-0000-4000-a000-000000000010');

-- 3. Session summaries
DELETE FROM session_summaries WHERE session_type = 'synthetic';

-- 4. Templates (synthetic)
DELETE FROM task_templates WHERE id LIKE 'TPL_SYN_%';

-- 5. Tasks (synthetic)
DELETE FROM tasks_master WHERE source_type = 'synthetic';

-- 6. Old generated tasks (also synthetic)
DELETE FROM tasks_master WHERE id LIKE 'G5_GEN_%';

-- 7. Reset benchmarks to zero
UPDATE skill_benchmarks SET total_attempts = 0, total_correct = 0,
    success_rate_grade_5 = NULL, last_calculated_at = NOW()
WHERE skill_id LIKE 'G5_%';

COMMIT;

-- After running this, load real data with:
--   psql -d algo_v1 -f <real_data>.sql