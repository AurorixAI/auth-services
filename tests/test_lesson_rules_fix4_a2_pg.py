"""LESSON-RULES FIX4 А2: the cancellation of a lesson whose START HAS COME does not take the substitute off it (the rule by the time, any kind of cancellation, whoever cancels); the cancellation of a
lesson that is still to come takes it off as before.  The lesson that is given back goes to the one who stands on it - the substitute - and he gets the money, the main teacher gets nothing.

World: ``lesson_rules_helpers.world`` (group «Г» of the teacher A «Иванов И.»: Mon / Wed 10:00-11:30, nine students, the fee 450 000 a month, today Thu 26.11.2026 12:00).  The teacher B
«Петров П.» takes Mon 23.11 (3 days ago) for A: A asks on 20.11, the owner decides «substitute B».  The rate of A and B: 50 % (the calculation: 450 000 / 12 lessons = 37 500 a lesson of a student,
half of it 18 750, nine present = 168 750 for a lesson everybody came to).  A lesson nobody held, or a cancelled one, pays nobody: 0 and 0."""
from datetime import date
from decimal import Decimal

import pytest

from src.models.auth import GroupAttendance, LessonSubstitutionEvent, LessonTeacherAssignment, PayrollRate, LessonSubstitutionRequest as Req
from tests.lesson_rules_helpers import MON, PAST_MON, WED, give_rate, kind_in_db, marks_of, reconcile, save, set_kind, teacher_cancels, world, clock, pg_session, needs_pg, client, API  # noqa: F401

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"
FULL_LESSON = 168_750            # 450 000 / 12 * 50 % * 9 present - by hand


def sub_world(pg_session, clock, day=PAST_MON, lesson=MON, now="2026-11-26 12:00", **kw):
    """The world, the rates of A and B, the request of A for ``day`` (made on 20.11) decided «substitute B»; the clock goes to ``now``."""
    w, g, people, members = world(pg_session, clock, **kw)
    give_rate(w)
    w.db.add(PayrollRate(org_id=w.org.id, teacher_id=w.B.id, lesson_type="group", percent=Decimal("50"), valid_from=date(2020, 1, 1), created_by=w.owner.id))
    w.db.commit()
    clock.set("2026-11-20 12:00")
    r = client(w, "A").post(SUB, json={"lessons": [{"groupId": str(g.id), "lessonId": lesson, "lessonDate": day}], "reasonCode": "sick"})
    assert r.status_code == 201, r.text
    rid = r.json()["created"][0]["id"]
    assert client(w, "owner").post(f"{SUB}/{rid}/decide", json={"decision": "substitute", "teacherId": str(w.B.id)}).status_code == 200
    clock.set(now)
    return w, g, people, rid


def standing(w, g, day=PAST_MON, lesson=MON):
    """Who stands on the lesson now: the teachers of the assignments that are in force (the main teacher is not among them)."""
    w.db.expire_all()
    return sorted(str(a.teacher_id) for a in w.db.query(LessonTeacherAssignment).filter_by(group_id=g.id, lesson_date=day, lesson_id=lesson, cancelled_at=None))


def cleared_events(w):
    w.db.expire_all()
    return w.db.query(LessonSubstitutionEvent).filter_by(org_id=w.org.id, action="assignment_cleared").count()


def pay(w):
    """The calculation of November for A and B (the owner's road): ``(A, B)`` totals."""
    r = client(w, "owner").post(f"{API}/org/payroll/months/2026-11/calculate", json={"teacher_ids": [str(w.A.id), str(w.B.id)]})
    assert r.status_code == 200, r.text
    got = {s["teacher_id"]: s["total_amount"] for s in r.json()["sheets"]}
    return got[str(w.A.id)], got[str(w.B.id)]


def mine(w, who="B"):
    return client(w, who).get(f"{SUB}/mine", params={"status": "all"}).json()


def held_by(w, g, people, who="B", day=PAST_MON, lesson=MON):
    """The substitute arranges the lesson that came back and completes it with nine present."""
    reconcile(w, g, day, lesson, who=who)
    save(w, g, people, day, lesson, who=who, marks=marks_of(people, first_present=9))


