"""K2-1: the mode of a session, the cut-over of the cookies, the invitation code as a hash, the dead activation columns.

Revision ID: k2a1b2c3d4e5
Revises: k3a1b2c3d4e5
Create Date: 2026-10-30

* **D** ``user_sessions.remember`` (NOT NULL, default false): a session without the tick is the short one (12 h from the sign-in); ``true`` slides 14 days.
* **m2 (cookie cut-over)** every session that is live when this revision runs is revoked (``revoke_reason = 'k2_cookie_switch'``): the cookies change their names
  (``__Host-``), so everybody signs in again anyway, and a refresh token copied before the switch must not be usable under the new name.
* **n3 (hashed invitation code)** ``invitations.code`` (the plain code of the e-mailed link) becomes ``invitations.code_hash`` (SHA-256 hex): a dump of the
  database holds no live link. Every invitation that is still pending is cancelled (before launch there are only test invitations; their plain codes are
  gone with the column).
* **n4** ``organizations.activation_token`` and ``activation_token_expires_at`` (the dead e-mail activation, removed with ``/auth/activate``) are dropped.

Idempotent (``IF [NOT] EXISTS`` / guarded blocks): a database built from the models and stamped back must upgrade. Downgrade restores the SHAPE (columns), not the
data: the plain codes cannot be recovered (``code`` gets the first 20 characters of the hash), the revoked sessions stay revoked.
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
    op.execute(
        "UPDATE user_sessions SET revoked_at = (now() AT TIME ZONE 'utc'), revoke_reason = 'k2_cookie_switch' WHERE revoked_at IS NULL"
    )

    op.execute("ALTER TABLE invitations ADD COLUMN IF NOT EXISTS code_hash VARCHAR(64)")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'invitations' AND column_name = 'code') THEN
                UPDATE invitations SET code_hash = encode(sha256(convert_to(code, 'UTF8')), 'hex') WHERE code_hash IS NULL;
            END IF;
        END $$
        """
    )
    op.execute("UPDATE invitations SET status = 'cancelled' WHERE status = 'pending' AND code_hash IS NOT NULL AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'invitations' AND column_name = 'code')")
    op.execute("ALTER TABLE invitations ALTER COLUMN code_hash SET NOT NULL")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'invitations_code_hash_key') THEN
                ALTER TABLE invitations ADD CONSTRAINT invitations_code_hash_key UNIQUE (code_hash);
            END IF;
        END $$
        """
    )
    op.execute("ALTER TABLE invitations DROP COLUMN IF EXISTS code")

    op.execute("ALTER TABLE organizations DROP COLUMN IF EXISTS activation_token")
    op.execute("ALTER TABLE organizations DROP COLUMN IF EXISTS activation_token_expires_at")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS activation_token VARCHAR(500)")
    op.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS activation_token_expires_at TIMESTAMP")

    op.execute("ALTER TABLE invitations ADD COLUMN IF NOT EXISTS code VARCHAR(20)")
    op.execute("UPDATE invitations SET code = left(code_hash, 20) WHERE code IS NULL")
    op.execute("ALTER TABLE invitations ALTER COLUMN code SET NOT NULL")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'invitations_code_key') THEN
                ALTER TABLE invitations ADD CONSTRAINT invitations_code_key UNIQUE (code);
            END IF;
        END $$
        """
    )
    op.execute("ALTER TABLE invitations DROP COLUMN IF EXISTS code_hash")

    op.execute("ALTER TABLE user_sessions DROP COLUMN IF EXISTS remember")
