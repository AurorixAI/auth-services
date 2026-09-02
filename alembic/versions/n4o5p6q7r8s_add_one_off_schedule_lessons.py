"""Add persisted one-off schedule lessons.

Revision ID: n4o5p6q7r8s
Revises: m3n4o5p6q7r8
Create Date: 2026-08-25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "n4o5p6q7r8s"
down_revision = "m3n4o5p6q7r8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_schedule_one_off_lessons",
        sa.Column("id", sa.String(length=100), primary_key=True),
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
        sa.Column("lesson_date", sa.String(length=10), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "idx_group_schedule_one_off_group_date",
        "group_schedule_one_off_lessons",
        ["group_id", "lesson_date"],
    )
    op.create_index(
        "idx_group_schedule_one_off_org_date",
        "group_schedule_one_off_lessons",
        ["org_id", "lesson_date"],
    )


def downgrade() -> None:
    op.drop_index("idx_group_schedule_one_off_org_date", table_name="group_schedule_one_off_lessons")
    op.drop_index("idx_group_schedule_one_off_group_date", table_name="group_schedule_one_off_lessons")
    op.drop_table("group_schedule_one_off_lessons")