# ============================================================================================ the rule: a lesson that has begun keeps its substitute
@pytest.mark.parametrize("who,extra,kind", [("B", {}, "unconfirmed"), ("admin", {}, "unconfirmed"), ("owner", {"cancellationKind": "centre"}, "centre"),
                                            ("owner", {"cancellationKind": "holiday"}, "holiday")])
def test_a_cancelled_lesson_of_the_past_keeps_its_substitute_whoever_cancels(pg_session, clock, who, extra, kind):
    """Hand: Mon 23.11 10:00 began three days ago; B stands on it.  The main teacher A / the substitute B / an administrator without the right of money (all three: the cancellation WAITS, ``unconfirmed``)
    and the owner («по вине центра» / «праздник») cancel it «Урока не было».  Expected: 200, the answer has no warning «Замена снята», B still stands on the lesson, no ``assignment_cleared`` is written,
    B's block has nothing «снята»; the row is ``cancelled`` of the kind above; nobody is paid (0 and 0)."""
    w, g, people, rid = sub_world(pg_session, clock)
    assert standing(w, g) == [str(w.B.id)]
    r = teacher_cancels(w, g, who=who, **extra)
    assert r.json()["warnings"] == [] or all(x["code"] != "SUBSTITUTE_RELEASED" for x in r.json()["warnings"]), r.json()["warnings"]
    assert kind_of(w, g) == ("cancelled", kind)
    assert standing(w, g) == [str(w.B.id)]
    assert cleared_events(w) == 0
    assert mine(w)["clearedForMe"] == []
    assert pay(w) == (0, 0)


def test_the_main_teacher_cannot_cancel_the_lesson_the_substitute_stands_on(pg_session, clock):
    """Control of the fourth hand (the rule of the product, not changed): A, the teacher of the group, is told ``NOT_LESSON_TEACHER`` - the lesson is B's; nothing is written, B stands on it."""
    w, g, people, rid = sub_world(pg_session, clock)
    r = teacher_cancels(w, g, who="A", expect=403)
    assert r.json()["detail"]["code"] == "NOT_LESSON_TEACHER"
    assert standing(w, g) == [str(w.B.id)] and cleared_events(w) == 0 and kind_in_db(w, g) is None


def kind_of(w, g, day=PAST_MON, lesson=MON):
    w.db.expire_all()
    row = w.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_date=day, lesson_id=lesson).one()
    return row.status, row.cancellation_kind


def test_a_day_off_of_a_past_date_makes_the_report_a_holiday_and_the_substitute_stays(pg_session, clock):
    """Hand: the owner gives the day off 23.11 (a date of the past: the lesson that has begun is not touched by the day off, it only warns).  B reports «Урока не было»: the date is a day off, so the row
    is «праздник» (a holiday wins over what the author chose).  B still stands on the lesson, nothing is cleared, nobody is paid."""
    w, g, people, rid = sub_world(pg_session, clock)
    r = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": PAST_MON, "dateTo": PAST_MON, "reason": "Хайит"})
    assert r.status_code == 201, r.text
    assert kind_in_db(w, g) is None and standing(w, g) == [str(w.B.id)]
    teacher_cancels(w, g, who="B")
    assert kind_of(w, g) == ("cancelled", "holiday")
    assert standing(w, g) == [str(w.B.id)] and cleared_events(w) == 0
    assert pay(w) == (0, 0)


def test_a_cancellation_of_a_lesson_still_to_come_takes_the_substitute_off_as_before(pg_session, clock):
    """Control (nothing broken): B takes Mon 30.11 (4 days ahead).  The administrator cancels it from the lesson sheet: the answer says «Замена снята: Петров П.», the assignment is off, ONE
    ``assignment_cleared`` (trigger ``cancel``), B's block lists what was taken off."""
    w, g, people, rid = sub_world(pg_session, clock, day="2026-11-30")
    assert standing(w, g, "2026-11-30") == [str(w.B.id)]
    r = client(w, "admin").post(f"{API}/org/groups/{g.id}/attendance", json={"lessonDate": "2026-11-30", "lessonId": MON, "status": "cancelled", "records": {}, "cancellationReason": "Центр закрыт"})
    assert r.status_code == 200, r.text
    assert [(x["code"], "Замена снята" in x["message"]) for x in r.json()["warnings"]] == [("SUBSTITUTE_RELEASED", True)]
    assert standing(w, g, "2026-11-30") == []
    w.db.expire_all()
    cleared = [e for e in w.db.query(LessonSubstitutionEvent).filter_by(org_id=w.org.id, action="assignment_cleared")]
    assert [e.snapshot["trigger"] for e in cleared] == ["cancel"]
    assert [(c["lessonDate"], c["groupName"]) for c in mine(w)["clearedForMe"]] == [("2026-11-30", "Г")]


