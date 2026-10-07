"""Store the diagnostic level decided by diagnostic-service.

Revision ID: r8s9t0u1v2w
Revises: q7r8s9t0u1v
Create Date: 2026-10-07

Nullable columns without defaults are metadata-only. Rows saved before this
revision keep NULLs and are read through the theta fallback in
src/auth/diagnostic_level.py.

Rollback: an older auth image cannot start on a database at this head (unknown
revision). Run ``alembic downgrade q7r8s9t0u1v`` before redeploying an older
image; this drops the stored level codes.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "r8s9t0u1v2w"
down_revision: str = "q7r8s9t0u1v"
branch_labels = None
depends_on = None

TABLE = "student_diagnostic_results"


def upgrade() -> None:
    # Fail fast instead of queueing behind a long transaction on a hot table.
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 001_baseline builds tables from the *current* ORM metadata, so a fresh
    # database already has these columns when it reaches this revision.
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}
    columns = (
        sa.Column("overall_level", sa.String(16), nullable=True),
        sa.Column("level_status", sa.String(16), nullable=True),
        sa.Column("class_equivalent", sa.String(64), nullable=True),
        sa.Column("level_provisional", sa.Boolean(), nullable=True),
    )
    for column in columns:
        if column.name not in existing:
            op.add_column(TABLE, column)


def downgrade() -> None:
    # Drops the stored level codes; readers then fall back to theta.
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name in ("level_provisional", "class_equivalent", "level_status", "overall_level"):
        op.drop_column(TABLE, name)
