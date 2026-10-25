"""PAY-F (25.10): the kind of a lesson cancellation and the days off of a centre.

Revision ID: pf1a2b3c4d5
Revises: cn1a2b3c4d5
Create Date: 2026-10-25

New table ``org_days_off`` (a date or a range of up to 14 days with a reason: the journal of «Выходной день центра»; ``restored`` keeps what voiding it gave back).
New columns of ``group_attendance``: ``cancellation_kind`` (``centre`` | ``holiday`` | NULL) and ``day_off_id`` (the day off that cancelled the lesson; a partial index).
Old cancellations keep ``cancellation_kind`` NULL: they are never recalculated.
``group_audit.action`` also accepts ``day_off_voided`` (one row per group when a day off is voided).
The CHECK and the foreign key of ``group_attendance`` are added ``NOT VALID`` and validated in a separate statement (a short lock on a big table).

Idempotent: a table or a column that already exists is skipped.
Rollback (downgrade): drops the two columns and the table - the kinds of the cancellations and the journal of the days off are LOST (the cancelled lessons
stay cancelled; with the kinds gone nothing is deducted any more, which is the behaviour before the release) and the ``day_off_voided`` audit rows go.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "pf1a2b3c4d5"
down_revision: str = "cn1a2b3c4d5"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
NOW_UTC = sa.text("timezone('utc', now())")
KIND_CHECK = "ck_group_attendance_cancellation_kind"


AUDIT_TABLE = "group_audit"
AUDIT_CHECK = "ck_group_audit_action"
AUDIT_OLD = (
    "action IN ('created', 'updated', 'teacher_changed', 'student_archived', 'student_restored', "
    "'group_price_set', 'membership_price_set', 'group_price_cancelled', 'membership_price_cancelled', "
    "'payment_writeoff', 'payment_writeoff_void', 'payment_refund', 'payment_refund_void', "
    "'membership_pause_set', 'membership_pause_void', 'opening_balance_set', 'opening_balance_void', "
    "'group_lesson_type_set', 'student_left_group', "
    "'group_archived', 'group_restored', 'lesson_teacher_set', 'lesson_teacher_cleared')"
)
AUDIT_NEW = AUDIT_OLD[:-1] + ", 'day_off_voided')"
FK = "group_attendance_day_off_id_fkey"
INDEX = "idx_group_attendance_day_off"


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _constraint(bind, table: str, name: str) -> bool:
    return bool(bind.execute(sa.text(
        "SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid WHERE t.relname = :t AND c.conname = :n"), {"t": table, "n": name}).scalar())


def _add_validated(table: str, name: str, definition: str) -> None:
    """``ADD CONSTRAINT ... NOT VALID`` then ``VALIDATE CONSTRAINT`` (two statements: the first takes only a short lock)."""
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} {definition} NOT VALID")
    op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    if "org_days_off" not in _tables(bind):
        op.create_table(
            "org_days_off",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("org_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("date_from", sa.Date(), nullable=False),
            sa.Column("date_to", sa.Date(), nullable=False),
            sa.Column("reason", sa.String(200), nullable=False),
            sa.Column("created_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=NOW_UTC),
            sa.Column("voided_at", sa.DateTime(), nullable=True),
            sa.Column("voided_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("restored", postgresql.JSONB(), nullable=True),
            sa.CheckConstraint("date_to >= date_from AND date_to - date_from <= 13", name="ck_org_days_off_range"),
            sa.CheckConstraint("char_length(btrim(reason)) >= 3", name="ck_org_days_off_reason"),
        )
        op.create_index("idx_org_days_off_org_dates", "org_days_off", ["org_id", "date_from", "date_to"])
    columns = _columns(bind, "group_attendance")
    if "cancellation_kind" not in columns:
        op.add_column("group_attendance", sa.Column("cancellation_kind", sa.String(20), nullable=True))
    if not _constraint(bind, "group_attendance", KIND_CHECK):
        _add_validated("group_attendance", KIND_CHECK, "CHECK (cancellation_kind IS NULL OR cancellation_kind IN ('centre', 'holiday'))")
    if "day_off_id" not in columns:
        op.add_column("group_attendance", sa.Column("day_off_id", UUID, nullable=True))
    if not _constraint(bind, "group_attendance", FK):
        _add_validated("group_attendance", FK, "FOREIGN KEY (day_off_id) REFERENCES org_days_off (id) ON DELETE SET NULL")
    op.execute(f"CREATE INDEX IF NOT EXISTS {INDEX} ON group_attendance (day_off_id) WHERE day_off_id IS NOT NULL")
    op.execute(f"ALTER TABLE {AUDIT_TABLE} DROP CONSTRAINT IF EXISTS {AUDIT_CHECK}")
    op.execute(f"ALTER TABLE {AUDIT_TABLE} ADD CONSTRAINT {AUDIT_CHECK} CHECK ({AUDIT_NEW})")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    op.execute(f"DELETE FROM {AUDIT_TABLE} WHERE action = 'day_off_voided'")
    op.execute(f"ALTER TABLE {AUDIT_TABLE} DROP CONSTRAINT IF EXISTS {AUDIT_CHECK}")
    op.execute(f"ALTER TABLE {AUDIT_TABLE} ADD CONSTRAINT {AUDIT_CHECK} CHECK ({AUDIT_OLD})")
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(f"ALTER TABLE group_attendance DROP CONSTRAINT IF EXISTS {FK}")
    op.execute(f"ALTER TABLE group_attendance DROP CONSTRAINT IF EXISTS {KIND_CHECK}")
    columns = _columns(bind, "group_attendance")
    if "day_off_id" in columns:
        op.drop_column("group_attendance", "day_off_id")
    if "cancellation_kind" in columns:
        op.drop_column("group_attendance", "cancellation_kind")
    if "org_days_off" in _tables(bind):
        op.drop_table("org_days_off")
