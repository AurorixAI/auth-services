"""Record the actor and assignment when a group is created.

Revision ID: o5p6q7r8s9t
Revises: n4o5p6q7r8s
Create Date: 2026-09-01
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "o5p6q7r8s9t"
down_revision = "n4o5p6q7r8s"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
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
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("actor_role", sa.String(length=20), nullable=False),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("assigned_teacher_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("action IN ('created')", name="ck_group_audit_action"),
        sa.CheckConstraint(
            "actor_role IN ('owner', 'admin', 'teacher', 'superadmin')",
            name="ck_group_audit_actor_role",
        ),
    )
    op.create_index("idx_group_audit_group", "group_audit", ["group_id", "created_at"])
    op.create_index("idx_group_audit_org", "group_audit", ["org_id", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_group_audit_org", table_name="group_audit")
    op.drop_index("idx_group_audit_group", table_name="group_audit")
    op.drop_table("group_audit")
