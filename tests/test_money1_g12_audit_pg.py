"""Money-1 / G-12 (N15): the journal of the marks (``group_attendance_audit``) outlives the row of a lesson that goes back to the schedule - it keeps the key of the lesson (PostgreSQL)."""
from sqlalchemy import text

from src.models.auth import GroupAttendanceAudit
from tests.b2_helpers import build_d2, clock, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.test_w1_cancel_pg import restore, row_of, sheet, w  # noqa: F401  (fixtures)

pytestmark = needs_pg


def journal_of(w):
    w.db.expire_all()
    return sorted(w.db.query(GroupAttendanceAudit).filter_by(group_id=w.G1.id).all(), key=lambda a: (a.created_at, a.action))


def test_g12_the_journal_of_a_cancelled_lesson_stays_when_the_lesson_goes_back_to_the_schedule(w, clock):
    """N15: «Вернуть на отметку» deleted the row of the cancelled lesson and its journal with it (the cancellation, the change of the kind)."""
    sheet(w, "admin", w.G1, "lesson-l1", "2026-10-26", reason="Ошибка администратора")
    row_id = row_of(w, w.G1, "lesson-l1", "2026-10-26").id
    before = [(a.action, a.attendance_id) for a in journal_of(w)]
    assert before and all(attendance_id == row_id for _, attendance_id in before)             # the journal exists and hangs on the row
    clock.set("2026-10-21 14:01")
    assert restore(w, "admin", w.G1, "lesson-l1", "2026-10-26", reason="Отменили по ошибке").status_code == 200
    assert row_of(w, w.G1, "lesson-l1", "2026-10-26") is None
    after = journal_of(w)
    assert [a.action for a in after] == [action for action, _ in before]                          # every record is on its place
    assert all(a.attendance_id is None and (a.group_id, a.lesson_date, a.lesson_id) == (w.G1.id, "2026-10-26", "lesson-l1") for a in after)          # unhooked, but readable: group, day, slot
    assert "cancelled" in {a.action for a in after} and any(a.cancellation_reason == "Ошибка администратора" for a in after)


def test_g12_every_new_record_carries_the_key_of_its_lesson(w):
    sheet(w, "admin", w.G1, "lesson-l1", "2026-10-26", reason="Преподаватель болен")
    assert {(a.lesson_date, a.lesson_id) for a in journal_of(w)} == {("2026-10-26", "lesson-l1")}


def test_g12_the_foreign_key_is_set_null_not_cascade(w):
    rule = w.db.execute(text("SELECT confdeltype FROM pg_constraint WHERE conrelid = 'group_attendance_audit'::regclass AND contype = 'f' AND conkey = "
                             "ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid = 'group_attendance_audit'::regclass AND attname = 'attendance_id')]")).scalar()
    assert rule == "n"                                                                             # 'n' = SET NULL ('c' = CASCADE)
