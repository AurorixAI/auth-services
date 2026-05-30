"""Add analytics tables: answer_events, session_summaries, student_skill_states

These tables existed in the production database before Alembic was introduced
(created via the original SQL init scripts). This migration ensures they are
created on a fresh database via `alembic upgrade head`.

On existing databases with these tables already present, run:
    alembic stamp <this_revision_id>

Revision ID: b2c3d4e5f6a7
Revises: a55f9fecfdec
Create Date: 2026-05-27
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b2c3d4e5f6a7"
down_revision = "a55f9fecfdec"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── answer_events ─────────────────────────────────────────────────────
    # Individual answer records per task in a diagnostic session.
    op.create_table(
        "answer_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("student_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_id", sa.String(60), nullable=False),
        sa.Column("skill_id", sa.String(60), nullable=False),
        sa.Column("is_correct", sa.Boolean(), nullable=False),
        sa.Column("student_answer", sa.Text(), nullable=True),
        sa.Column("correct_answer", sa.Text(), nullable=True),
        sa.Column("time_spent_ms", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=False),
        sa.Column("difficulty_level", sa.String(5), nullable=True),
        sa.Column("task_order_in_session", sa.Integer(), nullable=True),
        sa.Column("task_irt_a", sa.Numeric(6, 4), nullable=True),
        sa.Column("task_irt_b", sa.Numeric(6, 4), nullable=True),
        sa.Column("task_irt_c", sa.Numeric(6, 4), nullable=True),
        sa.Column("student_theta_before", sa.Numeric(8, 4), nullable=True),
        sa.Column("student_theta_after", sa.Numeric(8, 4), nullable=True),
        sa.Column("device_type", sa.String(20), nullable=True),
        sa.Column("client_version", sa.String(20), nullable=True),
        sa.Column("error_type", sa.String(50), nullable=True),
        sa.Column("error_explanation", sa.Text(), nullable=True),
        sa.Column("matched_distractor", sa.Text(), nullable=True),
    )
    op.create_index("idx_ae_session", "answer_events", ["session_id"])
    op.create_index("idx_ae_student", "answer_events", ["student_id"])
    op.create_index("idx_ae_skill", "answer_events", ["skill_id"])
    op.create_index("idx_ae_correct", "answer_events", ["is_correct"])
    op.create_index("idx_ae_skill_correct", "answer_events", ["skill_id", "is_correct"])
    op.create_index("idx_ae_submitted", "answer_events", ["submitted_at"])
    op.create_index(
        "idx_ae_error_type", "answer_events", ["error_type"],
        postgresql_where=sa.text("error_type IS NOT NULL"),
    )

    # ── session_summaries ─────────────────────────────────────────────────
    # Aggregated session stats (1 row per session).
    op.create_table(
        "session_summaries",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("student_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("total_duration_ms", sa.Integer(), nullable=True),
        sa.Column("total_tasks", sa.Integer(), nullable=True),
        sa.Column("correct_answers", sa.Integer(), nullable=True),
        sa.Column("final_theta", sa.Numeric(8, 4), nullable=True),
        sa.Column("confidence_interval_low", sa.Numeric(8, 4), nullable=True),
        sa.Column("confidence_interval_high", sa.Numeric(8, 4), nullable=True),
        sa.Column("identified_gaps", postgresql.JSONB(), nullable=True),
        sa.Column("root_gaps", postgresql.JSONB(), nullable=True),
        sa.Column("recommended_skills", postgresql.JSONB(), nullable=True),
        sa.Column("priority_order", postgresql.JSONB(), nullable=True),
        sa.Column("session_type", sa.String(30), nullable=True),
        sa.Column("platform", sa.String(30), nullable=True),
        sa.Column("client_version", sa.String(20), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.text("NOW()")),
        sa.CheckConstraint(
            "session_type IN ('diagnostic', 'quick_check', 'practice', 'exam')",
            name="session_summaries_session_type_check",
        ),
    )
    op.create_index("idx_ss_student", "session_summaries", ["student_id"])
    op.create_index("idx_ss_started", "session_summaries", ["started_at"])
    op.create_index("idx_ss_type", "session_summaries", ["session_type"])

    # ── student_skill_states ──────────────────────────────────────────────
    # Per-student Bayesian knowledge state for each skill (BKT model).
    op.create_table(
        "student_skill_states",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "student_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("skill_id", sa.String(60), nullable=False),
        sa.Column("mastery_probability", sa.Numeric(5, 4), nullable=False),
        sa.Column("learn_rate", sa.Numeric(5, 4), nullable=True),
        sa.Column("guess_rate", sa.Numeric(5, 4), nullable=True),
        sa.Column("slip_rate", sa.Numeric(5, 4), nullable=True),
        sa.Column("theta", sa.Numeric(8, 4), nullable=True),
        sa.Column("theta_se", sa.Numeric(8, 4), nullable=True),
        sa.Column("total_attempts", sa.Integer(), nullable=True),
        sa.Column("correct_attempts", sa.Integer(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_correct_at", sa.DateTime(), nullable=True),
        sa.Column("trend", sa.String(20), nullable=True),
        sa.Column("consecutive_correct", sa.Integer(), nullable=True),
        sa.Column("consecutive_incorrect", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.text("NOW()")),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=True,
        ),
        sa.UniqueConstraint("student_id", "skill_id", name="student_skill_states_student_id_skill_id_key"),
        sa.CheckConstraint(
            "status IN ('mastered', 'learning', 'struggling', 'gap', 'unknown')",
            name="student_skill_states_status_check",
        ),
        sa.CheckConstraint(
            "trend IN ('improving', 'stable', 'declining', 'unknown')",
            name="student_skill_states_trend_check",
        ),
    )
    op.create_index("idx_sss_student", "student_skill_states", ["student_id"])
    op.create_index("idx_sss_skill", "student_skill_states", ["skill_id"])
    op.create_index("idx_sss_status", "student_skill_states", ["status"])
    op.create_index("idx_sss_mastery", "student_skill_states", ["mastery_probability"])


def downgrade() -> None:
    op.drop_table("student_skill_states")
    op.drop_table("session_summaries")
    op.drop_table("answer_events")
