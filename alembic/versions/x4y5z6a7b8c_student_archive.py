"""«Ученик ушёл из центра»: archive stamp, audit actions, one-off clean-up (N13, ROADMAP 24 / 64).

Revision ID: x4y5z6a7b8c
Revises: w3x4y5z6a7b
Create Date: 2026-10-16

* ``students.archived_at`` (TIMESTAMP, naive UTC) and ``students.archive_reason`` (VARCHAR(300)) - both NULL:
  when and why a student went to the archive.  Nullable and additive: no table rewrite, no default.  They
  are what lets a student who had NO group at the moment of archiving keep his date and reason (there is
  no ``group_audit`` row to hold them), and what tells the return window which audit rows belong to the
  LAST archiving.
* ``group_audit``: the action CHECK (``ck_group_audit_action``) also accepts ``student_archived`` and
  ``student_restored``.
* Data clean-up of students already in the archive (before this revision the status was set by the generic
  ``PUT /org/students/{id}`` and nothing else changed):
    - their ACTIVE memberships are closed (``is_active = false``, ``left_at`` = now, naive UTC; a
      ``left_at`` that is already set is kept) - payments, receipts, lesson marks and payroll sheets stay;
    - their ``group_id`` is emptied (the invariant "the primary group has an active membership").
    - their ``archived_at`` is stamped with their ``updated_at`` (the last change of the row, which was the
      archiving; the reason stays NULL).  ``updated_at`` is written by the database (``now()`` cast to a
      TIMESTAMP in the session TimeZone), ``archived_at`` is naive UTC: the value is converted with
      ``AT TIME ZONE current_setting('TimeZone')`` -> ``AT TIME ZONE 'UTC'``, which changes nothing on a
      UTC server (the default; the application sets no other zone) and stays right on any other.
  ``students.updated_at`` itself is NOT touched.  No ``group_audit`` row is written (it needs an acting
  user).

Idempotent: the columns and the CHECK tolerate a partial earlier run (a database built from the models,
then stamped), and the clean-up and the stamp change nothing the second time.

Rollback (downgrade): restores the previous action CHECK only - NOT VALID, so audit rows that already use
the new actions stay and only new rows are constrained.  The two columns and the closed memberships are
KEPT on purpose: the columns are nullable and harmless to the previous code, and dropping them would lose
the reasons; re-opening memberships is impossible to do safely.  An older auth image cannot start on a
database at this head (unknown revision): downgrade first, roll the image back afterwards.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "x4y5z6a7b8c"
down_revision: str = "w3x4y5z6a7b"
branch_labels = None
depends_on = None

STUDENTS = "students"
GROUP_AUDIT_OLD = "action IN ('created', 'updated', 'teacher_changed')"
GROUP_AUDIT_NEW = (
    "action IN ('created', 'updated', 'teacher_changed', 'student_archived', 'student_restored')"
)


def _replace_check(table: str, name: str, expression: str, *, not_valid: bool = False) -> None:
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    # Older local databases may carry the PostgreSQL default name instead.
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_action_check")
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression})"
        + (" NOT VALID" if not_valid else "")
    )


def upgrade() -> None:
    # Fail fast instead of queueing behind a long transaction on a hot table.
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns(STUDENTS)}

    if "archived_at" not in columns:
        op.add_column(STUDENTS, sa.Column("archived_at", sa.DateTime(), nullable=True))
    if "archive_reason" not in columns:
        op.add_column(STUDENTS, sa.Column("archive_reason", sa.String(length=300), nullable=True))

    _replace_check("group_audit", "ck_group_audit_action", GROUP_AUDIT_NEW)

    # One-off clean-up: an archived student has no active membership and no primary group.
    op.execute(
        "UPDATE student_groups AS sg "
        "SET is_active = false, left_at = COALESCE(sg.left_at, (now() AT TIME ZONE 'UTC')) "
        "FROM students AS s "
        "WHERE sg.student_id = s.id AND s.status = 'archived' AND sg.is_active IS TRUE"
    )
    op.execute("UPDATE students SET group_id = NULL WHERE status = 'archived' AND group_id IS NOT NULL")
    # Legacy archived students get a frozen date (their last row change, as naive UTC); the reason stays NULL.
    op.execute(
        "UPDATE students "
        "SET archived_at = ((updated_at AT TIME ZONE current_setting('TimeZone')) AT TIME ZONE 'UTC') "
        "WHERE status = 'archived' AND archived_at IS NULL AND updated_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _replace_check("group_audit", "ck_group_audit_action", GROUP_AUDIT_OLD, not_valid=True)
