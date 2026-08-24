"""Require an explicit, auditable start before attendance is recorded.

Revision ID: k1l2m3n4o5p6
Revises: j0k1l2m3n4o5
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "k1l2m3n4o5p6"
down_revision: Union[str, Sequence[str], None] = "j0k1l2m3n4o5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("group_attendance", sa.Column("started_at", sa.DateTime(), nullable=True))
    # The immutable audit must distinguish a deliberate lesson start from a
    # later edit of its attendance snapshot.
    op.execute(
        "ALTER TABLE group_attendance_audit "
        "DROP CONSTRAINT IF EXISTS ck_group_attendance_audit_action"
    )
    op.create_check_constraint(
        "ck_group_attendance_audit_action",
        "group_attendance_audit",
        "action IN ('started', 'created', 'updated', 'corrected', 'cancelled')",
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE group_attendance_audit "
        "DROP CONSTRAINT IF EXISTS ck_group_attendance_audit_action"
    )
    op.create_check_constraint(
        "ck_group_attendance_audit_action",
        "group_attendance_audit",
        "action IN ('created', 'updated', 'corrected', 'cancelled')",
    )
    op.drop_column("group_attendance", "started_at")
