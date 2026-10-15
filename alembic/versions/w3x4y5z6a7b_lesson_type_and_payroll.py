"""Kind of lessons of a group and the payroll sheets (N13 P3).

Revision ID: w3x4y5z6a7b
Revises: u1v2w3x4y5z
Create Date: 2026-10-15

* ``groups.lesson_type`` - VARCHAR(16) NOT NULL DEFAULT 'group', CHECK in ('group', 'mini_group',
  'pair', 'individual').  Every existing group becomes "group".  The payroll percent is set per
  teacher and kind of lessons.
* ``payroll_sheets`` - one sheet per teacher and calendar month (``period_month`` = the 1st), UNIQUE
  ``(org_id, teacher_id, period_month)``, index ``(org_id, period_month)``.  ``lines`` holds the rows of
  the sheet with their marks by date, ``source_fingerprint`` the hash of the data the calculation read
  (a saved sheet never changes by itself; the page offers to recalculate when the hash differs).
* ``payroll_sheet_revisions`` - append-only history of a sheet (UNIQUE ``(sheet_id, revision)``), deleted
  together with its sheet (ON DELETE CASCADE).

Idempotent: the column, the CHECK, the tables and the indexes each tolerate a partial earlier run (a
database that was built from the models, then stamped).

Rollback: the downgrade drops both payroll tables and the column - EXPORT THE SHEETS BEFORE ROLLING BACK
(выгрузить ведомости перед откатом: the Excel export of every month, or
``SELECT * FROM payroll_sheets / payroll_sheet_revisions``): the
history of revisions is lost.  The kind of lessons of every group is lost too (the audit log keeps the
changes as they were written).  An older auth image cannot start on a database at this head (unknown
revision): downgrade first, roll the image back afterwards.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "w3x4y5z6a7b"
down_revision: str = "u1v2w3x4y5z"
branch_labels = None
depends_on = None

GROUPS = "groups"
SHEETS = "payroll_sheets"
REVISIONS = "payroll_sheet_revisions"
TYPE_CHECK = "ck_groups_lesson_type"
SHEET_INDEX = "idx_payroll_sheets_org_month"


def _check_names(inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_check_constraints(table)}


def upgrade() -> None:
    # Fail fast instead of queueing behind a long transaction on a hot table.
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. The kind of lessons.
    if "lesson_type" not in {c["name"] for c in inspector.get_columns(GROUPS)}:
        op.add_column(
            GROUPS,
            sa.Column("lesson_type", sa.String(length=16), nullable=False, server_default="group"),
        )
    if TYPE_CHECK not in _check_names(inspector, GROUPS):
        op.create_check_constraint(
            TYPE_CHECK, GROUPS, "lesson_type IN ('group', 'mini_group', 'pair', 'individual')"
        )

    # 2. Sheets.
    tables = set(inspector.get_table_names())
    if SHEETS not in tables:
        op.create_table(
            SHEETS,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "org_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "teacher_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
            ),
            sa.Column("period_month", sa.Date(), nullable=False),
            sa.Column(
                "percent_inputs",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column("divisor_default", sa.SmallInteger(), nullable=False, server_default="12"),
            sa.Column(
                "lines", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
            ),
            sa.Column("total_amount", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("source_fingerprint", sa.Text(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column(
                "created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column(
                "updated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
            ),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint(
                "org_id", "teacher_id", "period_month", name="uq_payroll_sheet_teacher_month"
            ),
            sa.CheckConstraint("EXTRACT(DAY FROM period_month) = 1", name="ck_payroll_sheets_first_day"),
            sa.CheckConstraint("total_amount >= 0", name="ck_payroll_sheets_total"),
            sa.CheckConstraint("revision >= 1", name="ck_payroll_sheets_revision"),
            sa.CheckConstraint("divisor_default BETWEEN 1 AND 31", name="ck_payroll_sheets_divisor"),
        )
    if SHEET_INDEX not in {ix["name"] for ix in sa.inspect(bind).get_indexes(SHEETS)}:
        op.create_index(SHEET_INDEX, SHEETS, ["org_id", "period_month"])

    # 3. History of revisions (append-only).
    if REVISIONS not in tables:
        op.create_table(
            REVISIONS,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "sheet_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(f"{SHEETS}.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column(
                "percent_inputs",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "lines", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
            ),
            sa.Column("total_amount", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column(
                "changed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
            ),
            sa.Column("changed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("change_note", sa.String(length=300), nullable=True),
            sa.UniqueConstraint("sheet_id", "revision", name="uq_payroll_sheet_revision"),
            sa.CheckConstraint("revision >= 1", name="ck_payroll_sheet_revisions_revision"),
        )


def downgrade() -> None:
    """Drops the payroll tables and ``groups.lesson_type``.  Export the sheets first (see the docstring)."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if REVISIONS in tables:
        op.drop_table(REVISIONS)
    if SHEETS in tables:
        op.drop_table(SHEETS)
    if GROUPS in tables:
        if TYPE_CHECK in _check_names(inspector, GROUPS):
            op.drop_constraint(TYPE_CHECK, GROUPS, type_="check")
        if "lesson_type" in {c["name"] for c in inspector.get_columns(GROUPS)}:
            op.drop_column(GROUPS, "lesson_type")
