"""Persist explicit cancellation reasons for lesson instances.

Revision ID: h8i9j0k1l2m3
Revises: g7h8i9j0k1l2
Create Date: 2026-08-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h8i9j0k1l2m3"
down_revision: Union[str, Sequence[str], None] = "g7h8i9j0k1l2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Additive only: existing lesson records and their JSON attendance snapshots
    # remain byte-for-byte unchanged.
    op.add_column(
        "group_attendance",
        sa.Column("cancellation_reason", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "group_attendance_audit",
        sa.Column("cancellation_reason", sa.String(length=500), nullable=True),
    )
    # Earlier local databases were created before Alembic's naming convention
    # was standardised, so the same check can have either physical name.
    # Drop both safely before installing the canonical constraint.
    op.execute(
        "ALTER TABLE group_attendance_audit "
        "DROP CONSTRAINT IF EXISTS ck_group_attendance_audit_action"
    )
    op.execute(
        "ALTER TABLE group_attendance_audit "
        "DROP CONSTRAINT IF EXISTS group_attendance_audit_action_check"
    )
    op.create_check_constraint(
        "ck_group_attendance_audit_action",
        "group_attendance_audit",
        "action IN ('created', 'updated', 'corrected', 'cancelled')",
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE group_attendance_audit "
        "DROP CONSTRAINT IF EXISTS ck_group_attendance_audit_action"
    )
    op.create_check_constraint(
        "ck_group_attendance_audit_action",
        "group_attendance_audit",
        "action IN ('created', 'updated', 'corrected')",
    )
    op.drop_column("group_attendance_audit", "cancellation_reason")
    op.drop_column("group_attendance", "cancellation_reason")
