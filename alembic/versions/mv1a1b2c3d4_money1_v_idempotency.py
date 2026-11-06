"""Money-1 (pack V1): a repeated request of a forgiveness / a refund writes one record.

Revision ID: mv1a1b2c3d4
Revises: mn1b2b3c4d5e
Create Date: 2026-11-07

* ``student_writeoffs.idempotency_key`` and ``student_refunds.idempotency_key`` (nullable) + a partial unique index per table ``(org_id, idempotency_key) WHERE idempotency_key IS NOT NULL``:
  the header ``Idempotency-Key`` of a double click.  Old rows keep NULL.

* ``group_audit.action`` (``ck_group_audit_action``) also accepts ``payment_void`` / ``payment_replace`` (V2: the journal of money decisions) and ``payment_file_removed`` (V10: a removed cheque).

Only ADDS (no data is moved).  Rollback (downgrade): the rows of the three new actions go first, then the check is put back, then the indexes and the columns are dropped.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "mv1a1b2c3d4"
down_revision: str = "mn1b2b3c4d5e"
branch_labels = None
depends_on = None

AUDIT_OLD = (
    "action IN ('created', 'updated', 'teacher_changed', 'student_archived', 'student_restored', "
    "'group_price_set', 'membership_price_set', 'group_price_cancelled', 'membership_price_cancelled', "
    "'payment_writeoff', 'payment_writeoff_void', 'payment_refund', 'payment_refund_void', "
    "'membership_pause_set', 'membership_pause_void', 'opening_balance_set', 'opening_balance_void', "
    "'group_lesson_type_set', 'student_left_group', "
    "'group_archived', 'group_restored', 'lesson_teacher_set', 'lesson_teacher_cleared', 'day_off_voided', 'lesson_restored', 'lesson_cancelled', "
    "'one_off_created', 'one_off_money_decided', 'exam_release_pending', 'exam_release_done')"
)
NEW_ACTIONS = ("payment_void", "payment_replace", "payment_file_removed")
AUDIT_NEW = AUDIT_OLD[:-1] + ", " + ", ".join(f"'{a}'" for a in NEW_ACTIONS) + ")"


def _audit_check(definition: str) -> None:
    op.execute("ALTER TABLE group_audit DROP CONSTRAINT IF EXISTS ck_group_audit_action")
    op.execute(f"ALTER TABLE group_audit ADD CONSTRAINT ck_group_audit_action CHECK ({definition}) NOT VALID")
    op.execute("ALTER TABLE group_audit VALIDATE CONSTRAINT ck_group_audit_action")


TABLES = (("student_writeoffs", "uq_student_writeoffs_idempotency"), ("student_refunds", "uq_student_refunds_idempotency"))


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    inspector = sa.inspect(op.get_bind())
    for table, index in TABLES:
        if "idempotency_key" not in {c["name"] for c in inspector.get_columns(table)}:
            op.add_column(table, sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=True))
        if index not in {i["name"] for i in inspector.get_indexes(table)}:
            op.create_index(index, table, ["org_id", "idempotency_key"], unique=True, postgresql_where=sa.text("idempotency_key IS NOT NULL"))
    _audit_check(AUDIT_NEW)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DELETE FROM group_audit WHERE action IN ('payment_void', 'payment_replace', 'payment_file_removed')")
    _audit_check(AUDIT_OLD)
    inspector = sa.inspect(op.get_bind())
    for table, index in TABLES:
        if index in {i["name"] for i in inspector.get_indexes(table)}:
            op.drop_index(index, table_name=table)
        if "idempotency_key" in {c["name"] for c in inspector.get_columns(table)}:
            op.drop_column(table, "idempotency_key")
