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
    # Older baseline runs used current ORM metadata, which can already contain
    # these fields. Also support a partially upgraded legacy schema.
    inspector = sa.inspect(op.get_bind())
    existing = {c["name"] for c in inspector.get_columns("student_groups")}
    columns = (
        sa.Column("initial_diagnostic_reviewed_invite_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("initial_diagnostic_reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("initial_diagnostic_reviewed_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    for column in columns:
        if column.name not in existing:
            op.add_column("student_groups", column)
    has_fk = any(
        fk["constrained_columns"] == ["initial_diagnostic_reviewed_by"]
        and fk["referred_table"] == "users"
        for fk in inspector.get_foreign_keys("student_groups")
    )
    if not has_fk:
        op.create_foreign_key(
            "fk_student_groups_diagnostic_reviewed_by", "student_groups", "users",
            ["initial_diagnostic_reviewed_by"], ["id"],
        )


def downgrade() -> None:
    op.drop_column("student_groups", "initial_diagnostic_reviewed_by")
    op.drop_column("student_groups", "initial_diagnostic_reviewed_at")
    op.drop_column("student_groups", "initial_diagnostic_reviewed_invite_id")
