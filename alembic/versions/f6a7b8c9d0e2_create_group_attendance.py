"""Create group_attendance, which the chain assumed already existed.

Revision ID: f6a7b8c9d0e2
Revises: f6a7b8c9d0e1
Create Date: 2026-09-06

``group_attendance`` was only ever created by ``Base.metadata.create_all()``,
which the service runs *after* ``alembic upgrade head``.  Every database that
had not already been touched by that call — including preview and staging —
therefore reached ``g7h8i9j0k1l2`` with no such table, the upgrade raised, the
startup re-raised, and the worker could never boot far enough to run
``create_all``.  The table is created here so the chain stands on its own.

The shape is deliberately the *pre-``g7h8i9j0k1l2``* one: that revision installs
the status check constraint and default, and later revisions add
``cancellation_reason``, ``started_at`` and ``roster_snapshot`` themselves.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f6a7b8c9d0e2"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A development database created from ORM metadata already has the table,
    # in its final shape.  Leave it alone; the guards in the revisions that
    # follow keep their own additive steps safe.
    if "group_attendance" in sa.inspect(op.get_bind()).get_table_names():
        return

    op.create_table(
        "group_attendance",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("lesson_date", sa.String(length=10), nullable=False),
        sa.Column("lesson_id", sa.String(length=100)),
        sa.Column("topic", sa.String(length=300)),
        sa.Column("status", sa.String(length=20), server_default="completed"),
        sa.Column("records", postgresql.JSONB()),
        sa.Column("marked_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("group_id", "lesson_date", "lesson_id", name="uq_group_lesson_date_id"),
    )
    op.create_index("idx_attendance_group", "group_attendance", ["group_id"])
    op.create_index("idx_attendance_org", "group_attendance", ["org_id"])


def downgrade() -> None:
    op.drop_index("idx_attendance_org", table_name="group_attendance")
    op.drop_index("idx_attendance_group", table_name="group_attendance")
    op.drop_table("group_attendance")
