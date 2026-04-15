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
    """Nothing to do — tables already exist."""
    pass


def downgrade() -> None:
    """Cannot downgrade from baseline."""
    raise RuntimeError("Cannot downgrade past the baseline migration.")
