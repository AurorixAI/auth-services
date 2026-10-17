"""One session per signed-in device: ``user_sessions`` (ROADMAP #101).

Revision ID: a7b8c9d0e1f
Revises: z6a7b8c9d0e
Create Date: 2026-10-17

Until this revision a user had exactly ONE refresh token, ``users.refresh_token_hash``, and every login and every
refresh overwrote it: the same person signed in on two devices (the owner on phone and laptop, one reception
account on two PCs, a teacher on phone and PC) was signed out of the first device at its next refresh (``401
Refresh token отозван``), at most one access-token lifetime (60 min) later.

``user_sessions`` - one row per signed-in device (the logic is ``src/auth/user_sessions.py``):

* ``user_id`` -> ``users.id`` ON DELETE CASCADE;
* ``refresh_token_hash`` UNIQUE NOT NULL - SHA-256 of the CURRENT refresh token of the device; the refresh route
  finds the session by it (no claim in the JWT is needed, so a token issued by the old code finds the row the
  backfill made for it);
* ``previous_token_hash`` / ``rotated_at`` - the token a refresh replaced and when: a concurrent refresh of the
  same browser (another tab) that still sends it within 60 s is answered with a valid pair; the same token later
  is a replay and revokes THAT session;
* ``created_at``, ``last_used_at``, ``expires_at`` (slides with every rotation), ``revoked_at`` NULL,
  ``revoke_reason`` NULL (``logout`` | ``refresh_reuse`` | ``password_changed`` | ``user_blocked`` |
  ``session_cap``) - all moments naive UTC (TIME_CONVENTIONS.md);
* ``user_agent`` (200) and ``ip`` (64), both NULL when unknown: the trusted client address, see the router.

Indexes: ``ix_user_sessions_user_active`` (user_id) WHERE revoked_at IS NULL - the active sessions of a user (the
cap, the bulk revocation); ``ix_user_sessions_previous_hash`` (previous_token_hash) WHERE NOT NULL - the lookup
of a just-rotated token; ``ix_user_sessions_expires_at`` - the bounded sweep of long-expired rows.

Backfill: every user with a non-empty ``users.refresh_token_hash`` gets ONE session with that hash (``created_at``
= ``last_used_at`` = now, ``expires_at`` = now + ``JWT_REFRESH_TTL_DAYS``, 30 by default), so every CURRENT sign-in
keeps working across the deploy; the refresh JWT keeps its own ``exp``.

``users.refresh_token_hash`` STAYS (rollback safety), but the code no longer reads or writes it after this
revision; a LATER migration drops it. Do not drop it before the image that stopped using it has been live and
the rollback window is over.

Idempotent: the table, the indexes and the backfill tolerate a partial earlier run (a database built from the
models by ``create_all`` and stamped back); the backfill skips every user who already has a session.

Rollback (downgrade): the legacy column is made true again for the previous code - every user's column is cleared
(a stale hash must never revive a token that was rotated away or signed out), then each user gets the CURRENT
token of the live session used most recently; their other devices have to sign in again. Then the table is
dropped. An older auth image cannot start on a database at this head (unknown revision): downgrade first, roll
the image back afterwards.
"""
from __future__ import annotations

import os

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a7b8c9d0e1f"
down_revision: str = "z6a7b8c9d0e"
branch_labels = None
depends_on = None

TABLE = "user_sessions"
ACTIVE_INDEX = "ix_user_sessions_user_active"
PREVIOUS_INDEX = "ix_user_sessions_previous_hash"
EXPIRES_INDEX = "ix_user_sessions_expires_at"
UNIQUE_HASH = "uq_user_sessions_refresh_token_hash"

_NOW_UTC = "(now() AT TIME ZONE 'UTC')"


def _refresh_ttl_days() -> int:
    """The refresh lifetime the service uses (``JWT_REFRESH_TTL_DAYS``, default 30)."""
    try:
        return max(1, int(os.getenv("JWT_REFRESH_TTL_DAYS", "30")))
    except ValueError:
        return 30


