"""Money-1 (pack A): the journal of reopened months, the nightly reconciliation, two new actions of the attendance audit.

Revision ID: mn1a2b3c4d5
Revises: wca1b2c3d4e5
Create Date: 2026-11-04

* ``payroll_period_reopenings`` (A1): a closed month of a teacher (his last sheet was saved after the month ended) reopened by the owner / an administrator with the payments
  right, with a reason.  Append-only; a reopening lasts until the sheet is saved again.
* ``money_reconcile_runs`` / ``money_reconcile_findings`` (A7): the result of the nightly check of the invariants I-1..I-4 (the platform console reads them; a centre never does).
* ``group_attendance_audit`` accepts ``completed_reverted`` («Вернуть проведённый урок», A2) and ``cancel_reverted`` («Вернуть "Урока не было"», A2); the CHECK is rebuilt from the
  live definition, so a parallel change of the list by another revision is kept.

Only ADDS (no data is moved).  Rollback (downgrade): drops the three tables and removes the audit rows with the two new actions (they have no meaning for the old code), then restores
the CHECK.  The journal of reopenings is lost with the table - after the downgrade the old code closes nothing.
"""
from __future__ import annotations

import re

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "mn1a2b3c4d5"
down_revision: str = "wca1b2c3d4e5"
branch_labels = None
depends_on = None

AUDIT = "group_attendance_audit"
AUDIT_CHECK = "ck_group_attendance_audit_action"
NEW_ACTIONS = ("completed_reverted", "cancel_reverted")


def _check_actions(bind) -> list[str]:
    row = bind.execute(
        sa.text("SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid WHERE t.relname = :t AND c.conname = :n"),
        {"t": AUDIT, "n": AUDIT_CHECK},
    ).scalar()
    return re.findall(r"'([a-z_]+)'", row or "")


def _set_check_actions(actions: list[str]) -> None:
    op.execute(f"ALTER TABLE {AUDIT} DROP CONSTRAINT IF EXISTS {AUDIT_CHECK}")
    listed = ", ".join(f"'{a}'" for a in actions)
    op.execute(f"ALTER TABLE {AUDIT} ADD CONSTRAINT {AUDIT_CHECK} CHECK (action IN ({listed}))")


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    tables = _tables(bind)
    if "payroll_period_reopenings" not in tables:
        op.create_table(
            "payroll_period_reopenings",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("teacher_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("period_month", sa.Date(), nullable=False),
            sa.Column("sheet_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("payroll_sheets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("sheet_revision", sa.Integer(), nullable=False),
            sa.Column("reopened_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("reason", sa.String(500), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("EXTRACT(DAY FROM period_month) = 1", name="ck_payroll_reopenings_first_day"),
            sa.CheckConstraint("char_length(btrim(reason)) >= 3", name="ck_payroll_reopenings_reason"),
        )
        op.create_index("ix_payroll_reopenings_teacher_month", "payroll_period_reopenings", ["org_id", "teacher_id", "period_month", "created_at"])
    if "money_reconcile_runs" not in tables:
        op.create_table(
            "money_reconcile_runs",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("status", sa.String(12), nullable=False, server_default="running"),
            sa.Column("centres_checked", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("findings_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("new_findings_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("mail_status", sa.String(12), nullable=True),
            sa.Column("error", sa.String(300), nullable=True),
            sa.CheckConstraint("status IN ('running', 'clean', 'findings', 'failed')", name="ck_money_reconcile_runs_status"),
        )
        op.create_index("ix_money_reconcile_runs_started", "money_reconcile_runs", ["started_at"])
    if "money_reconcile_findings" not in tables:
        op.create_table(
            "money_reconcile_findings",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("money_reconcile_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("invariant", sa.String(4), nullable=False),
            sa.Column("subject_key", sa.String(200), nullable=False),
            sa.Column("subject_id", sa.String(64), nullable=True),
            sa.Column("month", sa.String(7), nullable=True),
            sa.Column("expected", sa.BigInteger(), nullable=True),
            sa.Column("actual", sa.BigInteger(), nullable=True),
            sa.Column("details", postgresql.JSONB(), nullable=True),
            sa.Column("is_new", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("invariant IN ('I-1', 'I-2', 'I-3', 'I-4')", name="ck_money_reconcile_findings_invariant"),
        )
        op.create_index("ix_money_reconcile_findings_run", "money_reconcile_findings", ["run_id"])
        op.create_index("ix_money_reconcile_findings_org_key", "money_reconcile_findings", ["org_id", "invariant", "subject_key"])
    actions = _check_actions(bind)
    if actions:
        _set_check_actions(actions + [a for a in NEW_ACTIONS if a not in actions])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    actions = _check_actions(bind)
    if actions:
        op.execute(f"DELETE FROM {AUDIT} WHERE action IN ({', '.join(repr(a) for a in NEW_ACTIONS)})")
        _set_check_actions([a for a in actions if a not in NEW_ACTIONS])
    tables = _tables(bind)
    for name in ("money_reconcile_findings", "money_reconcile_runs", "payroll_period_reopenings"):
        if name in tables:
            op.drop_table(name)
