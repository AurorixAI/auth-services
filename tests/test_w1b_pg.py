"""W1 addendum (27.10): S16-S25, S10+, the tests «Т» (А-14, Д22, А-12), the lead's decisions on the report (1, 2, 3, 7) and the author of a one-off lesson (PostgreSQL).

Sources: schedule-series-edit-stop.md А-10 / А-12 / А-14 / Л-4; lesson-move-cancel-dayoff.md Д5 / Д10 / Д11 / Д17 / Д22 / Д28; substitutions.md D10 / D14 / D25 / D26 / D27 / D30 / D8."""
from datetime import date, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from src.models.auth import GroupAttendance, GroupAudit, GroupScheduleOverride, LessonSubstitutionEvent, LessonSubstitutionRequest as Req, LessonTeacherAssignment, OrganizationMember, StudentGroup
from src.auth.ops_rules import short_name
from tests.b2_helpers import API, build_d2, clock, decide, events_of, needs_pg, pg_session, req, requests_of, row_of  # noqa: F401  (fixtures)
from tests.ops_helpers import client, history_row, new_group, new_slot, new_student
from tests.pg_world import give_group_price
from tests.test_ops_b2_decide_pg import Sync, one  # noqa: F401

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"
DAYS = f"{API}/org/days-off"
REV = f"{API}/org/schedule/lessons/lesson-l1/recurrence"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")                        # Wednesday
    return build_d2(pg_session)


@pytest.fixture
def sync(monkeypatch):
    return Sync(monkeypatch)


def sheet(w, who, group, lesson, day, reason="Преподаватель болен", expect=200, **extra):
    r = client(w, who).post(f"{API}/org/groups/{group.id}/attendance", json={"lessonDate": day, "lessonId": lesson, "status": "cancelled", "records": {}, "cancellationReason": reason, **extra})
    assert r.status_code == expect, r.text
    return r


def stop(w, day, lesson="lesson-l1"):
    r = client(w, "admin").post(f"{API}/org/schedule/lessons/{lesson}/recurrence/end", json={"effectiveFrom": day, "reason": "Курс закончен"})
    assert r.status_code == 200, r.text
    return r.json()


def move(w, who, group, lesson, day, new_day, start="12:00", end="13:30", expect=200):
    r = client(w, who).post(f"{API}/org/groups/{group.id}/lesson-move", json={"lessonId": lesson, "lessonDate": day, "newDate": new_day, "startTime": start, "endTime": end, "reason": "Перенос"})
    assert r.status_code == expect, r.text
    return r


# ============================================================================================================ lead decisions
def test_d1_a_teacher_cancelling_his_started_empty_lesson_leaves_the_same_record_as_the_lesson_was_not_held(w, clock):
    """Decision 1: the O6 exception is one road with «Урока не было»: ``teacher_cancel``, kind centre, a record in «Замены»."""
    group = new_group(w.db, w.org, w.ids, "Gl", "Пустая", w.C, [new_slot("lesson-e", 3, "16:00", "17:30", "2026-10-14")])
    row = history_row(w.db, w.org, group, "lesson-e", "2026-10-21", w.C, [], status="in_progress", start="16:00")
    w.db.commit()
    clock.set("2026-10-21 17:20")
    r = client(w, "C").post(f"{API}/org/groups/{group.id}/attendance", json={"lessonDate": "2026-10-21", "lessonId": "lesson-e", "status": "cancelled", "records": {},
                                                                              "cancellationReason": "Урока не было"})
    assert r.status_code == 200, r.text
    w.db.refresh(row)
    assert (row.status, row.cancellation_kind) == ("cancelled", "centre")
    (rec,) = [x for x in requests_of(w) if x.group_id == group.id]
    assert (rec.status, rec.origin, rec.requester_id, rec.decided_by, rec.decided_via) == ("cancelled", "system", w.C.id, w.C.id, "teacher_cancel")
    assert events_of(w, rec.id)[0].snapshot["trigger"] == "teacher_cancel"
    # with marks or students on the roster it stays refused
    assert client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "status": "cancelled", "records": {},
                                                                                "cancellationReason": "Нет"}).status_code == 403


def test_d2_a_group_without_a_teacher_keeps_the_author_in_the_group_history(w):
    """Decision 2: no record of «Замены» (nobody to attribute it to) - the author, the moment and the cancellation stay in ``group_audit``."""
    group = new_group(w.db, w.org, w.ids, "Gn", "Без учителя", None, [new_slot("lesson-n", 3, "12:00", "13:00", "2026-10-14")])
    w.db.commit()
    sheet(w, "admin", group, "lesson-n", "2026-10-28", reason="Нет аудитории")
    assert [x for x in requests_of(w) if x.group_id == group.id] == []
    w.db.expire_all()
    (audit,) = w.db.query(GroupAudit).filter_by(group_id=group.id, action="lesson_cancelled").all()
    assert (audit.actor_id, audit.actor_role, audit.created_at is not None) == (w.admin.id, "admin", True)
    assert audit.snapshot == {"lessonId": "lesson-n", "lessonDate": "2026-10-28", "cancellationKind": "centre", "reason": "Нет аудитории", "version": 1}
    # a group with a teacher writes no such row (its record of «Замены» is the trace)
    sheet(w, "admin", w.G1, "lesson-l1", "2026-10-26")
    assert w.db.query(GroupAudit).filter_by(group_id=w.G1.id, action="lesson_cancelled").count() == 0


