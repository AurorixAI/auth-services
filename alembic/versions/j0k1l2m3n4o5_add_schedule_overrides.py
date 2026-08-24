"""Add versioned recurring schedule changes and one-off lesson transfers.

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-08-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "j0k1l2m3n4o5"
down_revision: Union[str, Sequence[str], None] = "i9j0k1l2m3n4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "group_schedule_overrides",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lesson_id", sa.String(length=100), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("effective_date", sa.String(length=10), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("changed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint("group_id", "lesson_id", "scope", "effective_date", name="uq_group_schedule_override_scope_date"),
        sa.CheckConstraint("scope IN ('occurrence', 'recurring')", name="ck_group_schedule_override_scope"),
    )
    op.create_index("idx_group_schedule_override_lookup", "group_schedule_overrides", ["group_id", "lesson_id", "effective_date"])
    op.create_index("idx_group_schedule_override_org", "group_schedule_overrides", ["org_id"])

    op.create_table(
        "group_schedule_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("override_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("group_schedule_overrides.id", ondelete="CASCADE"), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lesson_id", sa.String(length=100), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("effective_date", sa.String(length=10), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("before_payload", postgresql.JSONB(), nullable=True),
        sa.Column("after_payload", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.CheckConstraint("scope IN ('occurrence', 'recurring')", name="ck_group_schedule_audit_scope"),
    )
    op.create_index("idx_group_schedule_audit_group", "group_schedule_audit", ["group_id", "effective_date"])
    op.create_index("idx_group_schedule_audit_override", "group_schedule_audit", ["override_id"])


def downgrade() -> None:
    op.drop_index("idx_group_schedule_audit_override", table_name="group_schedule_audit")
    op.drop_index("idx_group_schedule_audit_group", table_name="group_schedule_audit")
    op.drop_table("group_schedule_audit")
    op.drop_index("idx_group_schedule_override_org", table_name="group_schedule_overrides")
    op.drop_index("idx_group_schedule_override_lookup", table_name="group_schedule_overrides")
    op.drop_table("group_schedule_overrides")
