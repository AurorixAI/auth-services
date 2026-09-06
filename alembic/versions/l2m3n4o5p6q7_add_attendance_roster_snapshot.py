"""Freeze the actual lesson roster for durable attendance history.

Revision ID: l2m3n4o5p6q7
Revises: k1l2m3n4o5p6
Create Date: 2026-08-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "l2m3n4o5p6q7"
down_revision: Union[str, Sequence[str], None] = "k1l2m3n4o5p6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    """Skip a column an ORM-created development database already has.

    ``group_attendance`` is also created by ``Base.metadata.create_all()`` in
    its final shape, so on such a database this additive step is already done.
    """
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}
    if column.name not in existing:
        op.add_column(table, column)


def upgrade() -> None:
    # Deliberately nullable: historical rows remain untouched and are read via
    # their immutable records until a new lesson is started under this model.
    _add_column_if_missing(
        "group_attendance",
        sa.Column("roster_snapshot", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("group_attendance", "roster_snapshot")