def test_d3_voiding_a_day_off_warns_about_the_substitutes_it_took_off(w):
    assert client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-10-28", "lessonId": "lesson-l2", "teacherId": str(w.D.id), "reason": "Болезнь"}).status_code == 200
    made = client(w, "owner").post(DAYS, json={"dateFrom": "2026-10-28", "dateTo": "2026-10-28", "reason": "Хайит"}).json()
    dry = client(w, "owner").post(f"{DAYS}/{made['dayOff']['id']}/void", params={"dry_run": True}).json()
    assert [(x["code"], x["date"], x["lessonId"]) for x in dry["warnings"]] == [("SUBSTITUTE_NOT_RESTORED", "2026-10-28", "lesson-l2")]
    out = client(w, "owner").post(f"{DAYS}/{made['dayOff']['id']}/void").json()
    (warning,) = out["warnings"]
    assert warning["code"] == "SUBSTITUTE_NOT_RESTORED" and warning["date"] == "2026-10-28" and warning["substituteName"] == short_name(w.D) and f"({short_name(w.D)})" in warning["message"] and "назначьте её заново" in warning["message"]
    assert [x["lessonId"] for x in out["restored"]] == ["lesson-l2"]
    # a day off without substitutes warns about nothing
    made2 = client(w, "owner").post(DAYS, json={"dateFrom": "2026-11-04", "dateTo": "2026-11-04", "reason": "Другой"}).json()
    assert client(w, "owner").post(f"{DAYS}/{made2['dayOff']['id']}/void").json()["warnings"] == []


# (the estimate - owner only, covered lessons, package students - is tested in tests/test_w1r3_pg.py: test_r3_7_*)


# ============================================================================================================ S16
def test_s16_a_lesson_of_a_stopped_series_is_not_moved_on_or_after_the_stop(w):
    stop(w, "2026-11-16")
    r = move(w, "admin", w.G1, "lesson-l1", "2026-11-09", "2026-11-17", expect=422)
    assert r.json()["detail"]["code"] == "SERIES_ENDED" and "16.11" in r.json()["detail"]["message"] and "на более раннюю дату" in r.json()["detail"]["message"]
    assert move(w, "A", w.G1, "lesson-l1", "2026-11-09", "2026-11-16", expect=422).json()["detail"]["code"] == "SERIES_ENDED"
    check = client(w, "A").get(f"{API}/org/groups/{w.G1.id}/lesson-move-check", params={"lessonId": "lesson-l1", "lessonDate": "2026-11-09", "newDate": "2026-11-18", "startTime": "12:00", "endTime": "13:30"}).json()
    assert (check["ok"], check["code"]) == (False, "SERIES_ENDED")
    assert row_of(w, w.G1, "lesson-l1", "2026-11-09") is None
    assert move(w, "admin", w.G1, "lesson-l1", "2026-11-09", "2026-11-12").json()["result"]["kind"] == "reschedule"          # before the stop: fine
    # the decision of a request refuses the same
    item = one(w, "A", "L1", "2026-11-02")
    r2 = decide(w, "admin", item["id"], decision="reschedule", newDate="2026-11-20", startTime="12:00", endTime="13:30", reason="Перенос")
    assert r2.status_code == 422 and r2.json()["detail"]["code"] == "SERIES_ENDED"
    # a copy of a move is a one-off lesson: it may be moved on (it is not a lesson of the series)
    new_id = client(w, "admin").post(f"{API}/org/groups/{w.G1.id}/lesson-move", json={"lessonId": "lesson-l1", "lessonDate": "2026-11-02", "newDate": "2026-11-03", "startTime": "12:00",
                                                                                     "endTime": "13:30", "reason": "Перенос"}).json()
    assert move(w, "admin", w.G1, new_id["result"]["newLesson"]["id"], "2026-11-03", "2026-11-20", "14:00", "15:30").status_code == 200


