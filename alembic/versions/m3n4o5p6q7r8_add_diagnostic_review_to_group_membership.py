"""Track teacher acknowledgement of an initial diagnostic per group membership.

Revision ID: m3n4o5p6q7r8
Revises: l2m3n4o5p6q7
Create Date: 2026-08-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "m3n4o5p6q7r8"
down_revision: Union[str, Sequence[str], None] = "l2m3n4o5p6q7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("student_groups", sa.Column("initial_diagnostic_reviewed_invite_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("student_groups", sa.Column("initial_diagnostic_reviewed_at", sa.DateTime(), nullable=True))
    op.add_column(
        "student_groups",
        sa.Column("initial_diagnostic_reviewed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("student_groups", "initial_diagnostic_reviewed_by")
    op.drop_column("student_groups", "initial_diagnostic_reviewed_at")
    op.drop_column("student_groups", "initial_diagnostic_reviewed_invite_id")
