"""ROADMAP #39: a decision on an excuse AFTER its confirmation deadline is allowed but MARKED (PostgreSQL).

``attendance_excuses.decided_after_deadline`` / ``deadline_at_decision`` are worked out at the moment of the decision with the
very function the pending list uses, cleared by every way back to ``pending`` and exposed in the list / review / reset
responses and (administrators only) in the journal.
"""
from datetime import datetime, time, timedelta

import src.auth.attendance_excuses as excuses_module
from src.auth.attendance_excuses import AttendanceExcuseService
from src.auth.excuse_deadline import month_end
from src.models.auth import StudentPayment

from tests.pg_world import (  # noqa: F401  (fixtures)
    business_today, excused_lesson, make_client, needs_pg, pg_session, save, world,
)
from tests.test_attendance_excuses import audit_rows, excuse_of, journal, key_of
from tests.test_excuse_deadline import API, lesson_marks, listing
from tests.test_payments_service_pg import day, pay, set_settings

pytestmark = needs_pg

BASE = f"{API}/org/attendance/excuses"


def stored(w, student=None):
    """``(decided_after_deadline, deadline_at_decision)`` as the table holds them."""
    row = excuse_of(w, student)
    return row.decided_after_deadline, row.deadline_at_decision


def shown(item):
    return item["decided_after_deadline"], item["deadline_at_decision"]


def review(w, item_id, decision="confirmed", note=None):
    r = make_client(w).post(f"{BASE}/{item_id}/review", json={"decision": decision, "note": note})
    assert r.status_code == 200, r.text
    return r.json()


def reset(w, item_id):
    r = make_client(w).post(f"{BASE}/{item_id}/reset")
    assert r.status_code == 200, r.text
    return r.json()


def item_of(w, student=None):
    sid = str((student or w.students[0]).id)
    (found,) = [i for i in listing(w) if i["student"]["id"] == sid]
    return found


def set_valid_to(w, payment, valid_to):
    w.db.query(StudentPayment).filter_by(id=payment["id"]).update({"valid_to": valid_to})
    w.db.commit()


# ---- on time / after the deadline ------------------------------------------------------


def test_a_decision_on_time_is_marked_false_with_its_deadline_day(world):
    excused_lesson(world, offset_days=3)
    pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(20))              # the deadline is day(15)
    pending = item_of(world)
    assert pending["status"] == "pending" and pending["deadline"]["date"] == day(15).isoformat()
    assert shown(pending) == (None, None)                                             # nothing decided, nothing marked
    body = review(world, pending["id"])
    assert shown(body) == (False, day(15).isoformat())                                # the review response
    assert stored(world) == (False, day(15))                                          # the table
    assert shown(item_of(world)) == (False, day(15).isoformat())                      # the list afterwards
    assert body["deadline"] is None                                                   # the hint is for pending items only


def test_a_decision_after_the_deadline_is_allowed_and_marked_true(world):
    excused_lesson(world, offset_days=3)
    pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))              # the deadline passed yesterday
    pending = item_of(world)
    assert pending["deadline"]["overdue"] is True
    body = review(world, pending["id"], "rejected", note="Справка не принята")
    assert body["status"] == "rejected" and body["review_note"] == "Справка не принята"   # the decision itself is unchanged
    assert shown(body) == (True, day(-1).isoformat())
    assert stored(world) == (True, day(-1))
    assert shown(item_of(world)) == (True, day(-1).isoformat())


def test_the_deadline_day_itself_is_still_on_time(world):
    excused_lesson(world, offset_days=3)
    pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(0))               # the deadline is today: strictly AFTER is late
    assert shown(review(world, item_of(world)["id"])) == (False, day(0).isoformat())


def test_no_covering_payment_marks_against_the_month_end_like_the_list(world):
    lesson_day, _ = excused_lesson(world, offset_days=3)
    shown_deadline = item_of(world)["deadline"]
    assert shown_deadline["date"] == month_end(lesson_day).isoformat()
    body = review(world, item_of(world)["id"])
    assert shown(body) == (business_today() > month_end(lesson_day), month_end(lesson_day).isoformat())
    assert body["decided_after_deadline"] is shown_deadline["overdue"]                 # the mark never disagrees with the hint


# ---- the lessons package has no day ---------------------------------------------------