# ============================================================================================================ S17
def test_s17_the_reason_of_a_cancellation_is_changed_in_the_window_with_a_record_and_not_for_an_old_period(w):
    sheet(w, "admin", w.G1, "lesson-l1", "2026-10-26", reason="Преподаватель болен")
    kind = lambda day, reason="Это был праздник", kind="holiday", who="admin": client(w, who).put(f"{API}/org/groups/{w.G1.id}/attendance/cancellation-kind", json={
        "lessonDate": day, "lessonId": "lesson-l1", "cancellationKind": kind, "reason": reason})
    r = kind("2026-10-26")
    assert r.status_code == 200 and r.json()["changed"] is True
    recs = [x for x in requests_of(w) if x.closed_reason == "KIND_CHANGED"]
    assert len(recs) == 1 and (recs[0].status, recs[0].origin, recs[0].decided_by, recs[0].decided_via) == ("closed", "system", w.admin.id, "reason_change")
    ev = events_of(w, recs[0].id)[-1]
    assert ev.snapshot["trigger"] == "manager_reason_change" and ev.snapshot["cancellationKind"] == {"from": "centre", "to": "holiday"} and ev.snapshot["reason"] == "Это был праздник"
    listed = client(w, "admin").get(SUB, params={"status": "decided"}).json()["items"]
    assert any(i["decision"]["closedReasonText"] == "Вид отмены изменён" for i in listed)
    assert kind("2026-10-26", kind="holiday").json()["changed"] is False                                  # nothing changed: nothing recorded
    assert len([x for x in requests_of(w) if x.closed_reason == "KIND_CHANGED"]) == 1
    # the window: the 1st of the previous month (01.09 today) - a row older than that is closed for money
    w.db.add(GroupAttendance(org_id=w.org.id, group_id=w.G1.id, lesson_date="2026-08-31", lesson_id="lesson-l1", status="cancelled", records={}, roster_snapshot=[],
                             cancellation_reason="Давно", cancellation_kind="centre", marked_by=w.admin.id, version=1))
    w.db.add(GroupAttendance(org_id=w.org.id, group_id=w.G1.id, lesson_date="2026-09-07", lesson_id="lesson-l1", status="cancelled", records={}, roster_snapshot=[],
                             cancellation_reason="В окне", cancellation_kind="centre", marked_by=w.admin.id, version=1))
    w.db.commit()
    old = kind("2026-08-31")
    assert old.status_code == 422 and old.json()["detail"]["code"] == "REASON_CHANGE_TOO_OLD"
    assert row_of(w, w.G1, "lesson-l1", "2026-08-31").cancellation_kind == "centre"
    assert kind("2026-09-07").status_code == 200
    assert kind("2026-10-26", who="A").status_code == 403


# ============================================================================================================ S10+ (а-г)
def test_s10plus_a_lesson_of_today_that_has_started_is_not_cancelled_by_the_day_off_and_the_text_says_what_to_do(w, clock):
    clock.set("2026-10-28 16:30")
    made = client(w, "owner").post(DAYS, json={"dateFrom": "2026-10-28", "dateTo": "2026-10-28", "reason": "Сегодня"}).json()
    assert made["cancelled"] == [] and [x["reason"] for x in made["blocked"]] == ["Урок уже начался — отмените вручную, если не состоялся"]
    assert row_of(w, w.G1, "lesson-l2", "2026-10-28") is None


def test_s10plus_b_voiding_does_not_give_back_a_lesson_of_today_whose_time_has_passed(w, clock):
    clock.set("2026-10-28 09:00")
    made = client(w, "owner").post(DAYS, json={"dateFrom": "2026-10-28", "dateTo": "2026-10-29", "reason": "Хайит"}).json()
    assert {x["lessonId"] for x in made["cancelled"]} == {"lesson-l2", "lesson-l5"}
    clock.set("2026-10-28 17:00")                                                       # the 16:00 lesson of today was due
    out = client(w, "owner").post(f"{DAYS}/{made['dayOff']['id']}/void").json()
    assert [x["lessonId"] for x in out["restored"]] == ["lesson-l5"]                    # Thursday's is still ahead
    assert [(x["lessonId"], x["reason"]) for x in out["kept"]] == [("lesson-l2", "Урок сегодня уже начался: остаётся отменённым")]
    assert row_of(w, w.G1, "lesson-l2", "2026-10-28").status == "cancelled"             # it stays cancelled (not a «пропущенный» lesson)


def test_s10plus_c_a_move_or_a_one_off_on_a_day_off_date_stays_in_either_order(w):
    # order 1: the day off first, then a move onto the date: the copy stays, the answer warns (as before)
    made = client(w, "owner").post(DAYS, json={"dateFrom": "2026-10-28", "dateTo": "2026-10-28", "reason": "Хайит"}).json()
    r = move(w, "admin", w.G1, "lesson-l1", "2026-11-02", "2026-10-28", "10:00", "11:30")
    assert [x["code"] for x in r.json()["result"]["warnings"] if x["code"] == "DAY_OFF"] == ["DAY_OFF"]
    copy1 = r.json()["result"]["newLesson"]["id"]
    assert row_of(w, w.G1, copy1, "2026-10-28") is None
    # order 2: the move first, then a day off of ANOTHER date range that holds the date: the copy stays too (blocked with the reason), nothing is cancelled
    m2 = move(w, "admin", w.G1, "lesson-l1", "2026-11-09", "2026-11-11", "10:00", "11:30").json()["result"]["newLesson"]["id"]
    made2 = client(w, "owner").post(DAYS, json={"dateFrom": "2026-11-11", "dateTo": "2026-11-11", "reason": "Навруз"}).json()
    assert m2 in {x["lessonId"] for x in made2["blocked"]} and m2 not in {x["lessonId"] for x in made2["cancelled"]}
    assert "осознанно" in [x["reason"] for x in made2["blocked"] if x["lessonId"] == m2][0]
    assert row_of(w, w.G1, m2, "2026-11-11") is None
    # a one-off lesson: the same
    one_off = client(w, "admin").post(f"{API}/org/schedule/occurrences", json={"groupId": str(w.G2.id), "lessonDate": "2026-11-18", "startTime": "12:00", "endTime": "13:00", "purpose": "extra", "money": "paid"}).json()["id"]
    made3 = client(w, "owner").post(DAYS, json={"dateFrom": "2026-11-18", "dateTo": "2026-11-18", "reason": "Третий"}).json()
    assert one_off in {x["lessonId"] for x in made3["blocked"]} and row_of(w, w.G2, one_off, "2026-11-18") is None


