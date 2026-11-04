"""Money-1 I-4 and the lessons the owner CONFIRMED: a sheet is saved with an unresolved lesson after the warning with the list (``confirm_open_lessons: true``) - that is a decision of the centre,
not a failure of the system, and the nightly reconciliation (whose letters are about failures only) does not report it.  A lesson that is not in the list of the LAST save still is (PostgreSQL).

World of ``g_pay_world`` (the previous month, 50 %, price 600 000); an unresolved lesson is a lesson ``in_progress`` that nobody finished."""
from datetime import date

import pytest

from src.auth import money_reconcile as mr
from src.models.auth import GroupAttendance, PayrollSheet

from tests.money1_helpers import lesson, reopen_now  # noqa: F401
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_logicA_payroll_pg import API, LESSONS, fp, g_pay_world, owner
from tests.test_pay2_api_pg import fixed_today  # noqa: E402,F401  (fixture)
from tests.test_payroll_pg import lesson as lesson_row

pytestmark = needs_pg


def free_days(n):
    return [d for d in range(1, 29) if d not in LESSONS][:n]


def unresolved(world, m, day_number):
    """A lesson of the month that went on and was never finished (the teacher of the group is the teacher of the world)."""
    return lesson_row(world, m.d(day_number), {world.students[0]: "present"}, status="in_progress")


def key_of(world, row):
    return f"{row.lesson_date}|{row.group_id}|{row.lesson_id}"


