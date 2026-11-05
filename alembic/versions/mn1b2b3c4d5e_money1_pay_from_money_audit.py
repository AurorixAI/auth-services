"""Money-1 (pack B): «Оплата с» of a stay, the journal of the money settings of a centre.

Revision ID: mn1b2b3c4d5e
Revises: mn1a2b3c4d5
Create Date: 2026-11-06

* ``student_group_periods.billing_from`` (B1): the first day the student of this stay is charged from (the first lesson of the group when he is added, or the day the owner chose); empty = the
  day he joined, as before.  The roster, the attendance and the lessons of the teacher still follow ``joined_at``.
* ``org_money_audit`` (B9): who changed the way the centre counts the payments (the mode, the day of the shared month, the size of the package, the recalculation of the lessons the centre
  cancelled) and what it did to the students (the sums); one row per save.  Append-only; a centre sees it on the page of the settings.

Only ADDS (no data is moved).  Rollback (downgrade): drops the table and the column; a stay then starts on the day he joined (the old behaviour).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "mn1b2b3c4d5e"
down_revision: str = "mn1a2b3c4d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "billing_from" not in {c["name"] for c in inspector.get_columns("student_group_periods")}:
        op.add_column("student_group_periods", sa.Column("billing_from", sa.Date(), nullable=True))
    if "org_money_audit" not in set(inspector.get_table_names()):
        op.create_table(
            "org_money_audit",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("actor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("action", sa.String(40), nullable=False),
            sa.Column("occurred_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        )
        op.create_index("ix_org_money_audit_org_time", "org_money_audit", ["org_id", "occurred_at"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "org_money_audit" in set(inspector.get_table_names()):
        op.drop_table("org_money_audit")
    if "billing_from" in {c["name"] for c in inspector.get_columns("student_group_periods")}:
        op.drop_column("student_group_periods", "billing_from")
