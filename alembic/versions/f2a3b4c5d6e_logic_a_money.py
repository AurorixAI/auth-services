"""LOGIC-A (pack «деньги»): pauses, opening balances, lesson-type history, payroll divisor history, payroll payouts.

Revision ID: f2a3b4c5d6e
Revises: e1f2a3b4c5d
Create Date: 2026-10-21

Spec: ``artifacts/partner_doc_check/logicA_spec.md`` §4.

New tables (every one guarded by the inspector - the revision is idempotent):
* ``membership_pauses`` - a pause «с … по …» of a membership (a FACT, cancelled by ``voided_*``); live pauses of one membership do not
  overlap (the service under the family lock + an EXCLUDE ``gist`` constraint where ``btree_gist`` can be created);
* ``membership_opening_balances`` - «оплачено до» and / or «долг X на дату T» (one LIVE row per membership);
* ``group_lesson_type_history`` - the type of lessons of a group by day (seed: one row per group, ``valid_from = 2020-01-01``);
* ``payroll_divisors`` - the payroll divisor by month (seed: one row per organisation from ``settings.payroll.divisor``, else 12);
* ``payroll_payouts`` - payouts of salary against a saved sheet (append-only, cancelled by ``voided_*``).
``group_audit.ck_group_audit_action`` accepts ``membership_pause_set`` / ``membership_pause_void`` / ``opening_balance_set`` /
``opening_balance_void`` / ``group_lesson_type_set`` / ``student_left_group``.

Excuses (pause): ``attendance_excuses.status`` accepts ``not_required`` («Не требуется: ученик на паузе», set when a pause is created over the
lesson; the downgrade turns those rows into ``confirmed`` - a confirmed excuse is not counted either) and ``group_attendance_audit.action``
accepts ``excuse_closed_pause`` (the downgrade deletes those audit rows).

Data: (1) F13 - for every membership that is closed (left / archived) and whose individual price WITH A SUM was in force on its last day a
marker is written to ``membership_price_overrides``: ``amount NULL`` from the day after (``reason = 'left_group'``, ``created_by NULL``), so
that a return is priced by the group; memberships that returned since are not touched (money of the past is not changed); (2) the seeds above.
Nothing else is converted: the stage / production money stays as it is (the ledger of PAY-2 is untouched).

Rollback (downgrade): drops the five tables, the ``left_group`` markers and the audit rows of the new actions, and restores the old CHECK of
``group_audit``.  LOST: pauses, opening balances, payouts, the history of divisors and of lesson types (an older image reads the current
values from ``settings.payroll.divisor`` / ``groups.lesson_type``, which the services mirror) - export them first.
"""
from __future__ import annotations

from uuid import uuid4

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f2a3b4c5d6e"
down_revision: str = "e1f2a3b4c5d"
branch_labels = None
depends_on = None

GROUP_AUDIT = "group_audit"
AUDIT_OLD = (
    "action IN ('created', 'updated', 'teacher_changed', 'student_archived', 'student_restored', "
    "'group_price_set', 'membership_price_set', 'group_price_cancelled', 'membership_price_cancelled', "
    "'payment_writeoff', 'payment_writeoff_void', 'payment_refund', 'payment_refund_void')"
)
NEW_ACTIONS = (
    "membership_pause_set", "membership_pause_void", "opening_balance_set", "opening_balance_void", "group_lesson_type_set",
    "student_left_group",
)
AUDIT_NEW = AUDIT_OLD[:-1] + ", " + ", ".join(f"'{a}'" for a in NEW_ACTIONS) + ")"
VOID_ALL_OR_NONE = (
    "(voided_at IS NULL AND voided_by IS NULL AND void_reason IS NULL) OR "
    "(voided_at IS NOT NULL AND voided_by IS NOT NULL AND void_reason IS NOT NULL)"
)
LESSON_TYPES = "'group', 'mini_group', 'pair', 'individual'"
EXCUSE_STATUS_OLD = "status IN ('pending', 'confirmed', 'rejected')"
EXCUSE_STATUS_NEW = "status IN ('pending', 'confirmed', 'rejected', 'not_required')"
ATT_AUDIT_OLD = (
    "action IN ('started', 'reconciliation_started', 'created', 'updated', 'corrected', 'cancelled', 'reconciliation_cancelled', "
    "'excuse_reviewed', 'excuse_reset', 'excuse_removed', 'excuse_file_added', 'excuse_file_removed')"
)
ATT_AUDIT_NEW = ATT_AUDIT_OLD[:-1] + ", 'excuse_closed_pause')"
TABLES = ("payroll_payouts", "payroll_divisors", "group_lesson_type_history", "membership_opening_balances", "membership_pauses")