# ============================================================================================ the lesson goes back to the substitute
def test_the_lesson_was_after_all_goes_to_the_substitute_and_he_is_paid(pg_session, clock):
    """Hand: B reports «Урока не было» (waits); the owner presses «Урок был» (the road ``lessons/restore``, ``expectedKind`` unconfirmed): the lesson returns to the mark.  B arranges it and completes it
    with nine present: 168 750 for B, 0 for A - the lesson is B's, as it was before the mistaken report."""
    w, g, people, rid = sub_world(pg_session, clock)
    teacher_cancels(w, g, who="B")
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonId": MON, "lessonDate": PAST_MON, "reason": "Урок был", "expectedKind": "unconfirmed"})
    assert r.status_code == 200 and r.json()["returnedToReview"] is True and r.json()["warnings"] == [], r.text          # nothing «не вернулась»: nothing was taken off
    assert standing(w, g) == [str(w.B.id)] and pay(w) == (0, 0)
    held_by(w, g, people)
    assert pay(w) == (0, FULL_LESSON)


def test_the_substitute_takes_back_his_own_report_and_is_paid(pg_session, clock):
    """Hand: B reports «Урока не было» and withdraws it («Отозвать», the road ``withdraw-cancellation``) the same day.  The lesson is in his list «без отметки» again, B stands on it, he holds it:
    168 750 for B, 0 for A; the sheets are 0 and 0 while it is not held."""
    w, g, people, rid = sub_world(pg_session, clock)
    teacher_cancels(w, g, who="B")
    r = client(w, "B").post(f"{API}/org/groups/{g.id}/lessons/withdraw-cancellation", json={"lessonId": MON, "lessonDate": PAST_MON, "expectedKind": "unconfirmed"})
    assert r.status_code == 200 and r.json()["restored"] is True, r.text
    assert standing(w, g) == [str(w.B.id)] and pay(w) == (0, 0)
    held_by(w, g, people)
    assert pay(w) == (0, FULL_LESSON)


def test_a_confirmed_cancellation_given_back_to_the_mark_goes_to_the_substitute(pg_session, clock):
    """Hand: B reports, the owner confirms «по вине центра» (nine students are deducted 50 000 each - the money of the students is not this test's business), then gives the lesson back to the mark
    («Вернуть на отметку»: ``lessons/restore`` with ``expectedKind`` centre, the owner holds the right).  B arranges and holds it: 168 750 for B, 0 for A."""
    w, g, people, rid = sub_world(pg_session, clock)
    teacher_cancels(w, g, who="B")
    set_kind(w, g, "centre")
    assert kind_of(w, g) == ("cancelled", "centre") and standing(w, g) == [str(w.B.id)]
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonId": MON, "lessonDate": PAST_MON, "reason": "Подтвердили зря", "expectedKind": "centre"})
    assert r.status_code == 200 and r.json()["returnedToReview"] is True, r.text
    assert standing(w, g) == [str(w.B.id)] and pay(w) == (0, 0)
    held_by(w, g, people)
    assert pay(w) == (0, FULL_LESSON)


