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
    """Create baseline tables if they do not exist."""
    from src.models import Base
    from alembic import op
    bind = op.get_bind()
    tables_to_create = [
        tbl for name, tbl in Base.metadata.tables.items()
        if name not in ("answer_events", "session_summaries", "student_skill_states")
    ]
    Base.metadata.create_all(bind=bind, tables=tables_to_create)


def downgrade() -> None:
    """Cannot downgrade past baseline."""
    pass
