"""Student profile journal (IA fix pack, 22.10): who changed a student's name, class, phone or note, and from what to what.

Revision ID: sa1b2c3d4e5
Revises: lb3c4d5e6f7
Create Date: 2026-10-22

``student_audit`` - one row per ``PUT /org/students/{id}`` that changed a journaled field; ``changes`` = ``{field: {"old", "new"}}``.
Rollback (downgrade): drops the table (the journal is lost; nothing else reads it).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "sa1b2c3d4e5"
down_revision: str = "lb3c4d5e6f7"
branch_labels = None
depends_on = None

TABLE = "student_audit"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    uuid = postgresql.UUID(as_uuid=True)
    if TABLE not in set(sa.inspect(bind).get_table_names()):
        op.create_table(
            TABLE,
            sa.Column("id", uuid, primary_key=True),
            sa.Column("org_id", uuid, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("student_id", uuid, sa.ForeignKey("students.id", ondelete="CASCADE"), nullable=False),
            sa.Column("actor_id", uuid, sa.ForeignKey("users.id"), nullable=False),
            sa.Column("actor_role", sa.String(20), nullable=False),
            sa.Column("action", sa.String(30), nullable=False, server_default="profile_updated"),
            sa.Column("changes", postgresql.JSONB(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("timezone('utc', now())")),
            sa.CheckConstraint("actor_role IN ('owner', 'admin', 'teacher', 'superadmin')", name="ck_student_audit_actor_role"),
        )
    if "idx_student_audit_student" not in {i["name"] for i in sa.inspect(bind).get_indexes(TABLE)}:
        op.create_index("idx_student_audit_student", TABLE, ["student_id", "created_at"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    if TABLE in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table(TABLE)
