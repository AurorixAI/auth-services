"""Platform console, pack «Консоль-1» (25.10): demo requests, console staff / roles / sessions / audit, password links, centre and user blocks.

Revision ID: cn1a2b3c4d5
Revises: sa1b2c3d4e5
Create Date: 2026-10-25

New tables: ``demo_requests``, ``demo_request_notes``, ``platform_roles`` (+ the five template roles), ``platform_staff``,
``platform_sessions``, ``platform_audit``, ``password_links``.
New columns: ``organizations.blocked_at / blocked_reason``; ``users.platform_blocked_at / platform_block_reason``.

Idempotent: a table or column that already exists (the baseline revision creates ``organizations`` and ``users`` from today's models) is skipped.
Rollback (downgrade): drops the seven tables and the four columns - the requests, the staff, the journal and the block marks are LOST;
nothing outside the console reads them. A centre that was blocked by the console becomes usable again.
"""
from __future__ import annotations

import json
from uuid import uuid4

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "cn1a2b3c4d5"
down_revision: str = "sa1b2c3d4e5"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
NOW_UTC = sa.text("timezone('utc', now())")

ALL_PERMISSIONS = [
    "requests.view", "requests.manage", "orgs.view", "orgs.manage", "orgs.block", "users.view", "users.manage", "users.block",
    "audit.view", "team.manage", "support.manage", "impersonate.view", "impersonate.write", "content.view", "finance.view",
    "ai_costs.view", "analytics.view",
]
TEMPLATES = [
    ("Владелец платформы", ALL_PERMISSIONS, True),
    ("Продажи", ["requests.view", "requests.manage", "orgs.view"], False),
    ("Поддержка", ["requests.view", "orgs.view", "users.view", "users.manage", "support.manage", "impersonate.view"], False),
    ("Контент", ["content.view"], False),
    ("Финансы", ["finance.view", "orgs.view"], False),
]


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _index(bind, table: str, name: str) -> bool:
    return name in {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()

    if "blocked_at" not in _columns(bind, "organizations"):
        op.add_column("organizations", sa.Column("blocked_at", sa.DateTime(), nullable=True))
    if "blocked_reason" not in _columns(bind, "organizations"):
        op.add_column("organizations", sa.Column("blocked_reason", sa.Text(), nullable=True))
    if "platform_blocked_at" not in _columns(bind, "users"):
        op.add_column("users", sa.Column("platform_blocked_at", sa.DateTime(), nullable=True))
    if "platform_block_reason" not in _columns(bind, "users"):
        op.add_column("users", sa.Column("platform_block_reason", sa.Text(), nullable=True))

    tables = _tables(bind)
    if "platform_roles" not in tables:
        op.create_table(
            "platform_roles",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("name", sa.String(80), nullable=False, unique=True),
            sa.Column("permissions", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("is_template", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=NOW_UTC),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=NOW_UTC),
        )
    roles = sa.table(
        "platform_roles",
        sa.column("id", UUID), sa.column("name", sa.String), sa.column("permissions", postgresql.JSONB),
        sa.column("is_template", sa.Boolean), sa.column("is_system", sa.Boolean),
    )
    existing = {r[0] for r in bind.execute(sa.text("SELECT name FROM platform_roles"))}
    for name, permissions, system in TEMPLATES:
        if name not in existing:
            bind.execute(roles.insert().values(id=uuid4(), name=name, permissions=permissions, is_template=True, is_system=system))

    if "platform_staff" not in tables:
        op.create_table(
            "platform_staff",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
            sa.Column("role_id", UUID, sa.ForeignKey("platform_roles.id"), nullable=False),
            sa.Column("is_owner", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("failed_logins", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("locked_until", sa.DateTime()),
            sa.Column("last_login_at", sa.DateTime()),
            sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=NOW_UTC),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=NOW_UTC),
        )

    if "platform_sessions" not in tables:
        op.create_table(
            "platform_sessions",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime()),
            sa.Column("revoke_reason", sa.String(40)),
            sa.Column("ip", sa.String(64)),
            sa.Column("user_agent", sa.String(200)),
        )
        op.create_index(
            "ix_platform_sessions_user_active", "platform_sessions", ["user_id"], postgresql_where=sa.text("revoked_at IS NULL")
        )

    if "platform_audit" not in tables:
        op.create_table(
            "platform_audit",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("actor_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("action", sa.String(60), nullable=False),
            sa.Column("target_type", sa.String(30)),
            sa.Column("target_id", sa.String(64)),
            sa.Column("reason", sa.Text()),
            sa.Column("details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("ip", sa.String(64)),
            sa.Column("user_agent", sa.String(200)),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=NOW_UTC),
        )
        op.create_index("ix_platform_audit_created", "platform_audit", ["created_at"])
        op.create_index("ix_platform_audit_target", "platform_audit", ["target_type", "target_id", "created_at"])
        op.create_index("ix_platform_audit_actor", "platform_audit", ["actor_user_id", "created_at"])

    if "demo_requests" not in tables:
        op.create_table(
            "demo_requests",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("contact_name", sa.String(120), nullable=False),
            sa.Column("centre_name", sa.String(200), nullable=False),
            sa.Column("city", sa.String(100), nullable=False),
            sa.Column("phone", sa.String(30), nullable=False),
            sa.Column("email", sa.String(255), nullable=False),
            sa.Column("students_count", sa.Integer()),
            sa.Column("comment", sa.Text()),
            sa.Column("source", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("ip_hash", sa.String(64)),
            sa.Column("status", sa.String(20), nullable=False, server_default="new"),
            sa.Column("decline_reason", sa.Text()),
            sa.Column("is_repeat", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("repeat_of_id", UUID),
            sa.Column("org_id", UUID, sa.ForeignKey("organizations.id", ondelete="SET NULL")),
            sa.Column("handled_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=NOW_UTC),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=NOW_UTC),
            sa.CheckConstraint(
                "status IN ('new', 'contacted', 'demo_done', 'approved', 'declined', 'spam')", name="ck_demo_requests_status"
            ),
        )
        op.create_index("ix_demo_requests_status_created", "demo_requests", ["status", "created_at"])
        op.create_index("ix_demo_requests_email", "demo_requests", ["email"])

    if "demo_request_notes" not in tables:
        op.create_table(
            "demo_request_notes",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("request_id", UUID, sa.ForeignKey("demo_requests.id", ondelete="CASCADE"), nullable=False),
            sa.Column("author_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=NOW_UTC),
        )
        op.create_index("ix_demo_request_notes_request", "demo_request_notes", ["request_id", "created_at"])

    if "password_links" not in tables:
        op.create_table(
            "password_links",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("purpose", sa.String(20), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("used_at", sa.DateTime()),
            sa.Column("revoked_at", sa.DateTime()),
            sa.Column("created_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=NOW_UTC),
            sa.CheckConstraint("purpose IN ('centre_invite', 'reset')", name="ck_password_links_purpose"),
        )
        op.create_index("ix_password_links_user", "password_links", ["user_id", "created_at"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    tables = _tables(bind)
    # children before parents
    for table in (
        "password_links", "demo_request_notes", "demo_requests", "platform_audit", "platform_sessions", "platform_staff",
        "platform_roles",
    ):
        if table in tables:
            op.drop_table(table)
    for table, column in (
        ("users", "platform_block_reason"), ("users", "platform_blocked_at"),
        ("organizations", "blocked_reason"), ("organizations", "blocked_at"),
    ):
        if column in _columns(bind, table):
            op.drop_column(table, column)