def test_a_lessons_package_deadline_stores_the_flag_without_a_day(world):
    s0 = world.students[0]
    set_settings(world, period_mode="lessons", lessons_per_payment=3, excuse_deadline_lessons=1)
    pay(world, s0, paid_on=day(-10), valid_from=day(-10))
    lesson_marks(world, 3, {s0: "excused"}, reasons={s0: "Болел"})
    lesson_marks(world, 2)                                                            # 1 left = N: the last lesson to confirm
    pending = item_of(world)
    assert pending["deadline"] == {"kind": "lessons", "lessons_left": 1, "urgent": True, "due_now": True, "overdue": False}
    assert shown(review(world, pending["id"])) == (False, None)
    lesson_marks(world, 1)                                                            # 0 left < N: overdue
    assert item_of(world)["status"] == "confirmed"
    reset(world, item_of(world)["id"])
    assert item_of(world)["deadline"]["overdue"] is True
    assert shown(review(world, item_of(world)["id"], "rejected")) == (True, None)
    assert stored(world) == (True, None)


# ---- a changed decision / reset -------------------------------------------------------


def test_a_changed_decision_is_marked_again(world):
    excused_lesson(world, offset_days=3)
    payment = pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(20))["payment"]
    item_id = item_of(world)["id"]
    assert shown(review(world, item_id, "confirmed")) == (False, day(15).isoformat())
    set_valid_to(world, payment, day(-1))                                             # the period ended before the next decision
    body = review(world, item_id, "rejected")                                         # a re-review of a decided excuse
    assert body["status"] == "rejected" and shown(body) == (True, day(-1).isoformat())
    assert stored(world) == (True, day(-1))                                           # overwritten, not kept from the first decision
    set_valid_to(world, payment, day(20))                                             # a late change flips it back
    assert shown(review(world, item_id, "confirmed")) == (False, day(15).isoformat())
    assert stored(world) == (False, day(15))


def test_reset_clears_the_mark_and_the_next_decision_sets_it_again(world):
    excused_lesson(world, offset_days=3)
    pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))
    item_id = item_of(world)["id"]
    assert shown(review(world, item_id)) == (True, day(-1).isoformat())
    body = reset(world, item_id)
    assert body["status"] == "pending" and shown(body) == (None, None)
    assert stored(world) == (None, None)
    assert shown(item_of(world)) == (None, None) and item_of(world)["deadline"]["overdue"] is True
    assert shown(review(world, item_id)) == (True, day(-1).isoformat())
    reset(world, item_id)
    reset(world, item_id)                                                             # resetting a pending excuse changes nothing
    assert stored(world) == (None, None)


def test_a_reason_edited_after_the_decision_returns_the_excuse_to_pending_without_a_mark(world):
    lesson_day, lesson_id = excused_lesson(world, offset_days=3)
    pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))
    s0 = world.students[0]
    review(world, item_of(world)["id"])
    assert stored(world) == (True, day(-1))
    save(world, lesson_day, lesson_id, [(s0, "excused")], excuseReasons={s0.id: "Другая причина"})
    changed = excuse_of(world)
    assert changed.status == "pending" and (changed.decided_after_deadline, changed.deadline_at_decision) == (None, None)


def test_bulk_review_marks_every_excuse_by_its_own_deadline(world):
    s0, s1, s2 = world.students
    pay(world, s0, paid_on=day(-30), valid_from=day(-30), valid_to=day(20))          # on time
    pay(world, s1, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))          # late
    lesson_marks(world, 3, {s: "excused" for s in (s0, s1, s2)}, reasons={s: "Болел" for s in (s0, s1, s2)})
    ids = [item_of(world, s)["id"] for s in (s0, s1, s2)]
    r = make_client(world).post(f"{BASE}/review-bulk", json={"ids": ids, "decision": "confirmed"})
    assert r.status_code == 200 and r.json()["updated"] == 3, r.text
    lesson_month_end = month_end(day(-3))                                             # s2 has no payment: the month end
    assert stored(world, s0) == (False, day(15))
    assert stored(world, s1) == (True, day(-1))
    assert stored(world, s2) == (business_today() > lesson_month_end, lesson_month_end)
    assert shown(item_of(world, s0)) == (False, day(15).isoformat())
    assert shown(item_of(world, s1)) == (True, day(-1).isoformat())


# ---- the journal (administrators only) ------------------------------------------------


def test_the_journal_shows_the_mark_to_administrators_only(world):
    lesson_day, lesson_id = excused_lesson(world, offset_days=3)
    pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))
    key, sid = key_of(lesson_day, lesson_id), str(world.students[0].id)
    assert journal(world, world.admin).excuses[key][sid]["decided_after_deadline"] is None     # pending
    review(world, item_of(world)["id"])
    admin_view = journal(world, world.admin).excuses[key][sid]
    assert (admin_view["decided_after_deadline"], admin_view["deadline_at_decision"]) == (True, day(-1).isoformat())
    teacher_view = journal(world, world.teacher, is_admin=False).excuses[key][sid]
    assert "decided_after_deadline" not in teacher_view and "deadline_at_decision" not in teacher_view
    assert "review_note" not in teacher_view


