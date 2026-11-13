"""Money-1 (pack G, G-12): the journal of the marks outlives the row of a lesson.

Revision ID: mg12a1b2c3d4
Revises: mv5a1b2c3d4
Create Date: 2026-11-13

* ``group_attendance_audit.lesson_date`` / ``lesson_id`` (nullable): the key of the lesson (group, day, slot) - a record stays readable when its row is gone.
* ``group_attendance_audit.attendance_id`` is nullable and its foreign key is ``ON DELETE SET NULL`` (it was ``CASCADE``: «Вернуть на отметку» / «Отменить выходной» deleted the row of
  a cancelled lesson and its journal with it).

Only ADDS and WEAKENS: no row is read, moved or changed (the old records keep their ``attendance_id``; their key columns stay empty - the row they hang on is still there).
Rollback (downgrade): the foreign key becomes ``CASCADE`` again; NOTHING is deleted - records already unhooked (``attendance_id`` NULL) stay in the table with their key, and the
columns / the nullability stay (an old reader ignores them), so a downgrade never costs a record of the journal.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "mg12a1b2c3d4"
down_revision: str = "mv5a1b2c3d4"
branch_labels = None
depends_on = None

TABLE = "group_attendance_audit"


def _attendance_fk_name() -> str | None:
    for fk in sa.inspect(op.get_bind()).get_foreign_keys(TABLE):
        if fk["constrained_columns"] == ["attendance_id"]:
            return fk["name"]
    return None


def _set_foreign_key(on_delete: str) -> None:
    name = _attendance_fk_name()
    if name:
        op.drop_constraint(name, TABLE, type_="foreignkey")
    op.create_foreign_key(name or f"{TABLE}_attendance_id_fkey", TABLE, "group_attendance", ["attendance_id"], ["id"], ondelete=on_delete)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    names = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}
    if "lesson_date" not in names:
        op.add_column(TABLE, sa.Column("lesson_date", sa.String(10), nullable=True))
    if "lesson_id" not in names:
        op.add_column(TABLE, sa.Column("lesson_id", sa.String(100), nullable=True))
    op.alter_column(TABLE, "attendance_id", existing_type=sa.dialects.postgresql.UUID(as_uuid=True), nullable=True)
    _set_foreign_key("SET NULL")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _set_foreign_key("CASCADE")          # attendance_id stays nullable and the key columns stay: a record that was unhooked is not deleted by a rollback