def test_waiting_then_a_day_off_then_the_day_off_voided_then_the_lesson_was_goes_to_the_substitute(pg_session, clock):
    """Hand: B reports (waits); the owner gives the day off 23.11 - the cancellation becomes «праздник» (nothing to confirm); the owner voids the day off - the cancellation waits again; «Урок был»;
    B holds the lesson: 168 750 for B, 0 for A.  The assignment of B is in force after every step; the sheets are 0 and 0 until B holds it."""
    w, g, people, rid = sub_world(pg_session, clock)
    teacher_cancels(w, g, who="B")
    r = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": PAST_MON, "dateTo": PAST_MON, "reason": "Хайит"})
    assert r.status_code == 201, r.text
    assert kind_of(w, g) == ("cancelled", "holiday") and standing(w, g) == [str(w.B.id)] and pay(w) == (0, 0)
    assert client(w, "owner").post(f"{API}/org/days-off/{r.json()['dayOff']['id']}/void").status_code == 200
    assert kind_of(w, g) == ("cancelled", "unconfirmed") and standing(w, g) == [str(w.B.id)] and pay(w) == (0, 0)
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonId": MON, "lessonDate": PAST_MON, "reason": "Урок был", "expectedKind": "unconfirmed"})
    assert r.status_code == 200 and r.json()["warnings"] == [], r.text
    held_by(w, g, people)
    assert pay(w) == (0, FULL_LESSON)


# ============================================================================================ a cancelled lesson of the past is a fact: the releases do not touch it
def _today_world(pg_session, clock):
    """B takes Wed 25.11; on 25.11 12:00 (the lesson ended 11:30, today) B reports «Урока не было» - waits.  Returns the world with the assignment in force."""
    w, g, people, rid = sub_world(pg_session, clock, day="2026-11-25", lesson=WED, now="2026-11-25 12:00")
    teacher_cancels(w, g, who="B", day="2026-11-25", lesson=WED)
    assert kind_of(w, g, "2026-11-25", WED) == ("cancelled", "unconfirmed") and standing(w, g, "2026-11-25", WED) == [str(w.B.id)]
    return w, g, people, requests_of_wed(w, g)


def requests_of_wed(w, g):
    w.db.expire_all()
    return sorted((str(r.id), r.status) for r in w.db.query(Req).filter_by(group_id=g.id, lesson_date="2026-11-25", lesson_id=WED))


def _unchanged(w, g, before):
    """The assignment of B is in force, nothing is cleared, no request of the lesson is opened or changed (the records are the very same as before the release)."""
    assert standing(w, g, "2026-11-25", WED) == [str(w.B.id)] and cleared_events(w) == 0
    assert requests_of_wed(w, g) == before and all(st != "pending" for _i, st in before)


def test_the_substitute_leaves_the_centre_the_cancelled_lesson_of_today_keeps_him(pg_session, clock):
    """Hand: B is removed from the centre on 25.11 12:00; the lesson of today (over, cancelled-waiting) is a fact: the assignment is kept, nothing cleared, no new request."""
    w, g, people, before = _today_world(pg_session, clock)
    assert client(w, "owner").delete(f"{API}/org/members/{w.ids('member_B')}").status_code == 200
    _unchanged(w, g, before)


def test_the_substitute_stops_being_a_teacher_the_cancelled_lesson_of_today_keeps_him(pg_session, clock):
    """Hand: B becomes an administrator; the assignment on the cancelled lesson of today is kept, no new request is opened."""
    w, g, people, before = _today_world(pg_session, clock)
    r = client(w, "owner").put(f"{API}/org/members/{w.ids('member_B')}/role", params={"role": "admin"})
    assert r.status_code == 200, r.text
    _unchanged(w, g, before)


def test_the_substitute_becomes_the_teacher_of_the_group_the_cancelled_lesson_of_today_keeps_the_assignment(pg_session, clock):
    """Hand: the owner makes B the teacher of the group (the assignment equal to the group teacher is released by ``release_equal_to_group_teacher`` - for a lesson not begun); the cancelled lesson of
    today keeps its assignment, the record of the request is not reopened."""
    w, g, people, before = _today_world(pg_session, clock)
    r = client(w, "owner").put(f"{API}/org/groups/{g.id}", json={"teacher_id": str(w.B.id)})
    assert r.status_code == 200, r.text
    _unchanged(w, g, before)