def test_a_decision_made_before_the_mark_existed_stays_unknown_not_on_time(world):
    excused_lesson(world, offset_days=3)
    pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))
    row = excuse_of(world)
    row.status, row.reviewed_by = "confirmed", world.owner.id                         # a historical decision: no mark at all
    world.db.commit()
    assert shown(item_of(world)) == (None, None)
    assert stored(world) == (None, None)


# ---- a failing deadline never blocks a decision ---------------------------------------


def test_a_deadline_that_cannot_be_worked_out_leaves_the_mark_unknown_and_the_decision_stands(world, monkeypatch):
    excused_lesson(world, offset_days=3)
    item_id = item_of(world)["id"]

    def broken(self, pending, context, *args):
        from sqlalchemy import text

        self.db.execute(text("SELECT 1 / 0"))        # a real SQL error: the transaction is aborted unless the savepoint holds

    monkeypatch.setattr(AttendanceExcuseService, "_pending_deadlines", broken)
    body = review(world, item_id, "confirmed", note="Принято")
    monkeypatch.undo()
    assert body["status"] == "confirmed" and shown(body) == (None, None)
    row = excuse_of(world)
    assert (row.status, row.review_note, row.reviewed_by) == ("confirmed", "Принято", world.admin.id)
    assert (row.decided_after_deadline, row.deadline_at_decision) == (None, None)
    # the session is healthy and the next decision is marked again (no payment: the month end of the lesson)
    lesson_month_end = month_end(day(-3))
    assert shown(review(world, item_id, "rejected")) == (business_today() > lesson_month_end, lesson_month_end.isoformat())


