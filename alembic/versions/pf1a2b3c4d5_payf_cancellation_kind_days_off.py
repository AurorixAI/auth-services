"""PAY-F (25.10): the kind of a lesson cancellation and the days off of a centre.

Revision ID: pf1a2b3c4d5
Revises: cn1a2b3c4d5
Create Date: 2026-10-25

New table ``org_days_off`` (a date or a range of up to 14 days with a reason: the journal of «Выходной день центра»).
New columns of ``group_attendance``: ``cancellation_kind`` (``centre`` | ``holiday`` | NULL) and ``day_off_id`` (the day off that cancelled the lesson).
Old cancellations keep ``cancellation_kind`` NULL: they are never recalculated.

Idempotent: a table or a column that already exists is skipped.
Rollback (downgrade): drops the two columns and the table - the kinds of the cancellations and the journal of the days off are LOST (the cancelled lessons
stay cancelled; with the kinds gone nothing is deducted any more, which is the behaviour before the release).
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


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


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
            sa.Column("created_by", UUID, sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=NOW_UTC),
            sa.Column("voided_at", sa.DateTime(), nullable=True),
            sa.Column("voided_by", UUID, sa.ForeignKey("users.id"), nullable=True),
            sa.CheckConstraint("date_to >= date_from AND date_to - date_from <= 13", name="ck_org_days_off_range"),
            sa.CheckConstraint("char_length(btrim(reason)) >= 3", name="ck_org_days_off_reason"),
        )
        op.create_index("idx_org_days_off_org_dates", "org_days_off", ["org_id", "date_from", "date_to"])
    columns = _columns(bind, "group_attendance")
    if "cancellation_kind" not in columns:
        op.add_column("group_attendance", sa.Column("cancellation_kind", sa.String(20), nullable=True))
        op.create_check_constraint(KIND_CHECK, "group_attendance", "cancellation_kind IS NULL OR cancellation_kind IN ('centre', 'holiday')")
    if "day_off_id" not in columns:
        op.add_column("group_attendance", sa.Column("day_off_id", UUID, sa.ForeignKey("org_days_off.id", ondelete="SET NULL"), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    columns = _columns(bind, "group_attendance")
    if "day_off_id" in columns:
        op.drop_column("group_attendance", "day_off_id")
    if "cancellation_kind" in columns:
        op.drop_constraint(KIND_CHECK, "group_attendance", type_="check")
        op.drop_column("group_attendance", "cancellation_kind")
    if "org_days_off" in _tables(bind):
        op.drop_table("org_days_off")
