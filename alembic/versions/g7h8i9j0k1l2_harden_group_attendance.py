"""Harden attendance status, provenance and audit history.

Revision ID: g7h8i9j0k1l2
Revises: f6a7b8c9d0e1
Create Date: 2026-08-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "g7h8i9j0k1l2"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Historical completed records remain intact; only the missing technical
    # timestamp is reconstructed from their already persisted update time.
    op.execute(
        """
        UPDATE group_attendance
        SET completed_at = COALESCE(updated_at, created_at, NOW())
        WHERE status = 'completed' AND completed_at IS NULL
        """
    )
    # A prior UI could create this unsupported state. Preserve its marks while
    # moving it to the only valid open state before adding the constraint.
    op.execute(
        "UPDATE group_attendance SET status = 'in_progress' WHERE status = 'scheduled'"
    )
    op.execute("ALTER TABLE group_attendance DROP CONSTRAINT IF EXISTS ck_group_attendance_status")
    op.create_check_constraint(
        "ck_group_attendance_status",
        "group_attendance",
        "status IN ('in_progress', 'completed', 'cancelled')",
    )
    op.alter_column(
        "group_attendance",
        "status",
        existing_type=sa.String(length=20),
        server_default="in_progress",
        nullable=False,
    )

    op.create_table(
        "group_attendance_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "attendance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("group_attendance.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("before_status", sa.String(length=20)),
        sa.Column("after_status", sa.String(length=20), nullable=False),
        sa.Column("before_records", postgresql.JSONB()),
        sa.Column("after_records", postgresql.JSONB(), nullable=False),
        sa.Column("correction_reason", sa.String(length=500)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.CheckConstraint(
            "action IN ('created', 'updated', 'corrected')",
            name="ck_group_attendance_audit_action",
        ),
    )
    op.create_index(
        "idx_group_attendance_audit_attendance",
        "group_attendance_audit",
        ["attendance_id"],
    )
    op.create_index(
        "idx_group_attendance_audit_group",
        "group_attendance_audit",
        ["group_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_group_attendance_audit_group", table_name="group_attendance_audit")
    op.drop_index("idx_group_attendance_audit_attendance", table_name="group_attendance_audit")
    op.drop_table("group_attendance_audit")
    op.execute("ALTER TABLE group_attendance DROP CONSTRAINT IF EXISTS ck_group_attendance_status")
    op.alter_column(
        "group_attendance",
        "status",
        existing_type=sa.String(length=20),
        server_default="completed",
        nullable=True,
    )