def save(world, m, confirm, expect=201, revision=None):
    body = {"month": m.label, "teacher_id": str(world.teacher.id), "expected_fingerprint": fp(world, m)}
    if confirm is not None:
        body["confirm_open_lessons"] = confirm
    if revision is not None:
        body["revision"] = revision
    r = owner(world).post(f"{API}/org/payroll/sheets", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def i4(world):
    return [f for f in mr.reconcile_org(world.db, world.org.id) if f.invariant == "I-4"]


def sheet_row(world, m):
    world.db.expire_all()
    return world.db.query(PayrollSheet).filter_by(org_id=world.org.id, teacher_id=world.teacher.id, period_month=m.first).one()


def closed_unresolved(world):
    """What the screens count as unresolved in CLOSED months (the base of I-4): the check below must look at something."""
    from src.auth.service import AuthService

    return [a for a in AuthService(world.db).get_open_attendance_actions(str(world.org.id), actor_id=None, is_org_admin=True, include_closed=True) if a.periodClosed]


def test_a_sheet_saved_with_the_confirmed_lesson_is_no_failure_the_same_lesson_saved_without_the_question_is(world, fixed_today):
    m, s1, s2 = g_pay_world(world)
    a = unresolved(world, m, free_days(1)[0])
    saved = save(world, m, True)
    assert sheet_row(world, m).confirmed_open_lessons == [key_of(world, a)]
    assert [x.lessonDate for x in closed_unresolved(world)] == [a.lesson_date]                      # the month is closed and the lesson is unresolved: the reconciliation has something to look at ...
    assert i4(world) == []                                                                          # ... and finds nothing: the owner said «save anyway»
    # the same lesson, but the sheet is saved by an old client (no question): nobody confirmed anything
    reopen_now(world.db, world.org.id, world.teacher.id, m.label, world.owner.id, "Правим ведомость")
    save(world, m, None, revision=saved["revision"])
    assert sheet_row(world, m).confirmed_open_lessons is None or sheet_row(world, m).confirmed_open_lessons == []
    (hit,) = i4(world)
    assert (hit.invariant, hit.month, hit.actual) == ("I-4", m.label, 1) and hit.details["lessons"] == [f"{a.lesson_date}|{a.group_id}"]


def test_a_lesson_that_became_unresolved_after_the_confirmation_is_the_only_one_reported(world, fixed_today):
    m, s1, s2 = g_pay_world(world)
    first, second = free_days(2)
    a = unresolved(world, m, first)
    save(world, m, True)                                                                            # A is confirmed
    assert i4(world) == []
    b = unresolved(world, m, second)                                                                # B becomes unresolved later, past the rules (a row inserted under the feet of the code)
    (hit,) = i4(world)
    assert hit.actual == 1 and hit.details["lessons"] == [f"{b.lesson_date}|{b.group_id}"]          # B only - A stays the decision of the centre
    assert f"{a.lesson_date}|" not in " ".join(hit.details["lessons"])


def test_a_resave_replaces_the_list_with_the_fresh_one_and_a_reopening_keeps_the_last(world, fixed_today):
    m, s1, s2 = g_pay_world(world)
    first, second = free_days(2)
    a = unresolved(world, m, first)
    saved = save(world, m, True)
    assert sheet_row(world, m).confirmed_open_lessons == [key_of(world, a)]
    b = unresolved(world, m, second)
    assert len(i4(world)) == 1
    reopen_now(world.db, world.org.id, world.teacher.id, m.label, world.owner.id, "Правим ведомость")
    assert sheet_row(world, m).confirmed_open_lessons == [key_of(world, a)]                         # the reopening erases nothing
    resaved = save(world, m, True, revision=saved["revision"])
    assert sheet_row(world, m).confirmed_open_lessons == sorted([key_of(world, a), key_of(world, b)])       # the fresh list: both lessons
    assert i4(world) == []
    world.db.query(GroupAttendance).filter_by(id=a.id).update({"status": "completed"})             # A is finished meanwhile
    world.db.commit()
    reopen_now(world.db, world.org.id, world.teacher.id, m.label, world.owner.id, "Правим ведомость")
    save(world, m, True, revision=resaved["revision"])
    assert sheet_row(world, m).confirmed_open_lessons == [key_of(world, b)]                         # replaced, not added to
    assert i4(world) == []


def test_the_question_is_asked_first_and_the_confirmed_lessons_stay_in_the_warnings_of_the_closed_month(world, fixed_today):
    m, s1, s2 = g_pay_world(world)
    a = unresolved(world, m, free_days(1)[0])
    asked = owner(world).post(f"{API}/org/payroll/sheets", json={"month": m.label, "teacher_id": str(world.teacher.id), "expected_fingerprint": fp(world, m), "confirm_open_lessons": False})
    assert asked.status_code == 409 and asked.json()["detail"]["code"] == "OPEN_LESSONS_CONFIRM_REQUIRED" and asked.json()["detail"]["count"] == 1
    assert world.db.query(PayrollSheet).filter_by(org_id=world.org.id).count() == 0                  # nothing was saved by the question
    save(world, m, True)
    warnings = owner(world).get(f"{API}/org/payroll/months/{m.label}").json()["teachers"][0]["warnings"]
    assert warnings["open_lessons"] == 1 and [i["lessonDate"] for i in warnings["open_lesson_items"]] == [a.lesson_date]       # the owner still sees it in the closed month


def test_a_recalculation_keeps_the_confirmation_of_the_last_save(world, fixed_today):
    m, s1, s2 = g_pay_world(world)
    a = unresolved(world, m, free_days(1)[0])
    saved = save(world, m, True)
    reopen_now(world.db, world.org.id, world.teacher.id, m.label, world.owner.id, "Правим ведомость")
    diff = owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}/diff").json()
    patched = owner(world).patch(f"{API}/org/payroll/sheets/{saved['id']}", json={"revision": saved["revision"], "recalculate": True, "expected_fingerprint": diff["source_fingerprint"]})
    assert patched.status_code == 200, patched.text
    assert sheet_row(world, m).confirmed_open_lessons == [key_of(world, a)] and i4(world) == []


# ---------------------------------------------------------------------------------------------------- the re-save (PATCH) closes the month again: the same question, the same memory
def patch(world, saved, confirm, expect=200, **extra):
    diff = owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}/diff").json()
    body = {"revision": sheet_row(world, saved_month(world)).revision, "recalculate": True, "expected_fingerprint": diff["source_fingerprint"], **extra}
    if confirm is not None:
        body["confirm_open_lessons"] = confirm
    r = owner(world).patch(f"{API}/org/payroll/sheets/{saved['id']}", json=body)
    assert r.status_code == expect, r.text
    return r


