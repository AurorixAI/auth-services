"""K2-1 (D): the sign-in mode of a session - «Запомнить меня» is a property of the server-side session row.

Revision ID: k2a1b2c3d4e5
Revises: k3a1b2c3d4e5
Create Date: 2026-10-30

Only adds one column: ``user_sessions.remember`` (NOT NULL, default false: a session without the tick is the short one, 12 h from the sign-in).
``remember = true`` slides 14 days at every refresh. The mode survives every rotation of the refresh token because it lives on the session row,
not in the token. No data migration (nothing is read from old rows: they were all issued with the old 30-day rule and expire by themselves).

Idempotent (``IF NOT EXISTS``), like the other revisions of this line. Rollback (downgrade) drops the column; the code of the previous revision does not read it.
"""
from __future__ import annotations

from alembic import op

revision: str = "k2a1b2c3d4e5"
down_revision: str = "k3a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE user_sessions ADD COLUMN IF NOT EXISTS remember BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE user_sessions DROP COLUMN IF EXISTS remember")
