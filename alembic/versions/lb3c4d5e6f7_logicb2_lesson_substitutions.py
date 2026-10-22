"""LOGIC-B2 (3/3): lesson substitution requests + events, and the durable marker of a pending exam release at group archive (fix of B).

Revision ID: lb3c4d5e6f7
Revises: lb2b3c4d5e6
Create Date: 2026-10-21

* ``groups.exam_release_pending`` BOOLEAN NOT NULL DEFAULT false: the group was archived (committed) but exam-service has not confirmed the release of
  its untouched exams; retried by the next archive-related call and by ``scripts/sweep_exam_releases.py``.

* ``lesson_substitution_requests`` (a teacher's "I cannot teach this lesson" and the decision) with the CHECKs ``status`` / ``reason_code`` / ``origin`` /
  ``(status = 'pending') = (decided_at IS NULL)``, the partial unique index ``uq_lsr_pending_lesson (group_id, lesson_id, lesson_date) WHERE status = 'pending'``
  (INV-B2-1) and the list / counter / chain indexes;
* ``lesson_substitution_events`` (append-only journal, ``actor_id`` NULL = the system; the source of the future notifications).
No data is rewritten.

Idempotent: every step is guarded (inspector).  Rollback (downgrade): drops both tables (THE DATA OF THE REQUESTS IS LOST IRREVERSIBLY) and the marker.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "lb3c4d5e6f7"
down_revision: str = "lb2b3c4d5e6"
branch_labels = None
depends_on = None

REQUESTS = "lesson_substitution_requests"
EVENTS = "lesson_substitution_events"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "exam_release_pending" not in {c["name"] for c in inspector.get_columns("groups")}:
        op.add_column("groups", sa.Column("exam_release_pending", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    tables = set(sa.inspect(bind).get_table_names())
    uuid = postgresql.UUID(as_uuid=True)
    if REQUESTS not in tables:
        op.create_table(
            REQUESTS,
            sa.Column("id", uuid, primary_key=True),
            sa.Column("org_id", uuid, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("group_id", uuid, sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
            sa.Column("lesson_id", sa.String(100), nullable=False),
            sa.Column("lesson_date", sa.String(10), nullable=False),
            sa.Column("start_time", sa.String(5), nullable=False),
            sa.Column("end_time", sa.String(5), nullable=False),
            sa.Column("starts_at", sa.DateTime(), nullable=False),
            sa.Column("requester_id", uuid, sa.ForeignKey("users.id"), nullable=False),
            sa.Column("origin", sa.String(8), nullable=False, server_default="teacher"),
            sa.Column("batch_id", uuid, nullable=False),
            sa.Column("reason_code", sa.String(8), nullable=False),
            sa.Column("comment", sa.String(500), nullable=True),
            sa.Column("suggested_teacher_id", uuid, sa.ForeignKey("users.id"), nullable=True),
            sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
            sa.Column("decided_by", uuid, sa.ForeignKey("users.id"), nullable=True),
            sa.Column("decided_at", sa.DateTime(), nullable=True),
            sa.Column("decided_via", sa.String(20), nullable=True),
            sa.Column("decision_note", sa.String(500), nullable=True),
            sa.Column("substitute_id", uuid, sa.ForeignKey("users.id"), nullable=True),
            sa.Column("assignment_id", uuid, sa.ForeignKey("lesson_teacher_assignments.id", ondelete="SET NULL"), nullable=True),
            sa.Column("moved_to_lesson_id", sa.String(100), nullable=True),
            sa.Column("moved_to_date", sa.String(10), nullable=True),
            sa.Column("moved_to_start", sa.String(5), nullable=True),
            sa.Column("moved_to_end", sa.String(5), nullable=True),
            sa.Column("closed_reason", sa.String(30), nullable=True),
            sa.Column("previous_request_id", uuid, sa.ForeignKey("lesson_substitution_requests.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("timezone('utc', now())")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("timezone('utc', now())")),
            sa.CheckConstraint(
                "status IN ('pending', 'substituted', 'rescheduled', 'cancelled', 'rejected', 'withdrawn', 'closed')", name="ck_lsr_status"
            ),
            sa.CheckConstraint("reason_code IN ('sick', 'personal', 'other')", name="ck_lsr_reason_code"),
            sa.CheckConstraint("origin IN ('teacher', 'system')", name="ck_lsr_origin"),
            sa.CheckConstraint("(status = 'pending') = (decided_at IS NULL)", name="ck_lsr_decided_at"),
        )
    existing = {i["name"] for i in sa.inspect(bind).get_indexes(REQUESTS)}
    if "uq_lsr_pending_lesson" not in existing:
        op.create_index("uq_lsr_pending_lesson", REQUESTS, ["group_id", "lesson_id", "lesson_date"], unique=True,
                        postgresql_where=sa.text("status = 'pending'"))
    for name, cols in (
        ("idx_lsr_org_status_starts", ["org_id", "status", "starts_at"]),
        ("idx_lsr_org_requester", ["org_id", "requester_id", "created_at"]),
        ("idx_lsr_batch", ["batch_id"]),
        ("idx_lsr_lesson", ["group_id", "lesson_id", "lesson_date"]),
        ("idx_lsr_substitute", ["substitute_id"]),
    ):
        if name not in existing:
            op.create_index(name, REQUESTS, cols)
    if EVENTS not in tables:
        op.create_table(
            EVENTS,
            sa.Column("id", uuid, primary_key=True),
            sa.Column("request_id", uuid, sa.ForeignKey("lesson_substitution_requests.id", ondelete="CASCADE"), nullable=False),
            sa.Column("org_id", uuid, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("actor_id", uuid, sa.ForeignKey("users.id"), nullable=True),
            sa.Column("action", sa.String(24), nullable=False),
            sa.Column("snapshot", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("timezone('utc', now())")),
            sa.CheckConstraint(
                "action IN ('requested', 'withdrawn', 'substituted', 'rescheduled', 'cancelled', 'rejected', 'closed', 'reopened', "
                "'assignment_cleared', 'suggestion_unavailable')",
                name="ck_lse_action",
            ),
        )
    if "idx_lse_request" not in {i["name"] for i in sa.inspect(bind).get_indexes(EVENTS)}:
        op.create_index("idx_lse_request", EVENTS, ["request_id", "created_at"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    inspector = sa.inspect(op.get_bind())
    for table in (EVENTS, REQUESTS):
        if table in inspector.get_table_names():
            op.drop_table(table)
    if "exam_release_pending" in {c["name"] for c in inspector.get_columns("groups")}:
        op.drop_column("groups", "exam_release_pending")