def _uuid(name: str, *, nullable: bool = False, fk: str | None = None, ondelete: str | None = None, pk: bool = False):
    args = [sa.ForeignKey(fk, ondelete=ondelete)] if fk else []
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, nullable=nullable, primary_key=pk)


def _now():
    return sa.text("timezone('utc', now())")


def _replace_check(expression: str) -> None:
    op.execute(f"ALTER TABLE {GROUP_AUDIT} DROP CONSTRAINT IF EXISTS ck_group_audit_action")
    op.execute(f"ALTER TABLE {GROUP_AUDIT} ADD CONSTRAINT ck_group_audit_action CHECK ({expression})")


def _replace_named_check(table: str, name: str, expression: str) -> None:
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression})")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "membership_pauses" not in tables:
        op.create_table(
            "membership_pauses",
            _uuid("id", pk=True),
            _uuid("org_id", fk="organizations.id", ondelete="CASCADE"),
            _uuid("membership_id", fk="student_groups.id", ondelete="RESTRICT"),
            _uuid("student_id", fk="students.id", ondelete="RESTRICT"),
            _uuid("group_id", fk="groups.id", ondelete="RESTRICT"),
            sa.Column("first_day", sa.Date(), nullable=False),
            sa.Column("last_day", sa.Date(), nullable=False),
            sa.Column("reason", sa.String(300), nullable=True),
            _uuid("created_by", fk="users.id"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_now()),
            sa.Column("voided_at", sa.DateTime(), nullable=True),
            _uuid("voided_by", nullable=True, fk="users.id"),
            sa.Column("void_reason", sa.String(500), nullable=True),
            sa.CheckConstraint("last_day >= first_day AND last_day - first_day <= 365", name="ck_membership_pauses_range"),
            sa.CheckConstraint(VOID_ALL_OR_NONE, name="ck_membership_pauses_void_all_or_none"),
        )
        op.create_index("ix_mp_membership_live", "membership_pauses", ["org_id", "membership_id"],
                        postgresql_where=sa.text("voided_at IS NULL"))
        op.create_index("ix_mp_org_first_live", "membership_pauses", ["org_id", "first_day"],
                        postgresql_where=sa.text("voided_at IS NULL"))
        try:  # the second guard against overlapping live pauses; the service is the first (it answers 409 PAUSE_OVERLAP)
            with bind.begin_nested():
                op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
                op.execute(
                    "ALTER TABLE membership_pauses ADD CONSTRAINT ex_membership_pauses_no_overlap EXCLUDE USING gist "
                    "(membership_id WITH =, daterange(first_day, last_day, '[]') WITH &&) WHERE (voided_at IS NULL)"
                )
        except Exception:  # pragma: no cover - the extension is not available: the service alone keeps the rule
            pass

    if "membership_opening_balances" not in tables:
        op.create_table(
            "membership_opening_balances",
            _uuid("id", pk=True),
            _uuid("org_id", fk="organizations.id", ondelete="CASCADE"),
            _uuid("membership_id", fk="student_groups.id", ondelete="RESTRICT"),
            _uuid("student_id", fk="students.id", ondelete="RESTRICT"),
            _uuid("group_id", fk="groups.id", ondelete="RESTRICT"),
            sa.Column("paid_until", sa.Date(), nullable=True),
            sa.Column("debt_amount", sa.BigInteger(), nullable=True),
            sa.Column("debt_on", sa.Date(), nullable=True),
            sa.Column("note", sa.String(300), nullable=True),
            sa.Column("source", sa.String(10), nullable=False, server_default="manual"),
            _uuid("created_by", nullable=True, fk="users.id"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_now()),
            sa.Column("voided_at", sa.DateTime(), nullable=True),
            _uuid("voided_by", nullable=True, fk="users.id"),
            sa.Column("void_reason", sa.String(500), nullable=True),
            sa.CheckConstraint("debt_amount IS NULL OR debt_amount > 0", name="ck_mob_debt_amount"),
            sa.CheckConstraint("paid_until IS NOT NULL OR debt_amount IS NOT NULL", name="ck_mob_not_empty"),
            sa.CheckConstraint("(debt_amount IS NULL) = (debt_on IS NULL)", name="ck_mob_debt_pair"),
            sa.CheckConstraint("source IN ('import', 'manual', 'add')", name="ck_mob_source"),
            sa.CheckConstraint(VOID_ALL_OR_NONE, name="ck_mob_void_all_or_none"),
        )
        op.create_index("uq_mob_membership_live", "membership_opening_balances", ["membership_id"], unique=True,
                        postgresql_where=sa.text("voided_at IS NULL"))
        op.create_index("ix_mob_org_live", "membership_opening_balances", ["org_id"], postgresql_where=sa.text("voided_at IS NULL"))

    seed_types = "group_lesson_type_history" not in tables
    if seed_types:
        op.create_table(
            "group_lesson_type_history",
            _uuid("id", pk=True),
            _uuid("group_id", fk="groups.id", ondelete="CASCADE"),
            sa.Column("valid_from", sa.Date(), nullable=False),
            sa.Column("lesson_type", sa.String(16), nullable=False),
            _uuid("created_by", nullable=True, fk="users.id"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_now()),
            sa.UniqueConstraint("group_id", "valid_from", name="uq_group_lesson_type_from"),
            sa.CheckConstraint(f"lesson_type IN ({LESSON_TYPES})", name="ck_group_lesson_type_history_type"),
        )
    seed_divisors = "payroll_divisors" not in tables
    if seed_divisors:
        op.create_table(
            "payroll_divisors",
            _uuid("id", pk=True),
            _uuid("org_id", fk="organizations.id", ondelete="CASCADE"),
            sa.Column("valid_from", sa.Date(), nullable=False),
            sa.Column("divisor", sa.SmallInteger(), nullable=False),
            sa.Column("note", sa.String(300), nullable=True),
            _uuid("created_by", nullable=True, fk="users.id"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_now()),
            sa.CheckConstraint("EXTRACT(DAY FROM valid_from) = 1", name="ck_payroll_divisors_first_day"),
            sa.CheckConstraint("divisor BETWEEN 1 AND 31", name="ck_payroll_divisors_range"),
        )
        op.create_index("idx_payroll_divisors_lookup", "payroll_divisors", ["org_id", "valid_from"])
    if "payroll_payouts" not in tables:
        op.create_table(
            "payroll_payouts",
            _uuid("id", pk=True),
            _uuid("org_id", fk="organizations.id", ondelete="CASCADE"),
            _uuid("sheet_id", fk="payroll_sheets.id", ondelete="RESTRICT"),
            _uuid("teacher_id", fk="users.id"),
            sa.Column("period_month", sa.Date(), nullable=False),
            sa.Column("amount", sa.BigInteger(), nullable=False),
            sa.Column("paid_on", sa.Date(), nullable=False),
            sa.Column("method", sa.String(16), nullable=False),
            sa.Column("note", sa.String(500), nullable=True),
            sa.Column("sheet_revision", sa.Integer(), nullable=False),
            sa.Column("sheet_total", sa.BigInteger(), nullable=False),
            _uuid("idempotency_key", nullable=True),
            _uuid("created_by", fk="users.id"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_now()),
            sa.Column("voided_at", sa.DateTime(), nullable=True),
            _uuid("voided_by", nullable=True, fk="users.id"),
            sa.Column("void_reason", sa.String(500), nullable=True),
            sa.CheckConstraint("amount > 0", name="ck_payroll_payouts_amount"),
            sa.CheckConstraint("method IN ('cash', 'card', 'bank_transfer', 'other')", name="ck_payroll_payouts_method"),
            sa.CheckConstraint("EXTRACT(DAY FROM period_month) = 1", name="ck_payroll_payouts_first_day"),
            sa.CheckConstraint(VOID_ALL_OR_NONE, name="ck_payroll_payouts_void_all_or_none"),
        )
        op.create_index("uq_payroll_payouts_idem", "payroll_payouts", ["org_id", "idempotency_key"], unique=True,
                        postgresql_where=sa.text("idempotency_key IS NOT NULL"))
        op.create_index("ix_pp_org_month_live", "payroll_payouts", ["org_id", "period_month"],
                        postgresql_where=sa.text("voided_at IS NULL"))
        op.create_index("ix_pp_sheet_live", "payroll_payouts", ["sheet_id"], postgresql_where=sa.text("voided_at IS NULL"))

    _replace_check(AUDIT_NEW)
    _replace_named_check("attendance_excuses", "ck_attendance_excuse_status", EXCUSE_STATUS_NEW)
    _replace_named_check("group_attendance_audit", "ck_group_attendance_audit_action", ATT_AUDIT_NEW)

    # ---- data: seeds
    if seed_types:
        for gid, lesson_type in bind.execute(sa.text("SELECT id, lesson_type FROM groups")).fetchall():
            bind.execute(
                sa.text("INSERT INTO group_lesson_type_history (id, group_id, valid_from, lesson_type) "
                        "VALUES (:id, :g, DATE '2020-01-01', :t)"),
                {"id": uuid4(), "g": gid, "t": lesson_type or "group"},
            )
    if seed_divisors:
        for org_id, settings in bind.execute(sa.text("SELECT id, settings FROM organizations")).fetchall():
            raw = ((settings or {}).get("payroll") or {}).get("divisor") if isinstance(settings, dict) else None
            divisor = raw if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= 31 else 12
            bind.execute(
                sa.text("INSERT INTO payroll_divisors (id, org_id, valid_from, divisor, note) "
                        "VALUES (:id, :o, DATE '2020-01-01', :d, 'migration: the divisor before the history')"),
                {"id": uuid4(), "o": org_id, "d": divisor},
            )

    # ---- data: F13 - the individual price of a closed membership ends the day after its last day.  ONE query finds them (the last day of
    # the last stay in the business time, the exception with a sum in force on it) and ONE batch inserts the markers.
    found = bind.execute(sa.text(
        """
        SELECT x.id, x.left_day
          FROM (
            SELECT sg.id,
                   ((COALESCE(max(p.left_at), sg.left_at)) AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Tashkent')::date AS left_day
              FROM student_groups sg
              LEFT JOIN student_group_periods p ON p.membership_id = sg.id AND p.left_at IS NOT NULL
             WHERE sg.is_active = false
             GROUP BY sg.id, sg.left_at
          ) x
         WHERE x.left_day IS NOT NULL
           AND (SELECT o.amount FROM membership_price_overrides o
                 WHERE o.membership_id = x.id AND o.valid_from <= x.left_day
                 ORDER BY o.valid_from DESC LIMIT 1) IS NOT NULL
        """
    )).fetchall()
    if found:
        bind.execute(
            sa.text("INSERT INTO membership_price_overrides (id, membership_id, amount, valid_from, reason, created_by, created_at) "
                    "VALUES (:id, :m, NULL, CAST(:d AS date) + 1, 'left_group', NULL, timezone('utc', now())) "
                    "ON CONFLICT ON CONSTRAINT uq_membership_price_overrides_from DO NOTHING"),
            [{"id": uuid4(), "m": mid, "d": left_day} for mid, left_day in found],
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table in TABLES:
        if table in tables:
            op.drop_table(table)
    op.execute("DELETE FROM membership_price_overrides WHERE reason = 'left_group' AND amount IS NULL")
    op.execute(f"DELETE FROM {GROUP_AUDIT} WHERE action IN ({', '.join(repr(a) for a in NEW_ACTIONS)})")
    _replace_check(AUDIT_OLD)
    op.execute("UPDATE attendance_excuses SET status = 'confirmed' WHERE status = 'not_required'")
    op.execute("DELETE FROM group_attendance_audit WHERE action = 'excuse_closed_pause'")
    _replace_named_check("attendance_excuses", "ck_attendance_excuse_status", EXCUSE_STATUS_OLD)
    _replace_named_check("group_attendance_audit", "ck_group_attendance_audit_action", ATT_AUDIT_OLD)
