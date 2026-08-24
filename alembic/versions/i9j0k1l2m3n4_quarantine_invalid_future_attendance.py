"""Quarantine attendance rows that could not validly exist in the future.

Revision ID: i9j0k1l2m3n4
Revises: h8i9j0k1l2m3
Create Date: 2026-08-20

A scheduled occurrence is represented only by the group's recurring schedule.
An attendance row is created when the lesson starts, except that a future
lesson may be explicitly cancelled.  Older UI versions could create a row for
a future occurrence. They could also retain anonymous open records, which
cannot be treated as valid attendance because no teacher or audit event can be
attributed to them. Both cases made a lesson look started incorrectly.

The raw snapshots are copied to an immutable integrity ledger before removal,
so the repair does not silently discard information.  They are deliberately
not restored on downgrade: restoring an impossible future attendance state
would violate the lifecycle contract again.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "i9j0k1l2m3n4"
down_revision: Union[str, Sequence[str], None] = "h8i9j0k1l2m3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _invalid_attendance_predicate() -> str:
    # lesson_date is the canonical YYYY-MM-DD string.  Using the platform's
    # business timezone avoids an accidental cleanup around UTC midnight.
    return """
        (
            lesson_date > TO_CHAR(NOW() AT TIME ZONE 'Asia/Tashkent', 'YYYY-MM-DD')
            AND status <> 'cancelled'
        )
        OR (
            status = 'in_progress'
            AND marked_by IS NULL
            AND NOT EXISTS (
                SELECT 1
                FROM group_attendance_audit audit
                WHERE audit.attendance_id = group_attendance.id
            )
        )
    """


def upgrade() -> None:
    op.create_table(
        "group_attendance_integrity_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("source_attendance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lesson_date", sa.String(length=10), nullable=False),
        sa.Column("lesson_id", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("records", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.String(length=100), nullable=False),
        sa.Column("detected_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index(
        "idx_group_attendance_integrity_audit_source",
        "group_attendance_integrity_audit",
        ["source_attendance_id"],
    )
    op.create_index(
        "idx_group_attendance_integrity_audit_group",
        "group_attendance_integrity_audit",
        ["group_id", "lesson_date"],
    )

    predicate = _invalid_attendance_predicate()
    op.execute(
        f"""
        INSERT INTO group_attendance_integrity_audit
            (id, source_attendance_id, org_id, group_id, lesson_date, lesson_id, status, records, reason)
        SELECT
            gen_random_uuid(), id, org_id, group_id, lesson_date, lesson_id,
            status,
            COALESCE(records, '{{}}'::jsonb),
            CASE
                WHEN lesson_date > TO_CHAR(NOW() AT TIME ZONE 'Asia/Tashkent', 'YYYY-MM-DD')
                    THEN 'invalid_future_attendance'
                ELSE 'unattributable_legacy_attendance'
            END
        FROM group_attendance
        WHERE {predicate}
        """
    )
    op.execute(f"DELETE FROM group_attendance WHERE {predicate}")


def downgrade() -> None:
    op.drop_index("idx_group_attendance_integrity_audit_group", table_name="group_attendance_integrity_audit")
    op.drop_index("idx_group_attendance_integrity_audit_source", table_name="group_attendance_integrity_audit")
    op.drop_table("group_attendance_integrity_audit")
