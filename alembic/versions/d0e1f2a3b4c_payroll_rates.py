"""Teacher payroll rates: table ``payroll_rates`` + transfer of the percents of the saved sheets (UX-2).

Revision ID: d0e1f2a3b4c
Revises: c9d0e1f2a3b
Create Date: 2026-10-21

Owner decision (21.10): the percent of a teacher is set in the teacher's card, not on the payroll page.  The percent is
valid FROM A MONTH (``valid_from`` = the 1st day) and is looked up per (teacher, kind of lessons): the rate for month M
is the row with the greatest ``valid_from <= 1st of M`` (ties: the later ``created_at``).  The table is append-only.

Transfer (backfill).  Until now the percent lived only in ``payroll_sheets.percent_inputs``.  For every (centre, teacher,
kind of lessons) found in the saved sheets the months are walked from the earliest: the percent of the first sheet is
written with ``valid_from`` = its month, and one more row for every later month whose percent DIFFERS from the previous
one.  So every saved month keeps the rate it was calculated with (the lookup of a month without a sheet gives the last
earlier rate) and the rate of the latest sheet goes on being valid for the months after it.  ``created_by`` is NULL and
``note`` = «перенесено из ведомостей».  A value that is not a percent is skipped.

Idempotent: a table that is already there (a database built by ``create_all`` and stamped back) is left as it is and so
is its data - the transfer runs only when this migration creates the table.

Rollback (downgrade): drops the table - the rates set after the release are lost, the sheets are not touched (they keep
their own ``percent_inputs``).  An older auth image cannot start on a database at this head (unknown revision):
downgrade first, roll the image back afterwards.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from uuid import uuid4

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d0e1f2a3b4c"
down_revision: str = "c9d0e1f2a3b"
branch_labels = None
depends_on = None

TABLE = "payroll_rates"
LESSON_TYPES = ("group", "mini_group", "pair", "individual")
NOTE = "перенесено из ведомостей"


def _percent(value):
    try:
        number = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0 or number > 100 or number != number.quantize(Decimal("0.01")):
        return None
    return number


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    if TABLE in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("teacher_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("lesson_type", sa.String(16), nullable=False),
        sa.Column("percent", sa.Numeric(5, 2), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("lesson_type IN ('group', 'mini_group', 'pair', 'individual')", name="ck_payroll_rates_lesson_type"),
        sa.CheckConstraint("percent >= 0 AND percent <= 100", name="ck_payroll_rates_percent"),
        sa.CheckConstraint("EXTRACT(DAY FROM valid_from) = 1", name="ck_payroll_rates_first_day"),
    )
    op.create_index("idx_payroll_rates_lookup", TABLE, ["org_id", "teacher_id", "lesson_type", "valid_from"])

    sheets = bind.execute(
        sa.text(
            "SELECT org_id, teacher_id, period_month, percent_inputs FROM payroll_sheets "
            "ORDER BY org_id, teacher_id, period_month"
        )
    ).fetchall()
    last = {}  # (org, teacher, kind) -> the percent of the last row written
    rows = []
    for org_id, teacher_id, month, inputs in sheets:
        for lesson_type, value in sorted((inputs or {}).items()):
            percent = _percent(value)
            if lesson_type not in LESSON_TYPES or percent is None:
                continue
            key = (org_id, teacher_id, lesson_type)
            if last.get(key) != percent:
                last[key] = percent
                rows.append(
                    {"id": uuid4(), "o": org_id, "t": teacher_id, "k": lesson_type, "p": percent, "v": month, "n": NOTE}
                )
    if rows:
        bind.execute(
            sa.text(
                "INSERT INTO payroll_rates (id, org_id, teacher_id, lesson_type, percent, valid_from, note, created_by, created_at) "
                "VALUES (:id, :o, :t, :k, :p, :v, :n, NULL, now())"
            ),
            rows,
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    if TABLE in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table(TABLE)
