"""Money-2 (the reconciliation of the rows): the baseline of the immutable money rows (invariant I-5) and what the run says about what it did not check.

Revision ID: mm21a1b2c3d4
Revises: mg13a1b2c3d4
Create Date: 2026-11-15

Only ADDS:
* ``money_row_baseline``: one row per money row (kind + id) with the fingerprint of its immutable fields, the sum, the «cancelled» sign and the runs that first / last confirmed it
  (``org_id ON DELETE CASCADE``, like the rows themselves);
* ``money_reconcile_runs.not_checked`` (what the night could not check, by reason) and ``.note`` (a re-baseline: the number of rows and the reason);
* ``money_reconcile_centres.rows_checked`` (this night looked at the rows of the centre - the next night knows the baseline is not new);
* the CHECKs widen: the finding invariant ``I-5``, the run status ``partial`` and ``rebaselined``, the journal action ``student_transferred`` (a transfer leaves a line in the journal of money decisions).

Rollback (downgrade): the baseline table is dropped and ``rows_checked`` is cleared (so the next «up» takes the baseline like a first night) (it holds nothing but what the next night takes again, and its foreign key to the runs would stop the older revision that drops them); the wider
CHECKs and the new columns stay - a row with a new value may already be there, an old reader ignores all of it.
"""
from __future__ import annotations

import re

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "mm21a1b2c3d4"
down_revision: str = "mg13a1b2c3d4"
branch_labels = None
depends_on = None

WIDEN = (
    ("group_audit", "ck_group_audit_action", "action", "student_transferred"),
    ("money_reconcile_findings", "ck_money_reconcile_findings_invariant", "invariant", "I-5"),
    ("money_reconcile_runs", "ck_money_reconcile_runs_status", "status", "partial"),
    ("money_reconcile_runs", "ck_money_reconcile_runs_status", "status", "rebaselined"),
)


def _widen(table: str, name: str, column: str, value: str) -> None:
    definition = op.get_bind().execute(
        sa.text("SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid WHERE t.relname = :t AND c.conname = :n"), {"t": table, "n": name},
    ).scalar()
    live = re.findall(r"'([^']+)'", definition or "")
    if not live or value in live:
        return
    listed = ", ".join(f"'{v}'" for v in [*live, value])
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({column} IN ({listed}))")


def _add_column(inspector, table: str, column: sa.Column) -> None:
    if column.name not in {c["name"] for c in inspector.get_columns(table)}:
        op.add_column(table, column)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    inspector = sa.inspect(op.get_bind())
    if "money_row_baseline" not in inspector.get_table_names():
        op.create_table(
            "money_row_baseline",
            sa.Column("kind", sa.String(16), primary_key=True),
            sa.Column("row_id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("fingerprint", sa.String(32), nullable=False),
            sa.Column("amount", sa.BigInteger(), nullable=True),
            sa.Column("voided", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("void_fingerprint", sa.String(32), nullable=True),
            sa.Column("accepted", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("first_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("money_reconcile_runs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("last_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("money_reconcile_runs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("first_seen_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("last_confirmed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_money_row_baseline_org", "money_row_baseline", ["org_id"])
    _add_column(inspector, "money_reconcile_runs", sa.Column("not_checked", postgresql.JSONB(), nullable=True))
    _add_column(inspector, "money_reconcile_runs", sa.Column("note", sa.String(500), nullable=True))
    _add_column(inspector, "money_reconcile_centres", sa.Column("rows_checked", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    for table, name, column, value in WIDEN:
        _widen(table, name, column, value)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    inspector = sa.inspect(op.get_bind())
    if "rows_checked" in {c["name"] for c in inspector.get_columns("money_reconcile_centres")}:
        op.execute("UPDATE money_reconcile_centres SET rows_checked = false")          # the baseline goes: after «down - up» the first night is the first night again (else every row is NEW and a finding)
    op.execute("DROP TABLE IF EXISTS money_row_baseline")          # a run, a finding or a journal line of the new kind may already be there: those stay
