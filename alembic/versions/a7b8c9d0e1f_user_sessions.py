"""One session per signed-in device: ``user_sessions`` + ``user_session_tokens`` (ROADMAP #101).

Revision ID: a7b8c9d0e1f
Revises: z6a7b8c9d0e
Create Date: 2026-10-17

Until this revision a user had exactly ONE refresh token, ``users.refresh_token_hash``, and every login and every
refresh overwrote it: the same person signed in on two devices (the owner on phone and laptop, one reception
account on two PCs, a teacher on phone and PC) was signed out of the first device at its next refresh (``401
Refresh token отозван``), at most one access-token lifetime (60 min) later.

``user_sessions`` - one row per signed-in device (the logic is ``src/auth/user_sessions.py``):

* ``user_id`` -> ``users.id`` ON DELETE CASCADE;
* ``created_at``, ``last_used_at`` (the last sign-in / refresh), ``expires_at`` (slides with every refresh),
  ``revoked_at`` NULL, ``revoke_reason`` NULL (``logout`` | ``refresh_reuse`` | ``password_changed`` |
  ``user_blocked`` | ``session_cap``) - all moments naive UTC (TIME_CONVENTIONS.md);
* ``user_agent`` (200) and ``ip`` (64), both NULL when unknown: the trusted client address, see the router.
  Privacy: they are kept until the session's ``expires_at`` plus the 30-day sweep window, then deleted with the row.

``user_session_tokens`` - ONE ROW PER REFRESH TOKEN ever issued to a session (only its SHA-256):

* ``token_hash`` PRIMARY KEY; the refresh route finds its session by it (no claim in the JWT is needed, so a token
  issued by the old code finds the token the backfill made for it);
* ``session_id`` -> ``user_sessions.id`` ON DELETE CASCADE, indexed;
* ``issued_at``; ``superseded_at`` NULL while the token is LIVE.

A refresh supersedes every live token of the session and issues one new live token. A token presented again within
60 s of its supersession (another tab of the same browser) is answered with a NEW live sibling and the other live
tokens stay live, so any number of simultaneous refreshes succeed; later it is a replay and revokes the session.
Superseded tokens are kept 7 days for that replay detection and then deleted (the replay of a deleted token is an
unknown token: a plain refusal that revokes nothing).

Indexes: ``ix_user_sessions_user_active`` (user_id) WHERE revoked_at IS NULL - the active sessions of a user (the
cap, the bulk revocation); ``ix_user_sessions_expires_at`` - the bounded sweep of long-expired sessions;
``ix_user_session_tokens_session`` (session_id) - the live tokens of a session; ``ix_user_session_tokens_superseded``
(superseded_at) WHERE NOT NULL - the sweep of old superseded tokens.

Backfill: every user with a non-empty ``users.refresh_token_hash`` gets ONE session (``created_at`` =
``last_used_at`` = now, ``expires_at`` = now + ``JWT_REFRESH_TTL_DAYS``, 30 by default) with that hash as its ONE
live token, so every CURRENT sign-in keeps working across the deploy; the refresh JWT keeps its own ``exp``.

``users.refresh_token_hash`` STAYS (rollback safety), but the code no longer reads or writes it after this
revision; a LATER migration drops it. Do not drop it before the image that stopped using it has been live and
the rollback window is over.

Idempotent: the tables, the indexes and the backfill tolerate a partial earlier run (a database built from the
models by ``create_all`` and stamped back); the backfill skips every user who already has a session (the legacy
column then holds a hash the code has rotated away since, and it must not come back to life).

Rollback (downgrade): the legacy column is made true again for the previous code - every user's column is cleared
(a stale hash must never revive a token that was superseded or signed out), then each user gets the newest LIVE
token of the live session used most recently; their other devices have to sign in again. Then both tables are
dropped. An older auth image cannot start on a database at this head (unknown revision): downgrade first, roll the
image back afterwards.
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

SESSIONS = "user_sessions"
TOKENS = "user_session_tokens"
ACTIVE_INDEX = "ix_user_sessions_user_active"
EXPIRES_INDEX = "ix_user_sessions_expires_at"
TOKEN_SESSION_INDEX = "ix_user_session_tokens_session"
TOKEN_SUPERSEDED_INDEX = "ix_user_session_tokens_superseded"

_NOW_UTC = "(now() AT TIME ZONE 'UTC')"


def _refresh_ttl_days() -> int:
    """The refresh lifetime the service uses (``JWT_REFRESH_TTL_DAYS``, default 30)."""
    try:
        return max(1, int(os.getenv("JWT_REFRESH_TTL_DAYS", "30")))
    except ValueError:
        return 30


def _index_names(inspector, table: str) -> set:
    return {ix["name"] for ix in inspector.get_indexes(table)}


def upgrade() -> None:
    # Fail fast instead of queueing behind a long transaction.
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if SESSIONS not in tables:
        op.create_table(
            SESSIONS,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("last_used_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("revoke_reason", sa.String(40), nullable=True),
            sa.Column("user_agent", sa.String(200), nullable=True),
            sa.Column("ip", sa.String(64), nullable=True),
        )
    if TOKENS not in tables:
        op.create_table(
            TOKENS,
            sa.Column("token_hash", sa.String(255), primary_key=True),
            sa.Column(
                "session_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("user_sessions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("issued_at", sa.DateTime(), nullable=False),
            sa.Column("superseded_at", sa.DateTime(), nullable=True),
        )
    inspector = sa.inspect(bind)
    session_indexes, token_indexes = _index_names(inspector, SESSIONS), _index_names(inspector, TOKENS)
    if ACTIVE_INDEX not in session_indexes:
        op.create_index(ACTIVE_INDEX, SESSIONS, ["user_id"], postgresql_where=sa.text("revoked_at IS NULL"))
    if EXPIRES_INDEX not in session_indexes:
        op.create_index(EXPIRES_INDEX, SESSIONS, ["expires_at"])
    if TOKEN_SESSION_INDEX not in token_indexes:
        op.create_index(TOKEN_SESSION_INDEX, TOKENS, ["session_id"])
    if TOKEN_SUPERSEDED_INDEX not in token_indexes:
        op.create_index(
            TOKEN_SUPERSEDED_INDEX, TOKENS, ["superseded_at"], postgresql_where=sa.text("superseded_at IS NOT NULL")
        )

    # Backfill: the one refresh token every user has today becomes that user's one session with one live token, so
    # every current sign-in survives the deploy. A user who already has a session is skipped: on a re-run (stamped
    # back) the legacy column holds a hash the code has rotated away since, and it must not come back to life. One
    # statement: the session and its token are created together.
    bind.execute(
        sa.text(
            f"""
            WITH legacy AS MATERIALIZED (
                SELECT DISTINCT ON (u.refresh_token_hash)
                       gen_random_uuid() AS session_id, u.id AS user_id, u.refresh_token_hash AS token_hash
                FROM users u
                WHERE u.refresh_token_hash IS NOT NULL AND u.refresh_token_hash <> ''
                  AND NOT EXISTS (SELECT 1 FROM user_sessions s WHERE s.user_id = u.id)
                  AND NOT EXISTS (SELECT 1 FROM user_session_tokens t WHERE t.token_hash = u.refresh_token_hash)
                ORDER BY u.refresh_token_hash, u.id
            ), made AS (
                INSERT INTO user_sessions (id, user_id, created_at, last_used_at, expires_at)
                SELECT session_id, user_id, {_NOW_UTC}, {_NOW_UTC},
                       {_NOW_UTC} + interval '1 day' * CAST(:ttl_days AS integer)
                FROM legacy
                RETURNING id
            )
            INSERT INTO user_session_tokens (token_hash, session_id, issued_at)
            SELECT l.token_hash, l.session_id, {_NOW_UTC}
            FROM legacy l JOIN made m ON m.id = l.session_id
            """
        ),
        {"ttl_days": _refresh_ttl_days()},
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # The previous code reads users.refresh_token_hash. Make it true again, and safe: no stale hash may revive a
    # token that was superseded or signed out meanwhile, so the column is cleared first and then holds, per user,
    # the newest LIVE token of the live session used most recently (the user's other devices sign in again).
    if SESSIONS in tables and TOKENS in tables and "refresh_token_hash" in {
        c["name"] for c in inspector.get_columns("users")
    }:
        op.execute("UPDATE users SET refresh_token_hash = NULL WHERE refresh_token_hash IS NOT NULL")
        op.execute(
            f"""
            UPDATE users u SET refresh_token_hash = s.token_hash
            FROM (
                SELECT DISTINCT ON (ss.user_id) ss.user_id, t.token_hash
                FROM user_sessions ss
                JOIN user_session_tokens t ON t.session_id = ss.id AND t.superseded_at IS NULL
                WHERE ss.revoked_at IS NULL AND ss.expires_at > {_NOW_UTC}
                ORDER BY ss.user_id, ss.last_used_at DESC, ss.created_at DESC, t.issued_at DESC, t.token_hash
            ) s
            WHERE s.user_id = u.id
            """
        )
    if TOKENS in tables:
        op.drop_table(TOKENS)  # its indexes go with it
    if SESSIONS in tables:
        op.drop_table(SESSIONS)
