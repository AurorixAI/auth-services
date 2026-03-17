"""002_baseline_stamp

Помечает текущее состояние БД как baseline.

ВСЕ таблицы уже созданы raw SQL файлами (database/001-010_*.sql).
Эта миграция НЕ создаёт и НЕ меняет ничего — она лишь фиксирует
точку отсчёта, от которой Alembic будет отслеживать изменения.

Для существующей БД: alembic stamp 002_baseline
Для новой БД: raw SQL создаёт таблицы, затем alembic stamp 002_baseline

После этого ВСЕ новые изменения схемы — только через:
  alembic revision --autogenerate -m "описание"
  alembic upgrade head

Revision ID: 002_baseline
Revises: 001_initial
Create Date: 2026-02-19
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = '002_baseline'
down_revision = '001_initial'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Пустая миграция — baseline stamp.
    
    Все 28 ORM-моделей (17 core + 9 auth + 2 exam) уже существуют в БД,
    созданные через database/*.sql файлы.
    
    Таблицы:
      Core: knowledge_hierarchy, skill_prerequisites, skill_benchmarks,
            tasks_master, textbooks, textbook_structure, textbook_tasks,
            textbook_toc, textbook_skill_map
      Analytics: answer_events, skill_correlations, error_patterns,
                 session_summaries, student_skill_states, graph_updates_log,
                 irt_calibration_history, skill_response_times
      Auth: organizations, users, organization_members, invitations,
            students, groups, student_groups, diagnostic_invites,
            student_diagnostic_results
      Exam: generated_exams, exam_sessions
      Legacy (SQL only, not in ORM): task_templates, task_tags, audit_log,
            topic_synonyms, digitization_log, task_distractors
    """
    pass


def downgrade() -> None:
    """Baseline нельзя откатить — это начальная точка."""
    pass
