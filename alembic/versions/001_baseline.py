"""Baseline: stamp current schema as starting point.

All tables already exist in the database (created via SQL scripts 001-013).
This migration exists so Alembic has a known starting revision.
Future changes to models → `alembic revision --autogenerate` → applied on startup.

Revision ID: 001_baseline
Revises: (none)
Create Date: 2026-04-15
"""
from typing import Sequence, Union

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401

revision: str = "001_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create only baseline tables, not tables owned by later revisions.

    This fixed allowlist is intentional: importing today's ORM metadata must
    not create future tables before their CREATE TABLE migrations run.
    """
    from src.models import Base
    from alembic import op
    bind = op.get_bind()
    baseline_tables = (
        "organizations", "users", "organization_members", "invitations",
        "students", "groups", "student_groups", "diagnostic_invites",
        "student_diagnostic_results",
    )
    tables_to_create = [Base.metadata.tables[name] for name in baseline_tables]
    Base.metadata.create_all(bind=bind, tables=tables_to_create)


def downgrade() -> None:
    """Cannot downgrade past baseline."""
    pass
