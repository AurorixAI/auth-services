"""LESSON-RULES С4 / С5 / С6: three guards of the teacher's own actions on a past lesson (PostgreSQL).

World: ``lesson_rules_helpers.world`` (group «Г» of the teacher A, Mon / Wed 10:00-11:30, nine students, 450 000).  Today is Thu 26.11 12:00 unless a test sets another time; the window of the
centre is 7 days (default).  The expectation of each test is in its comment BEFORE the call."""
import pytest

from src.models.auth import AttendanceExcuse

from tests.lesson_rules_helpers import MON, WED, kind_in_db, marks_of, reconcile, save, teacher_cancels, world, clock, pg_session, needs_pg, client, API  # noqa: F401  (fixtures)
from tests.ops_helpers import seen_version

pytestmark = needs_pg


# ================================================================================================ С4: nobody came, backdated
def test_c4_a_lesson_nobody_came_to_cannot_be_completed_by_its_teacher_the_day_after(pg_session, clock):
    """Wed 25.11 was never started; on Thu 26.11 the teacher «оформил» it (``started_at`` empty, the date is before today) and marks all nine absent: 409 ``NOBODY_CAME_BACKDATED`` whatever
    ``confirmNobodyCame`` says (the server judges, not the screen); the row stays ``in_progress`` and the lesson waits for the administrator."""
    w, g, people, members = world(pg_session, clock)
    reconcile(w, g, "2026-11-25")
    for extra in ({}, {"confirmNobodyCame": True}, {"confirmNobodyCame": False}):
        r = save(w, g, people, "2026-11-25", expect=409, **extra)
        d = r.json()["detail"]
        assert d["code"] == "NOBODY_CAME_BACKDATED" and d["message"] == "Никто не пришёл? Такой урок задним числом оформляет администратор — он видит его в уроках без отметки."
    assert kind_in_db(w, g, "2026-11-25", WED) == ("in_progress", None)
    w.db.expire_all()
    from src.models.auth import GroupAttendance
    assert w.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_date="2026-11-25", lesson_id=WED).one().status == "in_progress"


def test_c4_one_present_or_late_student_is_enough(pg_session, clock):
    """The same lesson: one «был» (or «опоздал») among the nine - the guard is silent: 200."""
    w, g, people, members = world(pg_session, clock)
    reconcile(w, g, "2026-11-25")
    save(w, g, people, "2026-11-25", marks=marks_of(people, first_present=1))
    reconcile(w, g, "2026-11-23", MON, expect=200)
    marks = {**marks_of(people), str(people[1].id): "late"}
    save(w, g, people, "2026-11-23", MON, marks=marks)


def test_c4_the_administrator_and_the_owner_complete_it_as_before(pg_session, clock):
    """The administrator (with or without the money right) and the owner complete the same lesson with nobody there: 200 (they are not the teacher who is paid for it)."""
    w, g, people, members = world(pg_session, clock)
    reconcile(w, g, "2026-11-25")
    save(w, g, people, "2026-11-25", who="admin", confirmNobodyCame=True)
    reconcile(w, g, "2026-11-23", MON)
    save(w, g, people, "2026-11-23", MON, who="owner", confirmNobodyCame=True)


def test_c4_a_lesson_started_on_its_day_is_not_backdated_even_if_closed_later(pg_session, clock):
    """«Начать» on Wed 25.11 10:30 (``started_at`` is set); the teacher closes it on Thu 26.11 with nobody there: the guard is not for a lesson started in its own day -> goes on to the old question
    (``NOBODY_CAME`` unless confirmed) -> 200 with ``confirmNobodyCame``."""
    w, g, people, members = world(pg_session, clock)
    clock.set("2026-11-25 10:30")
    r = client(w, "A").post(f"{API}/org/groups/{g.id}/attendance/start", json={"lessonDate": "2026-11-25", "lessonId": WED})
    assert r.status_code in (200, 201), r.text
    clock.set("2026-11-26 12:00")
    save(w, g, people, "2026-11-25", confirmNobodyCame=True)


def test_c4_forgot_to_press_start_and_closes_the_same_evening(pg_session, clock):
    """Wed 25.11 19:00: the teacher «оформил» the lesson of THIS day (``started_at`` empty, but the date is today, not before it) and marks nobody: not backdated -> 200 (``confirmNobodyCame``)."""
    w, g, people, members = world(pg_session, clock)
    clock.set("2026-11-25 19:00")
    reconcile(w, g, "2026-11-25")
    save(w, g, people, "2026-11-25", confirmNobodyCame=True)


# ================================================================================================ С5: his own started lesson is closed always
def test_c5_the_border_of_the_window_opened_at_23_58_closed_at_00_02(pg_session, clock):
    """Wed 18.11 is 7 days before Wed 25.11 (the window is 7: the border day passes).  23:58 on 25.11 the teacher «оформил» it; at 00:02 on 26.11 (age 8) he saves the marks in progress, then
    completes: both 200 (the old code answered 403 ``LESSON_TOO_OLD`` to the save).  Control: at 00:02 on 26.11 he cannot «оформить» another lesson of age 8 (Mon 18.11 is 8 days: 403)."""
    w, g, people, members = world(pg_session, clock)
    clock.set("2026-11-25 23:58")
    reconcile(w, g, "2026-11-18")
    clock.set("2026-11-26 00:02")
    save(w, g, people, "2026-11-18", status="in_progress", marks=marks_of(people, first_present=2))
    save(w, g, people, "2026-11-18", marks=marks_of(people, first_present=2))
    reconcile(w, g, "2026-11-16", MON, expect=403)