def saved_month(world):
    class M:
        first = world.db.query(PayrollSheet.period_month).filter_by(org_id=world.org.id, teacher_id=world.teacher.id).scalar()
    return M


def test_the_resave_after_a_reopening_asks_first_and_the_confirmed_lesson_is_no_failure(world, fixed_today):
    m, s1, s2 = g_pay_world(world)
    a = unresolved(world, m, free_days(1)[0])
    saved = save(world, m, None)                                                                    # an old client saved without the question: nothing confirmed
    assert len(i4(world)) == 1
    reopen_now(world.db, world.org.id, world.teacher.id, m.label, world.owner.id, "Правим ведомость")
    before = sheet_row(world, m).revision
    asked = patch(world, saved, False, expect=409)
    assert asked.json()["detail"]["code"] == "OPEN_LESSONS_CONFIRM_REQUIRED" and asked.json()["detail"]["count"] == 1
    assert [i["lessonDate"] for i in asked.json()["detail"]["items"]] == [a.lesson_date]
    assert sheet_row(world, m).revision == before                                                   # the question changed nothing
    from src.auth import closed_period

    assert closed_period.period_state(world.db, world.org.id, world.teacher.id, m.first)["state"] == "reopened"
    patch(world, saved, True)                                                                       # «Сохранить всё равно»
    assert sheet_row(world, m).confirmed_open_lessons == [key_of(world, a)]
    assert closed_period.period_state(world.db, world.org.id, world.teacher.id, m.first)["state"] == "closed"
    assert i4(world) == []                                                                          # the month is closed again, the lesson is the decision of the centre


def test_the_resave_with_nothing_unresolved_stores_an_empty_list_and_a_note_only_change_is_asked_too(world, fixed_today):
    m, s1, s2 = g_pay_world(world)
    a = unresolved(world, m, free_days(1)[0])
    saved = save(world, m, True)
    world.db.query(GroupAttendance).filter_by(id=a.id).update({"status": "completed"})
    world.db.commit()
    reopen_now(world.db, world.org.id, world.teacher.id, m.label, world.owner.id, "Правим ведомость")
    patch(world, saved, False)                                                                      # nothing is unresolved now: no question, the old confirmation is gone with the lesson
    assert sheet_row(world, m).confirmed_open_lessons == []
    b = unresolved(world, m, free_days(2)[1])
    reopen_now(world.db, world.org.id, world.teacher.id, m.label, world.owner.id, "Ещё раз")
    note_only = owner(world).patch(f"{API}/org/payroll/sheets/{saved['id']}", json={"revision": sheet_row(world, m).revision, "note": "Комментарий", "confirm_open_lessons": False})
    assert note_only.status_code == 409 and note_only.json()["detail"]["code"] == "OPEN_LESSONS_CONFIRM_REQUIRED"      # every re-save that closes the month asks, not only the recalculation
    assert b.id is not None


def test_the_resave_with_true_replaces_the_list_with_the_fresh_one(world, fixed_today):
    m, s1, s2 = g_pay_world(world)
    first, second = free_days(2)
    a = unresolved(world, m, first)
    saved = save(world, m, True)
    b = unresolved(world, m, second)
    assert len(i4(world)) == 1                                                                      # B appeared after the closing
    reopen_now(world.db, world.org.id, world.teacher.id, m.label, world.owner.id, "Правим ведомость")
    patch(world, saved, True)
    assert sheet_row(world, m).confirmed_open_lessons == sorted([key_of(world, a), key_of(world, b)]) and i4(world) == []
    world.db.query(GroupAttendance).filter_by(id=a.id).update({"status": "completed"})
    world.db.commit()
    reopen_now(world.db, world.org.id, world.teacher.id, m.label, world.owner.id, "Правим ведомость")
    patch(world, saved, True)
    assert sheet_row(world, m).confirmed_open_lessons == [key_of(world, b)] and i4(world) == []     # replaced, not added to
