"""Attendance excuses, lesson teacher snapshot, group change audit actions (N13 P1).

Revision ID: s9t0u1v2w3x
Revises: r8s9t0u1v2w
Create Date: 2026-10-14

* ``group_attendance.teacher_id`` - the group's teacher when the lesson row was
  created.  Existing rows are backfilled with the *current* teacher of their
  group (an approximation: groups never kept a teacher history).
* ``attendance_excuses`` / ``attendance_excuse_files`` - reason for an
  ``excused`` mark, the administrator's decision and attached certificates.
* ``group_attendance_audit.details`` (JSONB) - what exactly happened to an
  excuse or a certificate; the audit CHECK lists gain the new actions.

Everything is additive and idempotent: ``001_baseline`` and ``create_all`` may
have created some of these objects already.

Rollback runbook (an older auth image cannot start on a database at this head -
unknown revision):
  1. export ``attendance_excuses`` and ``attendance_excuse_files`` (the
     downgrade DROPS both tables, ``group_attendance.teacher_id`` and
     ``group_attendance_audit.details``);
  2. ``alembic downgrade r8s9t0u1v2w``;
  3. only then roll the image back.
Audit rows that use the new actions are kept; the restored old CHECKs are NOT
VALID, so they constrain new rows only.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "s9t0u1v2w3x"
down_revision: str = "r8s9t0u1v2w"
branch_labels = None
depends_on = None

ATTENDANCE = "group_attendance"
EXCUSES = "attendance_excuses"
EXCUSE_FILES = "attendance_excuse_files"

ATTENDANCE_AUDIT_OLD = (
    "action IN ('started', 'reconciliation_started', 'created', 'updated', "
    "'corrected', 'cancelled', 'reconciliation_cancelled')"
)
ATTENDANCE_AUDIT_NEW = (
    "action IN ('started', 'reconciliation_started', 'created', 'updated', "
    "'corrected', 'cancelled', 'reconciliation_cancelled', "
    "'excuse_reviewed', 'excuse_reset', 'excuse_removed', "
    "'excuse_file_added', 'excuse_file_removed')"
)
GROUP_AUDIT_OLD = "action IN ('created')"
GROUP_AUDIT_NEW = "action IN ('created', 'updated', 'teacher_changed')"


def _index_names(inspector, table: str) -> set[str]:
    return {ix["name"] for ix in inspector.get_indexes(table)}


def _replace_check(table: str, name: str, expression: str, *, not_valid: bool = False) -> None:
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    # Older local databases may carry the PostgreSQL default name instead.
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_action_check")
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression})"
        + (" NOT VALID" if not_valid else "")
    )


def upgrade() -> None:
    # Fail fast instead of queueing behind a long transaction on a hot table.
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Lesson teacher snapshot.
    existing = {c["name"] for c in inspector.get_columns(ATTENDANCE)}
    if "teacher_id" not in existing:
        op.add_column(
            ATTENDANCE,
            sa.Column(
                "teacher_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id"),
                nullable=True,
            ),
        )
    if "idx_attendance_teacher" not in _index_names(inspector, ATTENDANCE):
        op.create_index(
            "idx_attendance_teacher", ATTENDANCE, ["org_id", "teacher_id", "lesson_date"]
        )
    op.execute(
        "UPDATE group_attendance a SET teacher_id = g.teacher_id "
        "FROM groups g WHERE a.group_id = g.id AND a.teacher_id IS NULL"
    )

    # 2. Excuses.
    tables = set(inspector.get_table_names())
    if EXCUSES not in tables:
        op.create_table(
            EXCUSES,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "org_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "attendance_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("group_attendance.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("student_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("reason", sa.String(length=300), nullable=False),
            sa.Column(
                "reason_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
            ),
            sa.Column("reason_at", sa.DateTime(), nullable=False),
            sa.Column("status", sa.String(length=12), nullable=False, server_default="pending"),
            sa.Column(
                "reviewed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
            ),
            sa.Column("reviewed_at", sa.DateTime(), nullable=True),
            sa.Column("review_note", sa.String(length=500), nullable=True),
            sa.UniqueConstraint("attendance_id", "student_id", name="uq_attendance_excuse_student"),
            sa.CheckConstraint(
                "status IN ('pending', 'confirmed', 'rejected')",
                name="ck_attendance_excuse_status",
            ),
        )
    if "idx_attendance_excuses_org_status" not in _index_names(sa.inspect(bind), EXCUSES):
        op.create_index("idx_attendance_excuses_org_status", EXCUSES, ["org_id", "status"])

    # 3. Certificates.
    if EXCUSE_FILES not in tables:
        op.create_table(
            EXCUSE_FILES,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "excuse_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("attendance_excuses.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("filename", sa.String(length=200), nullable=True),
            sa.Column("content_type", sa.String(length=50), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("data", sa.LargeBinary(), nullable=False),
            sa.Column(
                "uploaded_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
            ),
            sa.Column("uploaded_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "size_bytes > 0 AND size_bytes <= 2097152",
                name="ck_attendance_excuse_file_size",
            ),
        )
    if "idx_attendance_excuse_files_excuse" not in _index_names(sa.inspect(bind), EXCUSE_FILES):
        op.create_index("idx_attendance_excuse_files_excuse", EXCUSE_FILES, ["excuse_id"])

    # 4-5. Audit details and actions.
    if "details" not in {c["name"] for c in sa.inspect(bind).get_columns("group_attendance_audit")}:
        op.add_column(
            "group_attendance_audit",
            sa.Column("details", postgresql.JSONB(), nullable=True),
        )
    _replace_check(
        "group_attendance_audit", "ck_group_attendance_audit_action", ATTENDANCE_AUDIT_NEW
    )
    _replace_check("group_audit", "ck_group_audit_action", GROUP_AUDIT_NEW)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    _replace_check(
        "group_audit", "ck_group_audit_action", GROUP_AUDIT_OLD, not_valid=True
    )
    _replace_check(
        "group_attendance_audit",
        "ck_group_attendance_audit_action",
        ATTENDANCE_AUDIT_OLD,
        not_valid=True,
    )

    if "details" in {c["name"] for c in inspector.get_columns("group_attendance_audit")}:
        op.drop_column("group_attendance_audit", "details")

    tables = set(inspector.get_table_names())
    if EXCUSE_FILES in tables:
        op.drop_table(EXCUSE_FILES)
    if EXCUSES in tables:
        op.drop_table(EXCUSES)

    if "idx_attendance_teacher" in _index_names(inspector, ATTENDANCE):
        op.drop_index("idx_attendance_teacher", table_name=ATTENDANCE)
    if "teacher_id" in {c["name"] for c in inspector.get_columns(ATTENDANCE)}:
        op.drop_column(ATTENDANCE, "teacher_id")
