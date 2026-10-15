"""Password recovery: one-time reset links and revocation of old logins (N13).

Revision ID: v2w3x4y5z6a
Revises: u1v2w3x4y5z
Create Date: 2026-10-15

* ``password_reset_tokens`` - the links of «Забыли пароль?» (purpose 'email', 1 hour) and the links an
  owner / admin hands over (purpose 'issued', 24 hours).  Only the sha256 of the token is stored
  (``token_hash``, unique); ``created_by`` is the person who issued an 'issued' link (SET NULL when that
  account is deleted), ``used_at`` is set when the password was really changed with the link, ``ip`` is
  the address of the request.  Rows follow their user (ON DELETE CASCADE).
* ``users.sessions_valid_after`` - timestamptz, NULL by default: access JWTs issued before it are
  rejected, so a password reset ends the old logins at once instead of when the 60-minute JWT expires.

Idempotent: the table, the column and the index tolerate a partial earlier run or a database built
from the models (``create_all``).

Rollback: the downgrade drops the table (outstanding links stop working, nothing else is affected) and
the column.  An older auth image cannot start on a database at this head (unknown revision): downgrade
first, roll the image back afterwards.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "v2w3x4y5z6a"
down_revision: str = "u1v2w3x4y5z"
branch_labels = None
depends_on = None

TABLE = "password_reset_tokens"
USER_INDEX = "idx_password_reset_tokens_user"


def upgrade() -> None:
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
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("purpose", sa.String(length=10), nullable=False),
            sa.Column(
                "created_by",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ip", sa.String(length=45), nullable=True),
            sa.UniqueConstraint("token_hash", name="uq_password_reset_tokens_hash"),
            sa.CheckConstraint(
                "purpose IN ('email', 'issued')", name="ck_password_reset_tokens_purpose"
            ),
        )
    inspector = sa.inspect(bind)
    if USER_INDEX not in {ix["name"] for ix in inspector.get_indexes(TABLE)}:
        op.create_index(USER_INDEX, TABLE, ["user_id"])

    if "sessions_valid_after" not in {c["name"] for c in inspector.get_columns("users")}:
        op.add_column(
            "users", sa.Column("sessions_valid_after", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE in set(inspector.get_table_names()):
        op.drop_table(TABLE)
    if "sessions_valid_after" in {c["name"] for c in inspector.get_columns("users")}:
        op.drop_column("users", "sessions_valid_after")
