"""K4a «Поддержка»: the tickets of the employees of a centre to the ALGO team, their thread, their screenshots.

Revision ID: k4a1b2c3d4e5
Revises: k2b1b2c3d4e5
Create Date: 2026-10-31

* ``support_tickets`` (a running number from ``support_ticket_number_seq``), ``support_messages``, ``support_attachments`` (the screenshots, ``bytea`` like the cheques).
* The right ``support.manage`` is in the template roles «Владелец платформы» and «Поддержка» (the seed of «Консоль-1» already carried it; this only makes sure).

Only ADDS. Idempotent (``IF NOT EXISTS``); the downgrade drops the three tables and the sequence (the right in the roles stays: a reserved key).
"""
from __future__ import annotations

from alembic import op

revision: str = "k4a1b2c3d4e5"
down_revision: str = "k2b1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("CREATE SEQUENCE IF NOT EXISTS support_ticket_number_seq")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS support_tickets (
            id UUID PRIMARY KEY,
            number INTEGER NOT NULL DEFAULT nextval('support_ticket_number_seq') UNIQUE,
            org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            author_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            author_name VARCHAR(200) NOT NULL DEFAULT '',
            author_role VARCHAR(20) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'new',
            text TEXT NOT NULL,
            page_template VARCHAR(300) NOT NULL DEFAULT '',
            browser VARCHAR(200) NOT NULL DEFAULT '',
            client_errors JSONB NOT NULL DEFAULT '[]'::jsonb,
            client_key UUID,
            team_mail_status VARCHAR(10),
            created_at TIMESTAMP NOT NULL DEFAULT timezone('utc', now()),
            updated_at TIMESTAMP NOT NULL DEFAULT timezone('utc', now()),
            last_author_reply_at TIMESTAMP,
            last_staff_reply_at TIMESTAMP,
            resolved_at TIMESTAMP,
            resolved_by UUID REFERENCES users(id) ON DELETE SET NULL,
            resolved_kind VARCHAR(10),
            author_seen_at TIMESTAMP,
            team_mail_at TIMESTAMP,
            author_mail_at TIMESTAMP,
            CONSTRAINT ck_support_tickets_status CHECK (status IN ('new', 'in_progress', 'waiting', 'resolved')),
            CONSTRAINT ck_support_tickets_resolved_kind CHECK (resolved_kind IS NULL OR resolved_kind IN ('manual', 'auto')),
            CONSTRAINT ck_support_tickets_author_role CHECK (author_role IN ('owner', 'admin', 'teacher')),
            CONSTRAINT ck_support_tickets_mail_status CHECK (team_mail_status IS NULL OR team_mail_status IN ('pending', 'sent', 'failed', 'skipped', 'throttled'))
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_support_tickets_author ON support_tickets (author_user_id, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_support_tickets_org ON support_tickets (org_id, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_support_tickets_status ON support_tickets (status, updated_at)")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_support_tickets_client_key ON support_tickets (author_user_id, client_key) WHERE client_key IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS support_messages (
            id UUID PRIMARY KEY,
            ticket_id UUID NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
            author_kind VARCHAR(10) NOT NULL,
            author_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT timezone('utc', now()),
            mail_status VARCHAR(10),
            mail_status_at TIMESTAMP,
            CONSTRAINT ck_support_messages_author_kind CHECK (author_kind IN ('user', 'staff')),
            CONSTRAINT ck_support_messages_mail_status CHECK (mail_status IS NULL OR mail_status IN ('pending', 'sent', 'failed', 'skipped', 'throttled'))
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_support_messages_ticket ON support_messages (ticket_id, created_at)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS support_attachments (
            id UUID PRIMARY KEY,
            ticket_id UUID NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
            uploaded_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            content_type VARCHAR(50) NOT NULL,
            size INTEGER NOT NULL,
            sha256 VARCHAR(64) NOT NULL,
            data BYTEA NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT timezone('utc', now()),
            CONSTRAINT ck_support_attachments_size CHECK (size > 0 AND size <= 2097152),
            CONSTRAINT ck_support_attachments_type CHECK (content_type IN ('image/jpeg', 'image/png'))
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_support_attachments_ticket ON support_attachments (ticket_id, created_at)")
    op.execute(
        """
        UPDATE platform_roles SET permissions = permissions || '["support.manage"]'::jsonb
        WHERE is_template AND name IN ('Владелец платформы', 'Поддержка') AND NOT (permissions @> '["support.manage"]'::jsonb)
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS support_attachments")
    op.execute("DROP TABLE IF EXISTS support_messages")
    op.execute("DROP TABLE IF EXISTS support_tickets")
    op.execute("DROP SEQUENCE IF EXISTS support_ticket_number_seq")
