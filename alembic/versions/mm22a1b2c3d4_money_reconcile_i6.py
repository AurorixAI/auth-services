"""PAY-TAILS Б10: the finding invariant ``I-6`` («пробная запись меняет счёт не так, как должна») of the nightly reconciliation.

Revision ID: mm22a1b2c3d4
Revises: mm21a1b2c3d4
Create Date: 2026-11-22

Only WIDENS one CHECK (``ck_money_reconcile_findings_invariant``: + ``'I-6'``); no data, no column.  Rollback (downgrade): nothing is undone - a finding with the new value may already be
stored (the same rule as the migration of I-5); an old reader ignores it.
"""
from __future__ import annotations

import re

from alembic import op
import sqlalchemy as sa

revision: str = "mm22a1b2c3d4"
down_revision: str = "mm21a1b2c3d4"
branch_labels = None
depends_on = None

TABLE, NAME, COLUMN, VALUE = "money_reconcile_findings", "ck_money_reconcile_findings_invariant", "invariant", "I-6"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    definition = op.get_bind().execute(
        sa.text("SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid WHERE t.relname = :t AND c.conname = :n"), {"t": TABLE, "n": NAME},
    ).scalar()
    live = re.findall(r"'([^']+)'", definition or "")
    if not live or VALUE in live:
        return
    listed = ", ".join(f"'{v}'" for v in [*live, VALUE])
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {NAME}")
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {NAME} CHECK ({COLUMN} IN ({listed})) NOT VALID")
    op.execute(f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {NAME}")


def downgrade() -> None:
    pass
