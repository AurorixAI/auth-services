"""K3 (A1): the fields of the three steps of the demo request, the idempotency key and the outcome of the letters.

Revision ID: k3a1b2c3d4e5
Revises: p159a1b2c3d4
Create Date: 2026-10-29

Only adds (columns nullable, nothing existing changes): ``org_type``, ``address``, ``branches_count``, ``position``, ``preferred_contact_time``, ``needs``,
``current_tool``, ``heard_from``, ``locale``, ``consent_at``, ``request_id`` (unique where set), ``team_mail_status``, ``applicant_mail_status``;
a CHECK on ``org_type`` (the six types of ``organizations``).

Idempotent: ``IF NOT EXISTS`` everywhere, because a database built from the models and stamped back must upgrade (the console pack does the same).
Rollback (downgrade): drops the columns - the data of the steps is LOST, the requests themselves stay; the code of the previous revision does not read them.
"""
from __future__ import annotations

from alembic import op

revision: str = "k3a1b2c3d4e5"
down_revision: str = "p159a1b2c3d4"
branch_labels = None
depends_on = None

TABLE = "demo_requests"
COLUMNS = (
    ("org_type", "VARCHAR(20)"),
    ("address", "TEXT"),
    ("branches_count", "INTEGER"),
    ("position", "VARCHAR(20)"),
    ("preferred_contact_time", "VARCHAR(100)"),
    ("needs", "JSONB"),
    ("current_tool", "VARCHAR(20)"),
    ("heard_from", "VARCHAR(200)"),
    ("locale", "VARCHAR(5)"),
    ("consent_at", "TIMESTAMP"),
    ("request_id", "UUID"),
    ("team_mail_status", "VARCHAR(10)"),
    ("applicant_mail_status", "VARCHAR(10)"),
)
ORG_TYPES = "'center', 'private_school', 'university', 'tutoring_center', 'tutor', 'other'"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name, sql_type in COLUMNS:
        op.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {name} {sql_type}")
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS ck_demo_requests_org_type")
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT ck_demo_requests_org_type CHECK (org_type IS NULL OR org_type IN ({ORG_TYPES})) NOT VALID")
    op.execute(f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT ck_demo_requests_org_type")
    op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS ux_demo_requests_request_id ON {TABLE} (request_id) WHERE request_id IS NOT NULL")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS ux_demo_requests_request_id")
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS ck_demo_requests_org_type")
    for name, _ in reversed(COLUMNS):
        op.execute(f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS {name}")