def test_c5_what_stays_old_a_lesson_not_started_a_cancel_and_a_correction_of_age_8(pg_session, clock):
    """Age 8 (Mon 16.11 on 26.11 is 10; Wed 18.11 is 8): a NOT started lesson cannot be «оформлен» (403 ``LESSON_TOO_OLD``, «Урок старше 7 дн. …»), «Урока не было» is refused (403), and the correction of
    a held lesson of age 8 (held on 25.11 by the teacher, corrected on 03.12 = 8 days later) is refused (403)."""
    w, g, people, members = world(pg_session, clock)
    r = reconcile(w, g, "2026-11-18", expect=403)
    assert r.json()["detail"]["code"] == "LESSON_TOO_OLD" and r.json()["detail"]["message"] == "Урок старше 7 дн. — его закрывает администратор или владелец."
    r = teacher_cancels(w, g, "2026-11-18", WED, expect=403)
    assert r.json()["detail"]["code"] == "LESSON_TOO_OLD"
    clock.set("2026-11-25 20:00")
    reconcile(w, g, "2026-11-25")
    save(w, g, people, "2026-11-25", marks=marks_of(people, first_present=3))
    clock.set("2026-12-03 12:00")
    r = save(w, g, people, "2026-11-25", marks=marks_of(people, first_present=4), correctionReason="Ошибся", expect=403)
    assert r.json()["detail"]["code"] == "LESSON_TOO_OLD"


# ================================================================================================ С6: a decided excuse is not erased by the teacher
def _excused_lesson(w, g, people, day="2026-11-25"):
    """Wed 25.11 in progress; student 0 is «уважительная» (reason «Болел»); the others present.  Returns the excuse id from the list of the administrator."""
    reconcile(w, g, day)
    marks = marks_of(people, mark="present")
    marks[str(people[0].id)] = "excused"
    save(w, g, people, day, status="in_progress", marks=marks, excuseReasons={str(people[0].id): "Болел"})
    w.db.expire_all()
    ex = w.db.query(AttendanceExcuse).filter_by(student_id=people[0].id).one()
    return ex.id, marks


def _decide(w, excuse_id, decision):
    r = client(w, "owner").post(f"{API}/org/attendance/excuses/{excuse_id}/review", json={"decision": decision})
    assert r.status_code == 200, r.text


def test_c6_an_approved_excuse_cannot_be_removed_by_the_teacher_the_owner_can(pg_session, clock):
    """Hand: the owner approves (``confirmed``).  The teacher saves the same student as «absent» (the excuse would be deleted): 409 ``EXCUSE_DECIDED``, the excuse stays.  The owner does the same
    through the same save: 200 (as before), the excuse is gone."""
    w, g, people, members = world(pg_session, clock)
    eid, marks = _excused_lesson(w, g, people)
    _decide(w, eid, "confirmed")
    changed = {**marks, str(people[0].id): "absent"}
    r = save(w, g, people, "2026-11-25", status="in_progress", marks=changed, expect=409)
    assert r.json()["detail"]["code"] == "EXCUSE_DECIDED" and r.json()["detail"]["message"] == "Уважительную причину уже рассмотрели — изменить отметку может администратор или владелец"
    w.db.expire_all()
    assert w.db.query(AttendanceExcuse).filter_by(id=eid).one().status == "confirmed"
    save(w, g, people, "2026-11-25", status="in_progress", marks=changed, who="owner")
    w.db.expire_all()
    assert w.db.query(AttendanceExcuse).filter_by(id=eid).first() is None


@pytest.mark.parametrize("decision", ["confirmed", "rejected"])
def test_c6_the_text_of_a_decided_excuse_is_not_rewritten_by_the_teacher(pg_session, clock, decision):
    """Approved or rejected: the teacher's new text «Другая причина» is refused (409 ``EXCUSE_DECIDED``), the old text stays."""
    w, g, people, members = world(pg_session, clock)
    eid, marks = _excused_lesson(w, g, people)
    _decide(w, eid, decision)
    r = save(w, g, people, "2026-11-25", status="in_progress", marks=marks, excuseReasons={str(people[0].id): "Другая причина"}, expect=409)
    assert r.json()["detail"]["code"] == "EXCUSE_DECIDED"
    w.db.expire_all()
    assert w.db.query(AttendanceExcuse).filter_by(id=eid).one().reason == "Болел"


def test_c6_a_rejected_excuse_may_become_absent_and_a_pending_one_is_free(pg_session, clock):
    """A rejected excuse -> «absent» by the teacher: 200 (the student is absent for the money either way - a refusal would be a dead end).  A pending excuse: the teacher changes the text and removes it: 200."""
    w, g, people, members = world(pg_session, clock)
    eid, marks = _excused_lesson(w, g, people)
    save(w, g, people, "2026-11-25", status="in_progress", marks=marks, excuseReasons={str(people[0].id): "Болел сильно"})        # pending: the text changes
    w.db.expire_all()
    assert w.db.query(AttendanceExcuse).filter_by(id=eid).one().reason == "Болел сильно"
    _decide(w, eid, "rejected")
    save(w, g, people, "2026-11-25", status="in_progress", marks={**marks, str(people[0].id): "absent"})
    w.db.expire_all()
    assert w.db.query(AttendanceExcuse).filter_by(id=eid).first() is None


def test_c6_the_administrator_without_the_money_right_is_not_bound_by_it(pg_session, clock):
    """The rule is the teacher's only (SPEC 6.8): an administrator without the right saves «absent» over an approved excuse as before: 200."""
    w, g, people, members = world(pg_session, clock)
    eid, marks = _excused_lesson(w, g, people)
    _decide(w, eid, "confirmed")
    save(w, g, people, "2026-11-25", status="in_progress", marks={**marks, str(people[0].id): "absent"}, who="admin")