def test_s10plus_d_voiding_opens_again_the_requests_the_day_off_closed(w):
    item = one(w, "A", "L2", "2026-10-28")
    made = client(w, "owner").post(DAYS, json={"dateFrom": "2026-10-28", "dateTo": "2026-10-28", "reason": "Хайит"}).json()
    old = [r for r in requests_of(w) if str(r.id) == item["id"]][0]
    assert (old.status, old.closed_reason) == ("closed", "LESSON_CANCELLED")
    closing = [e for e in events_of(w, old.id) if e.action == "closed"][-1]
    assert closing.snapshot["dayOffId"] == made["dayOff"]["id"] and closing.snapshot["trigger"] == "day_off"
    assert client(w, "admin").get(f"{SUB}/summary").json()["pendingTotal"] == 0
    client(w, "owner").post(f"{DAYS}/{made['dayOff']['id']}/void")
    pending = [r for r in requests_of(w) if r.status == "pending" and r.lesson_date == "2026-10-28"]
    assert len(pending) == 1 and (pending[0].origin, pending[0].requester_id, str(pending[0].previous_request_id)) == ("system", w.A.id, item["id"])
    assert [e.action for e in events_of(w, pending[0].id)] == ["requested"] or events_of(w, pending[0].id)[-1].action == "reopened"
    assert events_of(w, pending[0].id)[-1].snapshot["dayOffId"] == made["dayOff"]["id"]
    assert client(w, "admin").get(f"{SUB}/summary").json()["pendingTotal"] == 1
    # a request that had been DECIDED before the day off is not reopened
    item2 = one(w, "A", "L1", "2026-11-02")
    assert decide(w, "admin", item2["id"], decision="reject", reason="Нет замены").status_code == 200
    made2 = client(w, "owner").post(DAYS, json={"dateFrom": "2026-11-02", "dateTo": "2026-11-02", "reason": "Второй"}).json()
    client(w, "owner").post(f"{DAYS}/{made2['dayOff']['id']}/void")
    assert [r for r in requests_of(w) if r.status == "pending" and r.lesson_date == "2026-11-02"] == []


# ============================================================================================================ S18, S19, S20, S21, S22, S23, S24, S25
def test_s18_the_badge_runs_the_same_lazy_pass_as_the_list(w):
    item = one(w, "A", "L1", "2026-10-26")
    w.db.query(Req).filter_by(id=UUID(item["id"])).update({"lesson_date": "2026-10-27"})        # the lesson of the request is not on that date: it is void
    w.db.commit()
    assert client(w, "admin").get(f"{SUB}/summary").json()["pendingTotal"] == 0
    assert [(r.status, r.closed_reason) for r in requests_of(w)] == [("closed", "LESSON_REMOVED")]


def test_s19_a_requester_sees_only_his_own_requests_of_the_chain(w):
    first = one(w, "A", "L1", "2026-10-26", reason="sick", comment="Секретная причина")
    assert decide(w, "admin", first["id"], decision="substitute", teacherId=str(w.D.id)).status_code == 200
    second = req(w, "D", [(w.G1, "lesson-l1", "2026-10-26")], reason="personal", comment="Свой комментарий")
    assert second.status_code == 201, second.text
    sid = second.json()["created"][0]["id"]
    mine = client(w, "D").get(f"{SUB}/{sid}").json()
    assert mine["chain"] == [] and "Секретная причина" not in str(mine) and mine["item"]["previous"] is None          # #157 A1: not even the id / the name of the record of another teacher
    seen = client(w, "admin").get(f"{SUB}/{sid}").json()
    assert [c["id"] for c in seen["chain"]] == [first["id"]]
    # his own earlier request stays visible to him
    own = client(w, "D").get(f"{SUB}/{sid}").json()
    assert own["item"]["comment"] == "Свой комментарий"


def test_s20_a_request_that_falls_out_of_the_window_says_so_and_the_badge_warns_three_days_before(w, clock):
    item = one(w, "A", "L1", "2026-10-26")
    clock.set("2026-11-20 12:00")
    assert client(w, "admin").get(f"{SUB}/summary").json()["expiringTotal"] == 0              # far from the 1st
    clock.set("2026-11-29 12:00")
    s = client(w, "admin").get(f"{SUB}/summary").json()
    assert (s["pendingTotal"], s["expiringTotal"]) == (1, 1)                                   # 01.12: the window starts on 01.11, the lesson of 26.10 falls out
    assert client(w, "admin").get(SUB, params={"status": "pending"}).json()["expiringTotal"] == 1
    clock.set("2026-12-01 09:00")
    assert client(w, "admin").get(f"{SUB}/summary").json()["pendingTotal"] == 0                # the lazy pass closes it
    listed = client(w, "admin").get(SUB, params={"status": "decided", "dateFrom": "2026-10-01"}).json()["items"]
    assert [(i["status"], i["decision"]["closedReason"], i["decision"]["closedReasonText"], i["decision"]["by"]) for i in listed] == [("closed", "LESSON_OUTSIDE_WINDOW", "Не решено вовремя", None)]
    w.db.expire_all()
    ev = [e for e in events_of(w, UUID(item["id"])) if e.action == "closed"][-1]
    assert ev.snapshot["trigger"] == "window_expired"