def test_a_decision_keeps_its_row_lock_while_the_deadline_is_worked_out(world, monkeypatch):
    """The lock taken by ``_lock_excuse`` is still held when the mark is computed (a second session cannot decide)."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from src.core.db_timezone import utc_connect_args
    from tests.pg_world import DB_URL

    excused_lesson(world, offset_days=3)
    pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(20))
    excuse_id = excuse_of(world).id
    seen = {}
    original = AttendanceExcuseService._pending_deadlines

    def spying(self, pending, context, *args):
        engine = create_engine(DB_URL, connect_args=utc_connect_args(DB_URL))
        try:
            with sessionmaker(engine)() as other:
                other.execute(text("SET LOCAL lock_timeout = '300ms'"))
                try:
                    other.execute(text("SELECT id FROM attendance_excuses WHERE id = :i FOR UPDATE"), {"i": excuse_id})
                    seen["locked"] = False
                except Exception:
                    seen["locked"] = True
        finally:
            engine.dispose()
        return original(self, pending, context, *args)

    item_id = item_of(world)["id"]                                                    # (the list calls it too: look only at the decision)
    monkeypatch.setattr(AttendanceExcuseService, "_pending_deadlines", spying)
    review(world, item_id)
    assert seen == {"locked": True}


# ---- the same decision again only edits the note (the mark says WHEN THE DECISION WAS TAKEN) ----------


def test_confirming_again_with_a_note_does_not_re_measure_the_deadline(world):
    excused_lesson(world, offset_days=3)
    payment = pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(20))["payment"]   # the deadline is day(15)
    item_id = item_of(world)["id"]
    first = review(world, item_id, "confirmed")
    assert shown(first) == (False, day(15).isoformat())
    set_valid_to(world, payment, day(-1))                                             # the deadline passes afterwards ...
    r = make_client(world, user=world.owner).post(f"{BASE}/{item_id}/review", json={"decision": "confirmed", "note": "  Справка принесена  "})
    assert r.status_code == 200, r.text
    again = r.json()
    assert again["status"] == "confirmed" and again["review_note"] == "Справка принесена"
    assert shown(again) == (False, day(15).isoformat())                               # ... the decision was still on time
    assert stored(world) == (False, day(15))
    # who and when DECIDED stays; the editor of the note is in the journal row
    assert (again["reviewed_by"], again["reviewed_at"]) == (first["reviewed_by"], first["reviewed_at"])
    assert again["reviewed_by"]["id"] == str(world.admin.id)
    rows = audit_rows(world, "excuse_reviewed")
    assert [r.actor_id for r in rows] == [world.admin.id, world.owner.id]
    assert rows[1].details == {"excuse_id": item_id, "student_id": str(world.students[0].id), "decision_before": "confirmed",
                               "decision_after": "confirmed", "note": "Справка принесена"}      # no mark was overwritten
    # a CHANGED decision is a new decision: measured now
    assert shown(review(world, item_id, "rejected")) == (True, day(-1).isoformat())


def test_the_same_decision_again_keeps_an_unknown_mark_unknown(world):
    excused_lesson(world, offset_days=3)
    pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))
    row = excuse_of(world)
    row.status, row.reviewed_by = "confirmed", world.owner.id                         # a historical decision: no mark
    world.db.commit()
    body = review(world, str(row.id), "confirmed", note="Уточнение")
    assert body["review_note"] == "Уточнение" and shown(body) == (None, None)
    assert stored(world) == (None, None)


# ---- the journal row keeps the mark that is overwritten or cleared ------------------------------------


def test_the_audit_keeps_the_mark_that_a_decision_or_a_reset_overwrites(world):
    excused_lesson(world, offset_days=3)
    payment = pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))["payment"]    # the deadline is day(-1)
    item_id = item_of(world)["id"]
    review(world, item_id, "confirmed")
    set_valid_to(world, payment, day(20))                                             # the next decision is on time
    review(world, item_id, "rejected")
    reset(world, item_id)
    first, second = audit_rows(world, "excuse_reviewed")
    assert "decided_after_deadline_before" not in first.details and "deadline_at_decision_before" not in first.details   # pending: no mark
    assert (second.details["decided_after_deadline_before"], second.details["deadline_at_decision_before"]) == (True, day(-1).isoformat())
    (cleared,) = audit_rows(world, "excuse_reset")
    assert (cleared.details["decided_after_deadline_before"], cleared.details["deadline_at_decision_before"]) == (False, day(15).isoformat())
    assert cleared.details["decision_before"] == "rejected" and cleared.details["decision_after"] == "pending"
    # nothing to lose, nothing recorded: a reset of a pending excuse writes no row at all
    reset(world, item_id)
    assert len(audit_rows(world, "excuse_reset")) == 1


def test_the_audit_of_a_reason_edit_keeps_the_cleared_mark(world):
    lesson_day, lesson_id = excused_lesson(world, offset_days=3)
    pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))
    s0 = world.students[0]
    review(world, item_of(world)["id"])
    save(world, lesson_day, lesson_id, [(s0, "excused")], excuseReasons={s0.id: "Другая причина"})
    (row,) = audit_rows(world, "excuse_reset")
    assert row.details["reason_changed"] is True
    assert (row.details["decided_after_deadline_before"], row.details["deadline_at_decision_before"]) == (True, day(-1).isoformat())
    assert stored(world) == (None, None)


def test_the_audit_of_a_decision_without_a_mark_has_no_mark_keys(world):
    excused_lesson(world, offset_days=3)
    pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))
    row = excuse_of(world)
    row.status, row.reviewed_by = "confirmed", world.owner.id                         # a historical decision
    world.db.commit()
    reset(world, str(row.id))
    (cleared,) = audit_rows(world, "excuse_reset")
    assert not any(key.endswith("_before") and "deadline" in key for key in cleared.details)


# ---- one reading of the clock: the day of the decision is the day of its deadline check -------------------


def freeze(monkeypatch, moment):
    monkeypatch.setattr(excuses_module, "_business_now", lambda: moment)


def test_midnight_in_tashkent_on_time_until_the_deadline_day_ends(world, monkeypatch):
    excused_lesson(world, offset_days=3)
    pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(20))              # the deadline is day(15)
    item_id = item_of(world)["id"]
    deadline_day = day(15)
    late_evening = datetime.combine(deadline_day, time(23, 30))
    freeze(monkeypatch, late_evening)
    body = review(world, item_id, "confirmed")
    assert shown(body) == (False, deadline_day.isoformat())                           # 23:30 on the deadline day: on time
    assert body["reviewed_at"] == late_evening.isoformat()
    after_midnight = datetime.combine(deadline_day + timedelta(days=1), time(0, 30))
    freeze(monkeypatch, after_midnight)
    body = review(world, item_id, "rejected")
    assert shown(body) == (True, deadline_day.isoformat())                            # 00:30 the day after: late
    assert body["reviewed_at"] == after_midnight.isoformat()
    assert stored(world) == (True, deadline_day)


def test_the_stored_time_and_the_measured_day_come_from_one_reading_of_the_clock(world, monkeypatch):
    """A clock that jumps over midnight between two readings: the decision is recorded at the FIRST reading and measured
    against that same day - not against a second reading taken after midnight."""
    excused_lesson(world, offset_days=3)
    pay(world, paid_on=day(-30), valid_from=day(-30), valid_to=day(20))
    excuse_id = excuse_of(world).id
    before = datetime.combine(day(15), time(23, 59, 59))
    readings = []

    def clock():
        readings.append(1)
        return before if len(readings) == 1 else before + timedelta(seconds=2)       # every later reading is after midnight

    monkeypatch.setattr(excuses_module, "_business_now", clock)
    AttendanceExcuseService(world.db).review(str(world.org.id), excuse_id, str(world.admin.id), "confirmed", None)
    row = excuse_of(world)
    assert row.reviewed_at == before
    assert (row.decided_after_deadline, row.deadline_at_decision) == (False, day(15))
