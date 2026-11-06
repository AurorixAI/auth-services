"""Money-1 (pack V5): the receipt is marked «копия» when it is printed again.

Revision ID: mv5a1b2c3d4
Revises: mv6a1b2c3d4
Create Date: 2026-11-07

* ``student_payments.print_count`` (NOT NULL, default 0) and ``student_payments.last_printed_at`` (nullable): the first print of a receipt is the original, every next one is a copy.

Only ADDS (no data is moved; a constant default is not a rewrite of the table).  Rollback (downgrade): drops the columns.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "mv5a1b2c3d4"
down_revision: str = "mv6a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    names = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("student_payments")}
    if "print_count" not in names:
        op.add_column("student_payments", sa.Column("print_count", sa.Integer(), nullable=False, server_default="0"))
    if "last_printed_at" not in names:
        op.add_column("student_payments", sa.Column("last_printed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    names = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("student_payments")}
    for column in ("last_printed_at", "print_count"):
        if column in names:
            op.drop_column("student_payments", column)