# ============================================================================================ what the substitute sees
def test_the_substitute_sees_a_cancelled_lesson_of_the_past_as_his_not_as_a_coming_replacement(pg_session, clock):
    """Hand: B takes Wed 25.11 (the lesson of today, over at 12:00).  Before the report: «Вам назначена замена» (``assignedToMe``) lists it (control).  B reports «Урока не было»: the list does not show
    the cancelled lesson as a coming replacement; B's journal has the lesson as his with the mark «Отмена ждёт подтверждения» (``waiting``) and the right to withdraw; B's schedule has it as his
    (``isSubstitution``, ``teacherId`` B); the main teacher A sees the same lesson as replaced by B and cannot act on it."""
    w, g, people, rid = sub_world(pg_session, clock, day="2026-11-25", lesson=WED, now="2026-11-25 12:00")
    assert [(a["lessonDate"], a["lessonId"]) for a in mine(w)["assignedToMe"]] == [("2026-11-25", WED)]
    teacher_cancels(w, g, who="B", day="2026-11-25", lesson=WED)
    assert mine(w)["assignedToMe"] == []
    jb = next(l for l in client(w, "B").get(f"{API}/org/groups/{g.id}/attendance", params={"month": "2026-11"}).json()["lessons"] if l["date"] == "2026-11-25")
    assert (jb["status"], jb["waiting"], jb["canWithdraw"], jb["canActSelf"]) == ("cancelled", True, True, True)
    sb = next(i for i in client(w, "B").get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-11-25", "date_to": "2026-11-25"}).json() if i["id"] == WED)
    sa = next(i for i in client(w, "A").get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-11-25", "date_to": "2026-11-25"}).json() if i["id"] == WED)
    assert (sb["isSubstitution"], sb["teacherId"], sa["teacherId"], sa["replacedBy"]["id"], sa["canActSelf"]) == (True, str(w.B.id), str(w.B.id), str(w.B.id), False)


def test_a_waiting_request_of_a_cancelled_lesson_of_the_past_is_closed_and_the_return_does_not_open_it(pg_session, clock):
    """Hand: A asks for a replacement of Wed 25.11 on 20.11 and nobody is chosen (the request WAITS); on 25.11 12:00 the owner... the teacher A reports «Урока не было»: the waiting request is decided by
    this cancellation (status ``cancelled``); «Урок был» does not open it again (one record, ``cancelled``, with the return in its events)."""
    w, g, people, members = world(pg_session, clock)
    clock.set("2026-11-20 12:00")
    r = client(w, "A").post(SUB, json={"lessons": [{"groupId": str(g.id), "lessonId": WED, "lessonDate": "2026-11-25"}], "reasonCode": "sick"})
    rid = r.json()["created"][0]["id"]
    clock.set("2026-11-25 12:00")
    teacher_cancels(w, g, day="2026-11-25", lesson=WED)
    w.db.expire_all()
    assert [(str(x.id), x.status) for x in w.db.query(Req).filter_by(group_id=g.id, lesson_date="2026-11-25")] == [(rid, "cancelled")]
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonId": WED, "lessonDate": "2026-11-25", "reason": "Урок был", "expectedKind": "unconfirmed"})
    assert r.status_code == 200, r.text
    w.db.expire_all()
    rows = [(str(x.id), x.status) for x in w.db.query(Req).filter_by(group_id=g.id, lesson_date="2026-11-25") if x.status == "pending" or str(x.id) == rid]
    assert (rid, "cancelled") in rows and all(s != "pending" for _i, s in rows), rows


# ============================================================================================ expected and not changed (tests only)
def test_an_archived_group_the_cancellation_with_a_substitute_is_confirmed_but_not_returned(pg_session, clock):
    """Hand: the group (no students: one with balances cannot be archived) is archived while B's report waits.  The confirmation works (200, «по вине центра», nobody to deduct from); «Урок был» is
    409 ``GROUP_ARCHIVED``."""
    w, g, people, rid = sub_world(pg_session, clock, students=0)
    teacher_cancels(w, g, who="B")
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/archive", json={"missedLessonsAction": "cancel", "reason": "Группа закрыта"})
    assert r.status_code == 200, r.text
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/lessons/restore", json={"lessonId": MON, "lessonDate": PAST_MON, "reason": "Урок был", "expectedKind": "unconfirmed"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "GROUP_ARCHIVED", r.text
    set_kind(w, g, "centre")
    assert kind_of(w, g) == ("cancelled", "centre")