def _index_names(inspector) -> set:
    return {ix["name"] for ix in inspector.get_indexes(TABLE)}


def upgrade() -> None:
    # Fail fast instead of queueing behind a long transaction.
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE not in set(inspector.get_table_names()):
        op.create_table(
            TABLE,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("refresh_token_hash", sa.String(255), nullable=False),
            sa.Column("previous_token_hash", sa.String(255), nullable=True),
            sa.Column("rotated_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("last_used_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("revoke_reason", sa.String(40), nullable=True),
            sa.Column("user_agent", sa.String(200), nullable=True),
            sa.Column("ip", sa.String(64), nullable=True),
            sa.UniqueConstraint("refresh_token_hash", name=UNIQUE_HASH),
        )
    inspector = sa.inspect(bind)
    indexes = _index_names(inspector)
    unique_columns = [
        tuple(c["column_names"]) for c in inspector.get_unique_constraints(TABLE)
    ] + [tuple(ix["column_names"]) for ix in inspector.get_indexes(TABLE) if ix.get("unique")]
    if ("refresh_token_hash",) not in unique_columns:
        # A table made from the model by create_all carries its own unique constraint; this only repairs a table
        # that somehow has none.
        op.create_unique_constraint(UNIQUE_HASH, TABLE, ["refresh_token_hash"])
    if ACTIVE_INDEX not in indexes:
        op.create_index(ACTIVE_INDEX, TABLE, ["user_id"], postgresql_where=sa.text("revoked_at IS NULL"))
    if PREVIOUS_INDEX not in indexes:
        op.create_index(
            PREVIOUS_INDEX, TABLE, ["previous_token_hash"], postgresql_where=sa.text("previous_token_hash IS NOT NULL")
        )
    if EXPIRES_INDEX not in indexes:
        op.create_index(EXPIRES_INDEX, TABLE, ["expires_at"])

    # Backfill: the one refresh token every user has today becomes that user's one session, so every current
    # sign-in survives the deploy. A user who already has a session is skipped: on a re-run (stamped back) the
    # legacy column holds a hash the code has rotated away since, and it must not come back to life.
    op.get_bind().execute(
        sa.text(
            f"""
            INSERT INTO user_sessions (id, user_id, refresh_token_hash, created_at, last_used_at, expires_at)
            SELECT gen_random_uuid(), u.id, u.refresh_token_hash, {_NOW_UTC}, {_NOW_UTC},
                   {_NOW_UTC} + interval '1 day' * CAST(:ttl_days AS integer)
            FROM users u
            WHERE u.refresh_token_hash IS NOT NULL AND u.refresh_token_hash <> ''
              AND NOT EXISTS (SELECT 1 FROM user_sessions s WHERE s.user_id = u.id)
            ON CONFLICT (refresh_token_hash) DO NOTHING
            """
        ),
        {"ttl_days": _refresh_ttl_days()},
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in set(inspector.get_table_names()):
        return

    # The previous code reads users.refresh_token_hash. Make it true again, and safe: no stale hash may revive a
    # token that was rotated away or signed out meanwhile, so the column is cleared first and then holds, per
    # user, the CURRENT token of the live session used most recently (the user's other devices sign in again).
    if "refresh_token_hash" in {c["name"] for c in inspector.get_columns("users")}:
        op.execute("UPDATE users SET refresh_token_hash = NULL WHERE refresh_token_hash IS NOT NULL")
        op.execute(
            f"""
            UPDATE users u SET refresh_token_hash = s.refresh_token_hash
            FROM (
                SELECT DISTINCT ON (user_id) user_id, refresh_token_hash
                FROM user_sessions
                WHERE revoked_at IS NULL AND expires_at > {_NOW_UTC}
                ORDER BY user_id, last_used_at DESC, created_at DESC
            ) s
            WHERE s.user_id = u.id
            """
        )
    op.drop_table(TABLE)  # its indexes and the unique constraint go with it