def test_s21_a_teacher_who_becomes_an_administrator_has_his_waiting_requests_closed_and_his_substitutions_taken_off(w):
    assert client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Болезнь"}).status_code == 200
    mine = req(w, "D", [(w.G1, "lesson-l1", "2026-10-26")]).json()["created"][0]
    member = w.db.query(OrganizationMember).filter_by(org_id=w.org.id, user_id=w.D.id).one()
    r = client(w, "owner").put(f"{API}/org/members/{member.id}/role", params={"role": "admin"})
    assert r.status_code == 200, r.text
    w.db.expire_all()
    closed = [x for x in requests_of(w) if str(x.id) == mine["id"]][0]
    assert (closed.status, closed.closed_reason) == ("closed", "ROLE_CHANGED")
    assert events_of(w, closed.id)[-1].snapshot["trigger"] == "role_changed"
    # W1 round 2 (#156): he no longer leads lessons, so the substitution planned for him is TAKEN OFF (the same function as a dismissal, reason «Роль изменена»)
    assert r.json()["releasedSubstitutions"] == 1
    asg = w.db.query(LessonTeacherAssignment).filter_by(group_id=w.G1.id, teacher_id=w.D.id).one()
    assert asg.cancelled_at is not None
    from tests.pg_world import make_client
    d_as_admin = make_client(w, user=w.D, org="own", org_role="admin", role="admin")
    assert d_as_admin.get(f"{SUB}/mine").json()["assignedToMe"] == []
    occ = client(w, "admin").get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-10-26", "date_to": "2026-10-26"}).json()
    assert [o["teacherId"] for o in occ if o["id"] == "lesson-l1"] == [str(w.A.id)]
    assert {a_.action for a_ in w.db.query(GroupAudit).filter_by(group_id=w.G1.id)} >= {"lesson_teacher_cleared"}
    cleared = [x for x in w.db.query(LessonSubstitutionEvent) if x.action == "assignment_cleared" or (x.snapshot or {}).get("trigger") == "role_changed"]
    assert any((x.snapshot or {}).get("trigger") == "role_changed" for x in cleared)


def test_s22_a_release_never_takes_off_a_started_or_held_lesson_and_the_substitute_reads_what_was_cleared(w, clock):
    from src.auth.lesson_teachers import LessonTeachers

    put = lambda day: client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": day, "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Болезнь"})
    assert put("2026-10-26").status_code == 200 and put("2026-11-02").status_code == 200
    # the lesson of 26.10 has been held by D: a row exists
    w.db.add(GroupAttendance(org_id=w.org.id, group_id=w.G1.id, lesson_date="2026-10-26", lesson_id="lesson-l1", status="completed", records={}, roster_snapshot=[], teacher_id=w.D.id,
                             marked_by=w.D.id, version=2))
    w.db.commit()
    released = LessonTeachers(w.svc).release_substitutions_of(w.org.id, w.D.id, w.owner.id)
    w.db.commit()
    assert released == 1
    states = {a.lesson_date: a.cancelled_at is not None for a in w.db.query(LessonTeacherAssignment).filter_by(group_id=w.G1.id, teacher_id=w.D.id)}
    assert states == {"2026-10-26": False, "2026-11-02": True}
    mine = client(w, "D").get(f"{SUB}/mine").json()
    (cleared,) = mine["clearedForMe"]
    assert (cleared["lessonDate"], cleared["groupName"], cleared["clearedBy"]) == ("2026-11-02", "9-А", {"id": None, "name": None, "role": "owner"}) and cleared["clearedAt"]        # #157 R3: by the role only
    clock.set("2026-11-20 12:00")                                                               # 14 days later it is gone
    assert client(w, "D").get(f"{SUB}/mine").json()["clearedForMe"] == []


