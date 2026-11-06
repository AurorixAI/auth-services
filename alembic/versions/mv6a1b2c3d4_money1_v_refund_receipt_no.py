"""Money-1 (pack V6): the number of the receipt of a refund («В-n»).

Revision ID: mv6a1b2c3d4
Revises: mv1a1b2c3d4
Create Date: 2026-11-07

* ``student_refunds.receipt_no`` (nullable) + a partial unique index ``(org_id, receipt_no) WHERE receipt_no IS NOT NULL``: the paper of a refund is checked by its number.  Older refunds keep NULL.

Only ADDS (no data is moved).  Rollback (downgrade): drops the index and the column.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "mv6a1b2c3d4"
down_revision: str = "mv1a1b2c3d4"
branch_labels = None
depends_on = None

INDEX = "uq_student_refunds_receipt_no"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    inspector = sa.inspect(op.get_bind())
    if "receipt_no" not in {c["name"] for c in inspector.get_columns("student_refunds")}:
        op.add_column("student_refunds", sa.Column("receipt_no", sa.Integer(), nullable=True))
    if INDEX not in {i["name"] for i in inspector.get_indexes("student_refunds")}:
        op.create_index(INDEX, "student_refunds", ["org_id", "receipt_no"], unique=True, postgresql_where=sa.text("receipt_no IS NOT NULL"))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    inspector = sa.inspect(op.get_bind())
    if INDEX in {i["name"] for i in inspector.get_indexes("student_refunds")}:
        op.drop_index(INDEX, table_name="student_refunds")
    if "receipt_no" in {c["name"] for c in inspector.get_columns("student_refunds")}:
        op.drop_column("student_refunds", "receipt_no")
