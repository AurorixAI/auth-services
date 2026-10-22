"""LOGIC-B (1/2): optimistic lock of the marks, lesson teachers, "valid from" of the schedule slots, no invented room.

Revision ID: lb1a2b3c4d5
Revises: f2a3b4c5d6e
Create Date: 2026-10-21

Spec: ``artifacts/partner_doc_check/logicB_spec.md`` 3.1.

* ``group_attendance.version`` INTEGER NOT NULL DEFAULT 1 (O7: grows by 1 on every write of the row);
* ``group_attendance_audit`` accepts the action ``teacher_changed`` (the CHECK is rebuilt from the live definition, so a
  parallel change of the list by another revision is kept);
* ``lesson_teacher_assignments`` (O5: the teacher of ONE lesson occurrence when it is a substitution) + a partial unique index
  (one active row per group / lesson / date) + ``(org_id, teacher_id, lesson_date)``;
* DATA ``groups.schedule``: every slot without ``validFrom`` gets it (O1) = the business date (Asia/Tashkent) the GROUP was created
  (``groups.created_at``); if ``created_at`` is missing, the earliest of the first attendance row of the group and the first membership
  ``joined_at``.  The backfill must NOT change history: the lessons the old code generated for a legacy slot (from the day the group
  existed) are generated unchanged, so the ledger (proration of a partial first period) and «Незакрытые» are identical before and after;
  ``validFrom`` only bounds slots that are created later.  (Rehearsal on a stage copy, 22.10: the first version took the first attendance
  row / the migration day and moved 27 memberships from lessons to days and dropped the past lessons of a slot without rows.)  A slot that
  already has ``validFrom`` is kept;
* DATA (O11): the placeholder room ``Кабинет 204`` (exactly) becomes ``null`` in the schedule slots, in
  ``group_schedule_one_off_lessons.payload.room`` and in ``group_schedule_overrides.payload.room``.  The audit payloads stay
  (immutable).  Declared risk: a centre that really names a room "Кабинет 204" has to enter it again;
* TIME_CONVENTIONS (O13): ``COMMENT ON COLUMN`` for the two time groups of ``group_attendance``.

Idempotent: every step is guarded; a second run changes nothing.

Rollback (downgrade): drops ``lesson_teacher_assignments`` and ``group_attendance.version``, removes the audit rows with the action
``teacher_changed`` and restores the CHECK.  The data steps (``validFrom``, the cleared rooms) are NOT reverted: a slot with an
extra ``validFrom`` key is read by the old code as before (it ignores unknown keys), a cleared room shows the old placeholder.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "lb1a2b3c4d5"
down_revision: str = "f2a3b4c5d6e"
branch_labels = None
depends_on = None

ATTENDANCE = "group_attendance"
ATTENDANCE_AUDIT = "group_attendance_audit"
ASSIGNMENTS = "lesson_teacher_assignments"
AUDIT_CHECK = "ck_group_attendance_audit_action"
NEW_ACTIONS = ("teacher_changed",)
PLACEHOLDER_ROOM = "Кабинет 204"

COMMENTS = {
    "started_at": "business time Asia/Tashkent, naive",
    "completed_at": "business time Asia/Tashkent, naive",
    "created_at": "UTC naive",
    "updated_at": "UTC naive",
}


def _check_actions(bind, table: str, name: str) -> list[str]:
    row = bind.execute(
        sa.text(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname = :t AND c.conname = :n"
        ),
        {"t": table, "n": name},
    ).scalar()
    return re.findall(r"'([a-z_]+)'", row or "")


def _set_check_actions(table: str, name: str, actions: list[str]) -> None:
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_action_check")
    listed = ", ".join(f"'{a}'" for a in actions)
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK (action IN ({listed}))")


def _slot_lesson_id(group_id, slot: dict) -> str:
    """The id the service gives a slot (``AuthService._lesson_id_for_slot``)."""
    return str(
        slot.get("id")
        or f"lesson-{group_id}-{slot.get('day_of_week') or slot.get('dayOfWeek', 1)}-"
        f"{slot.get('start_time') or slot.get('startTime', '10:00')}"
    )


def _tashkent_date(moment) -> "str | None":
    """The business date (Asia/Tashkent) of a naive UTC instant."""
    if moment is None:
        return None
    return moment.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Tashkent")).date().isoformat()


def _backfill_schedule(bind) -> None:
    """``validFrom`` of a legacy slot = the business date the group was created (see the module docstring); idempotent (a slot that has it is kept)."""
    rows = bind.execute(
        sa.text(
            "SELECT id, schedule, created_at FROM groups "
            "WHERE schedule IS NOT NULL AND jsonb_typeof(schedule) = 'array' AND jsonb_array_length(schedule) > 0"
        )
    ).fetchall()
    for group_id, schedule, created_at in rows:
        changed = False
        fallback = None
        for slot in schedule:
            if not isinstance(slot, dict):
                continue
            if not slot.get("validFrom"):
                if fallback is None:
                    fallback = _tashkent_date(created_at)
                    if fallback is None:          # no creation date: the earliest evidence that the group existed
                        first_row = bind.execute(sa.text("SELECT min(lesson_date) FROM group_attendance WHERE group_id = :g"), {"g": group_id}).scalar()
                        joined = _tashkent_date(bind.execute(sa.text("SELECT min(joined_at) FROM student_groups WHERE group_id = :g"), {"g": group_id}).scalar())
                        fallback = min([d for d in (first_row, joined) if d] or ["0001-01-01"])
                slot["validFrom"] = fallback
                changed = True
            if slot.get("room") == PLACEHOLDER_ROOM:
                slot["room"] = None
                changed = True
        if changed:
            bind.execute(
                sa.text("UPDATE groups SET schedule = CAST(:s AS jsonb) WHERE id = :g"),
                {"s": json.dumps(schedule, ensure_ascii=False), "g": group_id},
            )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. the version of the marks
    if "version" not in {c["name"] for c in inspector.get_columns(ATTENDANCE)}:
        op.add_column(ATTENDANCE, sa.Column("version", sa.Integer(), nullable=False, server_default="1"))

    # 2. the audit action
    actions = _check_actions(bind, ATTENDANCE_AUDIT, AUDIT_CHECK)
    if actions and not set(NEW_ACTIONS) <= set(actions):
        _set_check_actions(ATTENDANCE_AUDIT, AUDIT_CHECK, actions + [a for a in NEW_ACTIONS if a not in actions])

    # 3. lesson teachers
    if ASSIGNMENTS not in inspector.get_table_names():
        op.create_table(
            ASSIGNMENTS,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
            sa.Column("lesson_id", sa.String(100), nullable=False),
            sa.Column("lesson_date", sa.String(10), nullable=False),
            sa.Column("teacher_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("reason", sa.String(300), nullable=False),
            sa.Column("set_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("timezone('utc', now())")),
            sa.Column("cancelled_at", sa.DateTime(), nullable=True),
            sa.Column("cancelled_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        )
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes(ASSIGNMENTS)}
    if "uq_lesson_teacher_assignment_active" not in indexes:
        op.create_index(
            "uq_lesson_teacher_assignment_active", ASSIGNMENTS, ["group_id", "lesson_id", "lesson_date"],
            unique=True, postgresql_where=sa.text("cancelled_at IS NULL"),
        )
    if "idx_lesson_teacher_assignment_teacher" not in indexes:
        op.create_index("idx_lesson_teacher_assignment_teacher", ASSIGNMENTS, ["org_id", "teacher_id", "lesson_date"])

    # 4. data: validFrom of the slots, the invented room
    _backfill_schedule(bind)
    for table in ("group_schedule_one_off_lessons", "group_schedule_overrides"):
        op.execute(
            f"UPDATE {table} SET payload = jsonb_set(payload, '{{room}}', 'null'::jsonb) "
            f"WHERE payload ->> 'room' = '{PLACEHOLDER_ROOM}'"
        )

    # 5. the two time conventions of group_attendance
    for column, text in COMMENTS.items():
        op.execute(f"COMMENT ON COLUMN {ATTENDANCE}.{column} IS '{text}'")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if ASSIGNMENTS in inspector.get_table_names():
        op.drop_table(ASSIGNMENTS)
    if "version" in {c["name"] for c in inspector.get_columns(ATTENDANCE)}:
        op.drop_column(ATTENDANCE, "version")
    actions = _check_actions(bind, ATTENDANCE_AUDIT, AUDIT_CHECK)
    if set(NEW_ACTIONS) & set(actions):
        op.execute(f"DELETE FROM {ATTENDANCE_AUDIT} WHERE action IN ({', '.join(repr(a) for a in NEW_ACTIONS)})")
        _set_check_actions(ATTENDANCE_AUDIT, AUDIT_CHECK, [a for a in actions if a not in NEW_ACTIONS])
    for column in COMMENTS:
        op.execute(f"COMMENT ON COLUMN {ATTENDANCE}.{column} IS NULL")