def test_s23_a_group_that_returns_from_the_archive_checks_its_substitutes(w):
    empty = new_group(w.db, w.org, w.ids, "Ea", "Архивная", w.C, [new_slot("lesson-e", 3, "12:00", "13:00", "2026-10-28")])
    w.db.commit()
    assert client(w, "admin").put(f"{API}/org/groups/{empty.id}/lesson-teacher", json={"lessonDate": "2026-10-28", "lessonId": "lesson-e", "teacherId": str(w.D.id), "reason": "Болезнь"}).status_code == 200
    assert client(w, "admin").post(f"{API}/org/groups/{empty.id}/archive", json={"reason": "Пауза"}).status_code == 200
    # while the group slept D got another lesson at that time
    busy = new_group(w.db, w.org, w.ids, "Eb", "Занятая", w.D, [new_slot("lesson-b", 3, "12:30", "13:30", "2026-10-12")])
    w.db.commit()
    r = client(w, "admin").post(f"{API}/org/groups/{empty.id}/restore", json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "TEACHER_BUSY", r.text
    w.db.expire_all()
    assert w.db.get(type(empty), empty.id).status == "archived"                               # nothing was restored
    w.db.query(type(busy)).filter_by(id=busy.id).update({"status": "archived"})
    w.db.commit()
    assert client(w, "admin").post(f"{API}/org/groups/{empty.id}/restore", json={}).status_code == 200


def test_s24_one_failing_request_of_a_bulk_decision_is_its_own_refusal(w, monkeypatch):
    from src.auth.lesson_substitutions import Substitutions

    a, b = one(w, "A", "L1", "2026-11-02"), one(w, "A", "L1", "2026-11-09")
    real = Substitutions._cancel

    def flaky(self, group, r, actor, body):
        if r.lesson_date == "2026-11-02":
            raise RuntimeError("boom")
        return real(self, group, r, actor, body)

    monkeypatch.setattr(Substitutions, "_cancel", flaky)
    r = client(w, "admin").post(f"{SUB}/decide-bulk", json={"ids": [a["id"], b["id"]], "decision": "cancel", "reason": "Курсы"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["updated"] == 1 and [(x["id"], x["code"]) for x in out["skipped"]] == [(a["id"], "DECISION_FAILED")]
    assert {str(x.id): x.status for x in requests_of(w)} == {a["id"]: "pending", b["id"]: "cancelled"}


def test_s25_an_explicit_null_takes_the_room_and_the_topic_off_and_an_absent_field_leaves_them(w):
    rev = lambda body: client(w, "admin").put(REV, json={"reason": "Изменение", **body})
    assert rev({"effectiveFrom": "2026-11-03", "room": "Каб. 5", "topic": "Дроби"}).status_code == 200
    assert rev({"effectiveFrom": "2026-11-10", "startTime": "12:00", "endTime": "13:30"}).status_code == 200             # an absent room / topic: kept
    occ = lambda day: next(o for o in client(w, "admin").get(f"{API}/org/schedule/occurrences", params={"date_from": day, "date_to": day}).json() if o["id"] == "lesson-l1")
    assert (occ("2026-11-16")["room"], occ("2026-11-16")["topic"]) == ("Каб. 5", "Дроби")
    out = rev({"effectiveFrom": "2026-11-17", "room": None, "topic": None})
    assert out.status_code == 200, out.text
    assert (occ("2026-11-23")["room"], occ("2026-11-23")["topic"], occ("2026-11-16")["room"]) == (None, None, "Каб. 5")       # from the date only
    assert rev({"effectiveFrom": "2026-11-17", "room": None}).json()["detail"]["code"] == "NO_CHANGES"


# ============================================================================================================ Т: А-14, Д22, А-12
def test_t_a14_the_cutover_of_the_regular_schedule_is_the_same_for_a_series_revision(w):
    """А-14: ``REGULAR_SCHEDULE_FROM`` (01.11.2026): a revision of a series changes the schedule the same way for a period that starts on 31.10 and for one that starts on 01.11 - the cutover
    is the ledger's business and a revision never moves a past or a closed period; what the revision may touch is only its own dates (from tomorrow)."""
    from src.auth import billing_ledger as bl

    assert bl.REGULAR_SCHEDULE_FROM == date(2026, 11, 1)
    out = client(w, "admin").put(REV, json={"effectiveFrom": "2026-10-30", "startTime": "12:00", "endTime": "13:30", "reason": "Время"})
    assert out.status_code == 200
    occ = lambda day: next((o for o in client(w, "admin").get(f"{API}/org/schedule/occurrences", params={"date_from": day, "date_to": day}).json() if o["id"] == "lesson-l1"), None)
    assert occ("2026-10-26")["startTime"] == "10:00"                                # a day before the revision: untouched (I1)
    assert occ("2026-11-02")["startTime"] == "12:00"
    # a one-off lesson on 31.10 (a Saturday) and one on 01.11 (a Sunday): both are lessons of their own dates, neither enters the regular schedule of the series
    for day in ("2026-10-31", "2026-11-01"):
        created = client(w, "admin").post(f"{API}/org/schedule/occurrences", json={"groupId": str(w.G1.id), "lessonDate": day, "startTime": "09:00", "endTime": "09:30", "purpose": "extra", "money": "paid"})
        assert created.status_code == 201 and created.json()["isOneOff"] is True
    assert [o["id"] for o in client(w, "admin").get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-10-31", "date_to": "2026-11-01"}).json() if o["groupId"] == str(w.G1.id)
            and not o["isOneOff"]] == []


def test_t_d22_move_then_cancel_the_copy_leaves_a_note_and_a_record_for_each_step(w):
    r = move(w, "admin", w.G1, "lesson-l1", "2026-10-26", "2026-10-28")
    copy = r.json()["result"]["newLesson"]["id"]
    sheet(w, "admin", w.G1, copy, "2026-10-28", reason="Не будет и на новой дате")
    by = {(x.lesson_id, x.status) for x in requests_of(w)}
    assert by == {("lesson-l1", "rescheduled"), (copy, "cancelled")}
    notes = client(w, "admin").get(f"{API}/org/students/{w.s1.id}/attendance", params={"month": "2026-10"}).json()["notices"]
    assert {(n["lessonId"], n["kind"]) for n in notes} == {("lesson-l1", "rescheduled"), (copy, "cancelled")}
    decided = client(w, "admin").get(SUB, params={"status": "decided"}).json()["items"]
    assert {(i["lesson"]["lessonId"], i["status"]) for i in decided} == {("lesson-l1", "rescheduled"), (copy, "cancelled")}


def test_t_a12_a_cancellation_set_in_advance_and_a_stop_that_comes_earlier_lose_the_deduction_and_the_record_together(pg_session, clock):
    """А-12: a «по вине центра» cancellation of 24.11 is set in advance; the series is stopped from 17.11: the cancelled row is removed with the stop (it is recorded in the audit), so the
    deduction that only existed through it is gone too, and the journal and the debt agree."""
    from tests.payf_helpers import API as PAPI, build_org, cancel, centre, client as pclient, statement
    from tests.test_payf_recalc_pg import month_world

    w2 = build_org(pg_session)
    g, s, m = month_world(w2, clock)                                                    # today 25.10, the setting «вычитать» is on
    cancel(w2, g, "lesson-mon", "2026-11-23")                                          # a Monday of the future, by the fault of the centre
    clock.set("2026-12-01 12:00")
    assert [x["details"]["recalc"]["text"] for x in statement(w2, m)["rows"] if x["type"] == "recalc"] == ["−46 000 сум: урок 23.11 отменён центром"]
    clock.set("2026-10-25 12:00")
    stopped = pclient(w2, "owner").post(f"{PAPI}/org/schedule/lessons/lesson-mon/recurrence/end", json={"effectiveFrom": "2026-11-16", "reason": "Курс закончен"})
    assert stopped.status_code == 200 and stopped.json()["removedCancellations"] == 1
    w2.db.expire_all()
    assert w2.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_id="lesson-mon", lesson_date="2026-11-23").count() == 0
    clock.set("2026-12-01 12:00")
    st = statement(w2, m)
    assert [x for x in st["rows"] if x["type"] == "recalc"] == [] and st["summary"]["recalculated"] == 0 and st["summary"]["reconciles"] is True


PURPOSE = {"admin": {"purpose": "extra", "money": "paid"}, "owner": {"purpose": "extra", "money": "paid"}}          # #154: the server requires the purpose of an owner / admin; a teacher sends none (= extra + wait)


# ============================================================================================================ the author of a one-off lesson
def test_author_a_one_off_lesson_shows_who_added_it_and_a_teachers_one_off_is_in_the_journal(w):
    mk = lambda who, group, day: client(w, who).post(f"{API}/org/schedule/occurrences", json={"groupId": str(group.id), "lessonDate": day, "startTime": "13:00", "endTime": "14:00", **PURPOSE.get(who, {})})
    by_teacher = mk("A", w.G1, "2026-10-29")
    assert by_teacher.status_code == 201, by_teacher.text
    assert by_teacher.json()["createdBy"] == {"id": str(w.A.id), "name": "Иванов И.", "role": "teacher"} or by_teacher.json()["createdBy"]["role"] == "teacher"
    by_admin = mk("admin", w.G2, "2026-10-30")
    assert by_admin.json()["createdBy"]["role"] == "admin" and by_admin.json()["createdBy"]["id"] == str(w.admin.id)
    occ = client(w, "admin").get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-10-29", "date_to": "2026-10-30"}).json()
    authors = {o["id"]: o["createdBy"] for o in occ if o["isOneOff"]}
    assert authors[by_teacher.json()["id"]]["id"] == str(w.A.id) and authors[by_admin.json()["id"]]["id"] == str(w.admin.id)
    assert all(o["createdBy"] is None for o in occ if not o["isOneOff"])
    journal = client(w, "admin").get(f"{API}/org/groups/{w.G1.id}/attendance", params={"date_from": "2026-10-29", "date_to": "2026-10-29"}).json()
    assert [l["createdBy"]["id"] for l in journal["lessons"] if l.get("id") == by_teacher.json()["id"]] == [str(w.A.id)]
    # the journal «Замены»: ONLY the teacher's one-off leaves an entry; the money is not touched (visibility only)
    entries = [x for x in requests_of(w) if x.closed_reason == "ONE_OFF_ADDED"]
    assert len(entries) == 1 and (entries[0].origin, entries[0].requester_id, entries[0].lesson_id, entries[0].lesson_date) == ("system", w.A.id, by_teacher.json()["id"], "2026-10-29")
    assert events_of(w, entries[0].id)[-1].snapshot["trigger"] == "teacher_one_off"
    listed = client(w, "admin").get(SUB, params={"status": "decided", "dateFrom": "2026-10-29"}).json()["items"]
    (item,) = [i for i in listed if i["decision"]["closedReason"] == "ONE_OFF_ADDED"]
    assert item["decision"]["closedReasonText"] == "Разовый урок добавил преподаватель" and item["teacher"]["id"] == str(w.A.id) and item["trigger"] == "teacher_one_off"


# ============================================================================================================ S27: a teacher removes the one-off lesson he added
def one_off(w, who, group, day, start="13:00", end="14:00"):
    r = client(w, who).post(f"{API}/org/schedule/occurrences", json={"groupId": str(group.id), "lessonDate": day, "startTime": start, "endTime": end, **PURPOSE.get(who, {})})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_s27_a_teacher_cancels_the_future_one_off_lesson_he_added_with_a_record(w):
    lid = one_off(w, "A", w.G1, "2026-10-29")
    r = sheet(w, "A", w.G1, lid, "2026-10-29", reason="Создал по ошибке")
    assert r.json()["warnings"] == []
    row = row_of(w, w.G1, lid, "2026-10-29")
    assert (row.status, row.cancellation_kind, row.marked_by, row.cancellation_reason) == ("cancelled", "centre", w.A.id, "Создал по ошибке")
    recs = [x for x in requests_of(w) if x.lesson_id == lid]
    (cancelled,) = [x for x in recs if x.status == "cancelled"]
    assert (cancelled.origin, cancelled.requester_id, cancelled.decided_by, cancelled.decided_via) == ("system", w.A.id, w.A.id, "teacher_cancel")
    assert events_of(w, cancelled.id)[0].snapshot["trigger"] == "teacher_cancel"
    assert any(x.closed_reason == "ONE_OFF_ADDED" for x in recs)                                   # the journal tells the whole story: added, then cancelled
    # an identical repeat is a 200 without a second record, a holiday kind is not his choice
    assert sheet(w, "A", w.G1, lid, "2026-10-29", reason="Создал по ошибке").json()["version"] == 1
    lid2 = one_off(w, "A", w.G1, "2026-10-30")
    assert sheet(w, "A", w.G1, lid2, "2026-10-30", cancellationKind="holiday", expect=403)


def test_s27_nothing_else_is_his_to_cancel_a_move_copy_the_one_off_of_an_administrator_a_past_or_started_one(w, clock):
    refused = lambda lesson, day, who="A": sheet(w, who, w.G1, lesson, day, expect=403).json()["detail"]["code"]
    # the copy of a move (the teacher moves his own lesson more than 60 minutes ahead)
    copy = move(w, "A", w.G1, "lesson-l1", "2026-10-26", "2026-10-28").json()["result"]["newLesson"]["id"]
    assert refused(copy, "2026-10-28") == "TEACHER_CANNOT_CANCEL"
    # a one-off of the administrator, even in the teacher's own group
    assert refused(one_off(w, "admin", w.G1, "2026-10-29"), "2026-10-29") == "TEACHER_CANNOT_CANCEL"
    # a one-off of the group of ANOTHER teacher is not even his group
    assert sheet(w, "B", w.G1, one_off(w, "A", w.G1, "2026-10-30"), "2026-10-30", expect=403)
    # a lesson that has started (a row exists, a start may have marks) and one whose time has passed
    started = one_off(w, "A", w.G2, "2026-10-21", "18:00", "19:00")
    clock.set("2026-10-21 18:30")
    assert sheet(w, "A", w.G2, started, "2026-10-21", expect=409).json()["detail"]["code"] == "LESSON_ALREADY_STARTED"          # #154 (Ц14): his OWN one-off whose time has come: 409, not the general refusal
    marked = one_off(w, "A", w.G1, "2026-10-22", "10:00", "11:00")
    clock.set("2026-10-22 10:05")
    assert client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance/start", json={"lessonDate": "2026-10-22", "lessonId": marked}).status_code == 200
    assert client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance", json={"lessonDate": "2026-10-22", "lessonId": marked, "status": "in_progress", "records": {str(w.s1.id): "present"}}).status_code == 200
    assert sheet(w, "A", w.G1, marked, "2026-10-22", expect=409).json()["detail"]["code"] == "LESSON_ALREADY_STARTED"
    assert row_of(w, w.G1, marked, "2026-10-22").status == "in_progress"


def test_s27_a_cancelled_one_off_deducts_nothing_before_or_after_the_cutover_of_the_regular_schedule(pg_session, clock):
    """Extra one-offs sit in neither the numerator nor the denominator of PAY-F: cancelling one «по вине центра» deducts nothing from the next payment - for a period that starts before
    01.11.2026 (REGULAR_SCHEDULE_FROM, the old way) and for one that starts after it."""
    from tests.payf_helpers import API as PAPI, build_org, client as pclient, statement
    from tests.test_payf_recalc_pg import month_world

    w2 = build_org(pg_session)
    g, s, m = month_world(w2, clock)                                                    # today Sunday 25.10, the setting «вычитать» is on, the student joined 06.10
    ids = {}
    for day in ("2026-10-28", "2026-11-04"):                                           # a Wednesday before the cutover and one after it
        r = pclient(w2, "A").post(f"{PAPI}/org/schedule/occurrences", json={"groupId": str(g.id), "lessonDate": day, "startTime": "15:00", "endTime": "16:00"})
        assert r.status_code == 201, r.text
        ids[day] = r.json()["id"]
    before = statement(w2, m)
    for day, lid in ids.items():
        c = pclient(w2, "A").post(f"{PAPI}/org/groups/{g.id}/attendance", json={"lessonDate": day, "lessonId": lid, "status": "cancelled", "records": {}, "cancellationReason": "По ошибке"})
        assert c.status_code == 200, c.text
    clock.set("2026-11-10 12:00")
    after = statement(w2, m)
    assert [x for x in after["rows"] if x["type"] == "recalc"] == [] and after["summary"]["recalculated"] == 0 and after["summary"]["reconciles"] is True
    # the regular lesson of the same day, cancelled by the fault of the centre, WOULD deduct - the control that the test sees deductions at all
    from tests.payf_helpers import cancel

    cancel(w2, g, "lesson-wed", "2026-10-28")
    assert statement(w2, m)["summary"]["recalculated"] == 46_000
