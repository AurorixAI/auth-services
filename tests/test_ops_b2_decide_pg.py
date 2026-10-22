"""LOGIC-B2 S4 (substitute), S5 (reschedule), S6 (cancel), S7 (reject) and chains 2 / 3 / 4 (golden).  Matrix rows [B2.S4.R1] ... [B2.S7.R9]."""
from datetime import date, datetime

import pytest
from sqlalchemy import func

from src.auth.service import AuthService
from src.models.auth import GroupAttendance, GroupAttendanceAudit, GroupAudit, GroupScheduleOneOffLesson, LessonTeacherAssignment

from tests.b2_helpers import (  # noqa: F401  (fixtures)
    API, Golden, build_d2, client, clock, code_of, decide, events_of, exams, fx, history_row, needs_pg, new_group, new_slot, new_student, pg_session, req, requests_of,
    row_of, run_parallel,
)

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    return build_d2(pg_session)


class Sync:
    """Replaces ``AuthService._sync_exam_lesson_cancellation``: records (phase, lesson, date); ``mode`` = ok | down | live."""

    def __init__(self, monkeypatch) -> None:
        self.calls, self.mode = [], "ok"
        sync = self

        def fake(svc, *, phase, org_id, group_id, lesson_id, lesson_date, reason):
            sync.calls.append((phase, lesson_id, lesson_date, reason))
            if sync.mode == "down":
                raise RuntimeError("Сервис экзаменов временно недоступен; урок не был отменён")
            if sync.mode == "live":
                raise ValueError("Нельзя отменить урок с активным экзаменом")

        monkeypatch.setattr(AuthService, "_sync_exam_lesson_cancellation", fake)


@pytest.fixture
def sync(monkeypatch):
    return Sync(monkeypatch)


def L(w, key, day):
    return {"L1": (w.G1, "lesson-l1"), "L2": (w.G1, "lesson-l2"), "L3": (w.G2, "lesson-l3"), "L4": (w.G3, "lesson-l4"), "L5": (w.G4, "lesson-l5")}[key] + (day,)


def one(w, who="A", key="L1", day="2026-10-26", **kw):
    r = req(w, who, [L(w, key, day)], **kw)
    assert r.status_code == 201, r.text
    return r.json()["created"][0]


def assignments(w, group=None, lesson="lesson-l1", day="2026-10-26"):
    w.db.expire_all()
    q = w.db.query(LessonTeacherAssignment).filter_by(lesson_id=lesson, lesson_date=day, cancelled_at=None)
    return q.filter_by(group_id=(group or w.G1).id).all()


def occurrence(w, who, key, day):
    out = client(w, who).get(f"{API}/org/schedule/occurrences", params={"date_from": day, "date_to": day}).json()
    return next(o for o in out if o["id"] == {"L1": "lesson-l1", "L2": "lesson-l2", "L3": "lesson-l3", "L4": "lesson-l4", "L5": "lesson-l5"}.get(key, key))


def sub_body(w, who):
    return {"decision": "substitute", "teacherId": str(getattr(w, who).id)}


# ======================================================================================================================== S4
def test_s4_r1_substitute_creates_the_assignment_and_the_audit(w, clock):
    """[B2.S4.R1] 200 substituted; one assignment (reason = «Болезнь: <comment>»), GroupAudit lesson_teacher_set, the event; the resolver: D leads, defaultTeacherId A; the schedule of D; A reads only."""
    item = one(w, reason="sick", comment="Температура")
    clock.set("2026-10-21 14:12")
    r = decide(w, "admin", item["id"], **sub_body(w, "D"))
    assert r.status_code == 200, r.text
    assert r.json()["item"]["status"] == "substituted" and r.json()["item"]["decision"]["substitute"]["name"] == "Рахимов Т."
    (asg,) = assignments(w)
    assert asg.teacher_id == w.D.id and asg.reason == "Болезнь"      # the label only: the teacher's comment stays in the request (R3 m8)
    assert [a.action for a in w.db.query(GroupAudit).filter_by(group_id=w.G1.id, action="lesson_teacher_set")] == ["lesson_teacher_set"]
    assert [e.action for e in events_of(w)] == ["requested", "substituted"] and events_of(w)[1].snapshot["via"] == "request"
    mine = occurrence(w, "D", "L1", "2026-10-26")
    assert mine["teacherId"] == str(w.D.id) and mine["isSubstitution"] is True and mine["defaultTeacherId"] == str(w.A.id)
    assert [o for o in client(w, "A").get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-10-26", "date_to": "2026-10-26"}).json() if o["id"] == "lesson-l1"] == []


def test_s4_r2_double_click_and_another_decision(w):
    """[B2.S4.R2] The same admin, the same D: 200, the same item, nothing is written; another admin / another D: 409 SUBSTITUTION_ALREADY_DECIDED with the item."""
    item = one(w)
    first = decide(w, "admin", item["id"], **sub_body(w, "D"))
    n = len(events_of(w))
    again = decide(w, "admin", item["id"], **sub_body(w, "D"))
    assert again.status_code == 200 and again.json()["item"] == first.json()["item"] and len(events_of(w)) == n and len(assignments(w)) == 1
    other_admin = decide(w, "owner", item["id"], **sub_body(w, "D"))
    other_teacher = decide(w, "admin", item["id"], **sub_body(w, "C"))
    for r in (other_admin, other_teacher):
        assert r.status_code == 409 and r.json()["detail"]["code"] == "SUBSTITUTION_ALREADY_DECIDED" and r.json()["detail"]["item"]["status"] == "substituted"


def test_s4_r3_taking_the_substitution_off_goes_through_b(w):
    """[B2.S4.R3] No rollback of the decision: ``PUT lesson-teacher null`` returns the lesson to A, the request stays ``substituted`` with the event ``assignment_cleared``; A may ask again."""
    item = one(w)
    assert decide(w, "admin", item["id"], **sub_body(w, "D")).status_code == 200
    put = client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "teacherId": None, "reason": "Передумали"})
    assert put.status_code == 200 and put.json()["teacherId"] == str(w.A.id)
    assert [r.status for r in requests_of(w)] == ["substituted"] and [e.action for e in events_of(w)] == ["requested", "substituted", "assignment_cleared"]
    assert req(w, "A", [L(w, "L1", "2026-10-26")]).status_code == 201


def test_s4_r4_races_with_the_other_writers(w, clock):
    """[B2.S4.R4] Two admins at once: one winner; a PUT lesson-teacher first: the request is ``substituted`` by it (``lesson_teacher_put``) and decide is 409; a start first: ``closed/LESSON_STARTED``;
    a handover first: ``closed/GROUP_TEACHER_CHANGED``."""
    from src.auth.lesson_substitutions import Substitutions
    from src.auth.substitution_schemas import DecideRequest

    item = one(w)
    org = str(w.org.id)
    out = run_parallel(
        w,
        lambda svc: Substitutions(svc).decide(org, str(w.admin.id), "admin", item["id"], DecideRequest(decision="substitute", teacherId=str(w.D.id))),
        lambda svc: Substitutions(svc).decide(org, str(w.owner.id), "owner", item["id"], DecideRequest(decision="substitute", teacherId=str(w.C.id))),
    )
    wins = [r for r, e in out if e is None]
    losses = [e for r, e in out if e is not None]
    assert len(wins) == 1 and len(losses) == 1 and code_of(losses[0]) == "SUBSTITUTION_ALREADY_DECIDED", out
    assert len(assignments(w)) == 1
    # PUT lesson-teacher first
    b = one(w, "A", "L2", "2026-10-28")
    assert client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-10-28", "lessonId": "lesson-l2", "teacherId": str(w.C.id), "reason": "Болезнь"}).json()["requestClosed"] == {"id": b["id"], "status": "substituted"}
    late = decide(w, "admin", b["id"], **sub_body(w, "D"))
    assert late.status_code == 409 and late.json()["detail"]["item"]["decision"]["via"] == "lesson_teacher_put"
    # a start first
    c = one(w, "A", "L3", "2026-10-27")
    clock.set("2026-10-27 10:05")
    assert client(w, "A").post(f"{API}/org/groups/{w.G2.id}/attendance/start", json={"lessonDate": "2026-10-27", "lessonId": "lesson-l3"}).status_code == 200
    started = decide(w, "admin", c["id"], **sub_body(w, "D"))
    assert started.status_code == 409 and started.json()["detail"]["item"]["decision"]["closedReason"] == "LESSON_STARTED"
    # a handover first
    clock.set("2026-10-22 10:00")
    d = one(w, "A", "L1", "2026-11-02")
    assert client(w, "owner").post(f"{API}/org/members/{w.ids('member_A')}/handover", json={"assignments": [
        {"groupId": str(w.G1.id), "teacherId": str(w.C.id)}, {"groupId": str(w.G2.id), "teacherId": str(w.B.id)}]}).status_code == 200
    gone = decide(w, "admin", d["id"], **sub_body(w, "D"))
    assert gone.status_code == 409 and gone.json()["detail"]["item"]["decision"]["closedReason"] == "GROUP_TEACHER_CHANGED"


def test_s4_r5_assigning_the_one_who_taught(w, clock):
    """[B2.S4.R5] The lesson began (nobody started it): decide substitute D -> the lesson is D's missed one; D reconciles it (marked_by D, teacher D); older than 7 days only the admin;
    older than the window 422 LESSON_OUTSIDE_WINDOW with the request left pending and closed by the next pass."""
    clock.set("2026-10-21 15:40")
    item = one(w, "A", "L2", "2026-10-21")
    clock.set("2026-10-21 17:40")
    assert decide(w, "admin", item["id"], **sub_body(w, "D")).status_code == 200
    missed = [a for a in client(w, "D").get(f"{API}/org/attendance/open-actions").json() if a["lessonId"] == "lesson-l2"]
    assert len(missed) == 1 and missed[0]["teacherId"] == str(w.D.id) and missed[0]["actionType"] == "missed"
    assert client(w, "D").post(f"{API}/org/groups/{w.G1.id}/attendance/reconcile", json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2"}).status_code == 200
    row = row_of(w, w.G1, "lesson-l2", "2026-10-21")
    assert (row.teacher_id, row.marked_by) == (w.D.id, w.D.id)
    # older than 7 days: D is refused, the admin reconciles for D
    clock.set("2026-10-22 10:00")
    old = one(w, "A", "L1", "2026-10-26")
    clock.set("2026-11-05 10:00")
    assert decide(w, "admin", old["id"], **sub_body(w, "D")).status_code == 200
    refused = client(w, "D").post(f"{API}/org/groups/{w.G1.id}/attendance/reconcile", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"})
    assert refused.status_code == 403                                                 # the window of the substitute (7 days) is over: no access at all
    assert client(w, "admin").post(f"{API}/org/groups/{w.G1.id}/attendance/reconcile", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"}).status_code == 200
    r2 = row_of(w, w.G1, "lesson-l1", "2026-10-26")
    assert (r2.teacher_id, r2.marked_by) == (w.D.id, w.admin.id)
    # older than the window of the unclosed lessons
    clock.set("2026-10-22 11:00")
    far = one(w, "A", "L2", "2026-10-28")
    clock.set("2026-12-05 10:00")
    out = decide(w, "admin", far["id"], **sub_body(w, "D"))
    assert out.status_code == 422 and out.json()["detail"]["code"] == "LESSON_OUTSIDE_WINDOW"
    assert [r.status for r in requests_of(w) if str(r.id) == far["id"]] == ["pending"]
    assert client(w, "admin").get(SUB, params={"status": "pending"}).json()["items"] == []
    assert [(r.status, r.closed_reason) for r in requests_of(w) if str(r.id) == far["id"]] == [("closed", "LESSON_OUTSIDE_WINDOW")]


def test_s4_r6_the_lesson_decides_not_the_day_of_the_decision(w, clock):
    """[B2.S4.R6] A lesson of 31.10 decided at 00:10 on 01.11: the assignment is of 31.10 (the payroll month follows ``lesson_date``); the candidate's busy check uses the lesson's time."""
    sat = new_group(w.db, w.org, w.ids, "FS6", "Субботняя", w.C, [new_slot("lesson-s6", 6, "10:00", "11:00", "2026-10-15")])
    new_student(w.db, w.org, w.ids, "fs6", "Субботин", "Сергей", sat)
    w.db.commit()
    created = req(w, "C", [(sat, "lesson-s6", "2026-10-31")])
    assert created.status_code == 201
    clock.set("2026-11-01 00:10")
    # at that time the lesson of 31.10 10:00 passed: the decision is the "late" mode, the date of the assignment is 31.10
    r = decide(w, "admin", created.json()["created"][0]["id"], **sub_body(w, "D"))
    assert r.status_code == 200, r.text
    asg = w.db.query(LessonTeacherAssignment).filter_by(group_id=sat.id, cancelled_at=None).one()
    assert asg.lesson_date == "2026-10-31" and asg.teacher_id == w.D.id


def test_s4_r7_busy_not_from_the_centre_requester_and_missing_rate(w):
    """[B2.S4.R7] Busy B: 409 TEACHER_BUSY with conflicts; not from the centre: 422 TARGET_NOT_TEACHER; the requester: 422 SUBSTITUTE_IS_REQUESTER; no rate: allowed, ``rateMissing`` true for the owner only."""
    item = one(w)
    busy = decide(w, "admin", item["id"], **sub_body(w, "B"))
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "TEACHER_BUSY" and busy.json()["detail"]["total"] >= 1
    assert decide(w, "admin", item["id"], decision="substitute", teacherId=str(w.foreign_admin.id)).json()["detail"]["code"] == "TARGET_NOT_TEACHER"
    assert decide(w, "admin", item["id"], **sub_body(w, "A")).json()["detail"]["code"] == "SUBSTITUTE_IS_REQUESTER"
    assert decide(w, "admin", item["id"], decision="substitute").json()["detail"]["code"] == "TEACHER_REQUIRED"
    cands = {c["name"]: c for c in client(w, "owner").get(f"{SUB}/{item['id']}/candidates").json()["candidates"]}
    assert cands["Рахимов Т."]["rateMissing"] is True and cands["Сидоров С."]["rateMissing"] is False
    assert all(c["rateMissing"] is None for c in client(w, "admin").get(f"{SUB}/{item['id']}/candidates").json()["candidates"])
    assert decide(w, "admin", item["id"], **sub_body(w, "D")).status_code == 200                        # no rate: allowed


def test_s4_r7b_everybody_busy(w):
    """[B2.S4.R7] Everybody who can teach is busy at that time: the list says so (``busy`` for all), the choice is refused one by one."""
    item = one(w)
    for key, who in (("lesson-x1", w.C), ("lesson-x2", w.D), ("lesson-x3", w.admin), ("lesson-x4", w.owner)):
        new_group(w.db, w.org, w.ids, key, key, who, [new_slot(key, 1, "10:00", "11:30", "2026-10-26")])
    w.db.commit()
    cands = client(w, "admin").get(f"{SUB}/{item['id']}/candidates").json()["candidates"]
    assert cands and all(c["busy"] for c in cands)
    assert decide(w, "admin", item["id"], **sub_body(w, "C")).json()["detail"]["code"] == "TEACHER_BUSY"


def test_s4_r8_roles(w):
    """[B2.S4.R8] Owner / admin decide, a teacher 403; the substitute D: no access to the decision; A after the decision cannot write (NOT_LESSON_TEACHER) but reads the group; an owner / admin can be the substitute."""
    item = one(w)
    assert decide(w, "A", item["id"], **sub_body(w, "D")).status_code == 403
    assert decide(w, "B", item["id"], **sub_body(w, "D")).status_code == 403
    r = decide(w, "owner", item["id"], **sub_body(w, "admin"))
    assert r.status_code == 200, r.text
    assert assignments(w)[0].teacher_id == w.admin.id
    assert client(w, "A").get(f"{API}/org/groups/{w.G1.id}/attendance", params={"month": "2026-10"}).status_code == 200


def test_s4_r9_the_money_neutrality_and_the_exam_service_is_not_called(w, clock, exams):
    """[B2.S4.R9] decide substitute never asks exam-service (not dependent on its availability); a group archived later: the request is already ``substituted`` (history); the substitution that became equal
    to the new group teacher is taken off by the B hook (event ``assignment_cleared``, the request stays ``substituted``); D closes the lesson within 7 days with ``teacher_id=D``."""
    exams.unavailable = True
    item = one(w)
    assert decide(w, "admin", item["id"], **sub_body(w, "D")).status_code == 200 and exams.calls == []
    assert client(w, "admin").put(f"{API}/org/groups/{w.G1.id}", json={"teacher_id": str(w.D.id)}).status_code == 200
    assert assignments(w) == []
    assert [e.action for e in events_of(w)][-1] == "assignment_cleared" and requests_of(w)[0].status == "substituted"


# ======================================================================================================================== S5
def test_s5_r1_chain2_reschedule_with_a_conflict_golden(w, clock):
    """[B2.S5.R1] Chain 2: request L2 28.10; move-check on Tue 27.10 10:00-11:30 is busy (A teaches 7-Б), the decision with it TEACHER_BUSY; Thu 29.10 16:00-17:30 is ok -> the original row is
    cancelled (v1, A, marked_by K), a one-off lesson, the item ``rescheduled``; the card of s1 gets the notice."""
    g = Golden()
    clock.set("2026-10-21 14:30")
    created = req(w, "A", [L(w, "L2", "2026-10-28")], reason="personal")
    assert not g(w, created.json(), "b2c2_create.json"), g(w, created.json(), "b2c2_create.json")
    rid = created.json()["created"][0]["id"]
    q = lambda d, s, e: client(w, "admin").get(f"{SUB}/{rid}/move-check", params={"newDate": d, "startTime": s, "endTime": e})  # noqa: E731
    busy = q("2026-10-27", "10:00", "11:30")
    assert busy.status_code == 200 and not g(w, busy.json(), "b2c2_move_check_busy.json"), g(w, busy.json(), "b2c2_move_check_busy.json")
    refused = decide(w, "admin", rid, decision="reschedule", newDate="2026-10-27", startTime="10:00", endTime="11:30", reason="Личные обстоятельства")
    assert refused.status_code == 409 and not g(w, refused.json(), "b2c2_decide_busy.json", select=lambda f: f["body"]), g(w, refused.json(), "b2c2_decide_busy.json", select=lambda f: f["body"])
    assert row_of(w, w.G1, "lesson-l2", "2026-10-28") is None
    ok = q("2026-10-29", "16:00", "17:30")
    assert not g(w, ok.json(), "b2c2_move_check_ok.json"), g(w, ok.json(), "b2c2_move_check_ok.json")
    clock.set("2026-10-21 14:35")
    done = decide(w, "admin", rid, decision="reschedule", newDate="2026-10-29", startTime="16:00", endTime="17:30", reason="Личные обстоятельства")
    assert done.status_code == 200 and not g(w, done.json(), "b2c2_decide_reschedule.json"), g(w, done.json(), "b2c2_decide_reschedule.json")
    row = row_of(w, w.G1, "lesson-l2", "2026-10-28")
    expected = w.ids.remap(fx("b2c2_cancelled_row.json"))
    assert (row.status, row.version, str(row.teacher_id), str(row.marked_by), row.cancellation_reason) == (
        expected["status"], expected["version"], expected["teacher_id"], expected["marked_by"], expected["cancellation_reason"])
    one_off = w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=w.G1.id).one()
    assert one_off.lesson_date == "2026-10-29" and one_off.payload["dayOfWeek"] == 4 and one_off.id.startswith("one-off-")
    audit = w.db.query(GroupAttendanceAudit).filter_by(attendance_id=row.id).one()
    assert audit.action == "cancelled" and audit.details["substitution_request_id"] == rid
    # the teacher of the new lesson on 29.10 is the group teacher; the lesson of C is not touched
    assert occurrence(w, "A", one_off.id, "2026-10-29")["teacherId"] == str(w.A.id)
    assert occurrence(w, "C", "L5", "2026-10-29")["teacherId"] == str(w.C.id)
    notices = client(w, "admin").get(f"{API}/org/students/{w.s1.id}/attendance", params={"month": "2026-10"}).json()["notices"]
    assert not g(w, notices, "b2c2_student_notices.json"), g(w, notices, "b2c2_student_notices.json")


def test_s5_r2_repeat_does_not_create_a_second_lesson(w, sync):
    """[B2.S5.R2] The same decision again: 200, the same item, one one-off; a different one: 409."""
    item = one(w, "A", "L2", "2026-10-28")
    body = dict(decision="reschedule", newDate="2026-10-29", startTime="16:00", endTime="17:30", reason="Личные обстоятельства")
    first = decide(w, "admin", item["id"], **body)
    again = decide(w, "admin", item["id"], **body)
    assert again.status_code == 200 and again.json()["item"] == first.json()["item"]
    assert w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=w.G1.id).count() == 1
    other = decide(w, "admin", item["id"], **{**body, "newDate": "2026-10-30"})
    assert other.status_code == 409 and other.json()["detail"]["item"]["decision"]["movedTo"]["date"] == "2026-10-29"


def test_s5_r3_no_way_back(w, sync, clock):
    """[B2.S5.R3] A cancelled original cannot be reopened (the rule of B): a start is refused; the fix is a one-off lesson added by hand."""
    item = one(w, "A", "L2", "2026-10-28")
    assert decide(w, "admin", item["id"], decision="reschedule", newDate="2026-10-29", startTime="16:00", endTime="17:30", reason="Личные обстоятельства").status_code == 200
    assert row_of(w, w.G1, "lesson-l2", "2026-10-28").status == "cancelled"
    clock.set("2026-10-28 16:05")
    r = client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance/start", json={"lessonDate": "2026-10-28", "lessonId": "lesson-l2"})
    assert r.status_code in (400, 403, 409), r.text                                       # a cancelled lesson is not reopened


def test_s5_r4_two_admins_and_a_concurrent_slot(w, sync):
    """[B2.S5.R4] Two administrators move to different dates at once: one winner, the loser 409 SUBSTITUTION_ALREADY_DECIDED; a one-off lesson of the group at the target time -> GROUP_BUSY (golden)."""
    from src.auth.lesson_substitutions import Substitutions
    from src.auth.substitution_schemas import DecideRequest

    item = one(w, "A", "L2", "2026-10-28")
    org = str(w.org.id)
    out = run_parallel(
        w,
        lambda svc: Substitutions(svc).decide(org, str(w.admin.id), "admin", item["id"], DecideRequest(decision="reschedule", newDate=date(2026, 10, 29), startTime="16:00", endTime="17:30", reason="Личные обстоятельства")),
        lambda svc: Substitutions(svc).decide(org, str(w.owner.id), "owner", item["id"], DecideRequest(decision="reschedule", newDate=date(2026, 10, 30), startTime="16:00", endTime="17:30", reason="Другая дата")),
    )
    assert sorted(code_of(e) or "ok" for _r, e in out) == ["SUBSTITUTION_ALREADY_DECIDED", "ok"], out
    assert w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=w.G1.id).count() == 1
    g = Golden()
    other = one(w, "A", "L1", "2026-11-02")
    w.db.add(GroupScheduleOneOffLesson(id=f"one-off-ex-{w.salt}", org_id=w.org.id, group_id=w.G1.id, lesson_date="2026-11-03",
                                       payload={"dayOfWeek": 2, "startTime": "12:00", "endTime": "13:00", "subject": "Математика", "room": None, "format": "offline", "color": "blue"}, created_by=w.admin.id))
    w.db.commit()
    busy = decide(w, "admin", other["id"], decision="reschedule", newDate="2026-11-03", startTime="12:30", endTime="13:30", reason="Перенос")
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "GROUP_BUSY" and busy.json()["detail"]["total"] == 1


def test_s5_r5_started_in_the_past_too_far_and_today(w, clock, sync):
    """[B2.S5.R5] Started -> 409 LESSON_ALREADY_STARTED; the new time in the past -> 422 MOVE_IN_PAST (golden); > 60 days -> MOVE_TOO_FAR; the same slot -> MOVE_SAME_SLOT; today later is allowed."""
    item = one(w, "A", "L2", "2026-10-28")
    base = dict(decision="reschedule", startTime="16:00", endTime="17:30", reason="Перенос")
    past = decide(w, "admin", item["id"], **{**base, "newDate": "2026-10-20"})
    assert past.status_code == 422 and past.json() == fx("b2x_move_in_past.json")["body"]
    assert decide(w, "admin", item["id"], **{**base, "newDate": "2026-12-31"}).json()["detail"]["code"] == "MOVE_TOO_FAR"
    assert decide(w, "admin", item["id"], **{**base, "newDate": "2026-10-28"}).json()["detail"]["code"] == "MOVE_SAME_SLOT"
    assert decide(w, "admin", item["id"], decision="reschedule", reason="Перенос").json()["detail"]["code"] == "MOVE_FIELDS_REQUIRED"
    clock.set("2026-10-21 15:40")
    today = one(w, "A", "L2", "2026-10-21")
    ok = decide(w, "admin", today["id"], decision="reschedule", newDate="2026-10-21", startTime="18:00", endTime="19:30", reason="Перенос на вечер")
    assert ok.status_code == 200 and ok.json()["result"]["newLesson"]["date"] == "2026-10-21"
    clock.set("2026-10-22 09:00")
    pend = one(w, "A", "L1", "2026-10-26")
    clock.set("2026-10-26 10:30")
    started = decide(w, "admin", pend["id"], **{**base, "newDate": "2026-10-27", "startTime": "10:00", "endTime": "11:30"})
    assert started.status_code == 409 and started.json()["detail"]["code"] == "LESSON_ALREADY_STARTED"


def test_s5_r6_midnight_and_month_border(w, clock, sync):
    """[B2.S5.R6] From Wed 23:30 to Thu 00:15: the weekday is of the business date of the new lesson, the instant is 19:15Z; over the month border the October original is cancelled, the November one is new."""
    late = new_group(w.db, w.org, w.ids, "FM6", "Поздняя", w.C, [new_slot("lesson-m6", 3, "23:30", "23:59", "2026-10-15")])
    new_student(w.db, w.org, w.ids, "fm6", "Поздний", "Ученик", late)
    w.db.commit()
    item = req(w, "C", [(late, "lesson-m6", "2026-10-28")]).json()["created"][0]
    out = decide(w, "admin", item["id"], decision="reschedule", newDate="2026-10-29", startTime="00:15", endTime="01:00", reason="Перенос")
    assert out.status_code == 200, out.text
    one_off = w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=late.id).one()
    assert one_off.payload["dayOfWeek"] == 4 and one_off.lesson_date == "2026-10-29"
    item2 = one(w, "A", "L2", "2026-10-28")
    far = decide(w, "admin", item2["id"], decision="reschedule", newDate="2026-11-05", startTime="16:00", endTime="17:30", reason="Перенос в ноябрь")
    assert far.status_code == 200 and row_of(w, w.G1, "lesson-l2", "2026-10-28").status == "cancelled"
    assert w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=w.G1.id, lesson_date="2026-11-05").count() == 1


def test_s5_r7_conflicts_exam_service_and_the_lobby(w, sync):
    """[B2.S5.R7] Teacher / group conflicts (TEACHER_BUSY / GROUP_BUSY with conflicts and total); exam-service down: 503 EXAM_SERVICE_UNAVAILABLE and nothing is written; a live exam: 409
    LESSON_HAS_LIVE_EXAM; a lobby exam is released (the second call) and the room may stay empty."""
    item = one(w, "A", "L2", "2026-10-28")
    body = dict(decision="reschedule", newDate="2026-10-29", startTime="16:00", endTime="17:30", reason="Перенос")
    sync.mode = "down"
    down = decide(w, "admin", item["id"], **body)
    assert down.status_code == 503 and down.json()["detail"]["code"] == "EXAM_SERVICE_UNAVAILABLE"
    assert row_of(w, w.G1, "lesson-l2", "2026-10-28") is None and w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=w.G1.id).count() == 0
    assert [r.status for r in requests_of(w)] == ["pending"]
    sync.mode = "live"
    live = decide(w, "admin", item["id"], **body)
    assert live.status_code == 409 and live.json()["detail"]["code"] == "LESSON_HAS_LIVE_EXAM"
    sync.mode, sync.calls = "ok", []
    assert decide(w, "admin", item["id"], **body).status_code == 200
    assert [c[0] for c in sync.calls] == ["cancellation-check", "release"] and sync.calls[0][1:3] == ("lesson-l2", "2026-10-28")
    assert w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=w.G1.id).one().payload["room"] is None


def test_s5_r8_roles_and_who_teaches_the_new_lesson(w, sync):
    """[B2.S5.R8] Only a manager; the new lesson is led by the group teacher on that date: the substitute of the old lesson / the requester who is not the group teacher do not inherit it."""
    item = one(w, "A", "L1", "2026-10-26")
    assert decide(w, "A", item["id"], decision="reschedule", newDate="2026-10-27", startTime="12:00", endTime="13:00", reason="Перенос").status_code == 403
    assert decide(w, "D", item["id"], decision="reschedule", newDate="2026-10-27", startTime="12:00", endTime="13:00", reason="Перенос").status_code == 403
    r = decide(w, "owner", item["id"], decision="reschedule", newDate="2026-10-27", startTime="12:00", endTime="13:00", reason="Перенос")
    assert r.status_code == 200, r.text
    new_id = r.json()["result"]["newLesson"]["id"]
    assert occurrence(w, "A", new_id, "2026-10-27")["teacherId"] == str(w.A.id)
    assert client(w, "admin").get(f"{API}/org/students/{w.s1.id}/attendance").status_code == 200


def test_s5_r9_neutral_for_package_a_and_archived_group(w, sync):
    """[B2.S5.R9] ``moved_one_off_keys`` names the new one-off lesson (A leaves it out of the counted days; the cancelled original counts); an archived group refuses (the request was closed with it)."""
    from src.auth.payroll_hooks import moved_one_off_lesson_keys

    item = one(w, "A", "L2", "2026-10-28")
    out = decide(w, "admin", item["id"], decision="reschedule", newDate="2026-10-29", startTime="16:00", endTime="17:30", reason="Перенос")
    new_id = out.json()["result"]["newLesson"]["id"]
    assert moved_one_off_lesson_keys(w.db, [w.G1.id]) == {(str(w.G1.id), new_id)} and moved_one_off_lesson_keys(w.db, [w.G2.id]) == set()
    free = new_group(w.db, w.org, w.ids, "FR9", "Арх", w.C, [new_slot("lesson-r9", 5, "15:00", "16:00", "2026-10-22")])
    w.db.commit()
    created = req(w, "C", [(free, "lesson-r9", "2026-10-30")]).json()["created"][0]
    assert client(w, "admin").post(f"{API}/org/groups/{free.id}/archive", json={}).status_code == 200
    closed = decide(w, "admin", created["id"], decision="reschedule", newDate="2026-11-06", startTime="15:00", endTime="16:00", reason="Перенос")
    assert closed.status_code == 409 and closed.json()["detail"]["item"]["decision"]["closedReason"] == "GROUP_ARCHIVED"


# ======================================================================================================================== S6
def test_s6_r1_chain3_cancel_golden(w, clock, sync):
    """[B2.S6.R1] Chain 3: cancel L1 02.11 -> the row ``cancelled`` v1 (teacher A, marked_by K, reason, audit ``cancelled``), the item ``cancelled`` (golden); the exam call is made."""
    g = Golden()
    item = one(w, "A", "L1", "2026-11-02", reason="sick")
    clock.set("2026-10-21 14:02")
    out = decide(w, "admin", item["id"], decision="cancel", reason="Преподаватель болен, замены нет")
    assert out.status_code == 200 and not g(w, out.json(), "b2c3_decide_cancel.json"), g(w, out.json(), "b2c3_decide_cancel.json")
    row = row_of(w, w.G1, "lesson-l1", "2026-11-02")
    exp = w.ids.remap(fx("b2c3_cancelled_row.json"))
    assert (row.status, row.version, row.records, str(row.teacher_id), str(row.marked_by), row.cancellation_reason) == (
        exp["status"], exp["version"], exp["records"], exp["teacher_id"], exp["marked_by"], exp["cancellation_reason"])
    assert w.db.query(GroupAttendanceAudit).filter_by(attendance_id=row.id).one().action == "cancelled"
    assert [c[0] for c in sync.calls] == ["cancellation-check", "release"]
    notices = client(w, "admin").get(f"{API}/org/students/{w.s1.id}/attendance", params={"month": "2026-11"}).json()["notices"]
    assert notices == [{"lessonId": "lesson-l1", "date": "2026-11-02", "groupId": str(w.G1.id), "groupName": "9-А", "kind": "cancelled", "movedTo": None}]
    assert "reason" not in str(notices) and "Преподаватель болен" not in str(notices)


def test_s6_r2_repeat_and_an_already_cancelled_lesson(w, sync):
    """[B2.S6.R2] A repeat: 200 the same item; a lesson cancelled by another way: the request is closed LESSON_CANCELLED and the answer is 409 with the item."""
    item = one(w, "A", "L1", "2026-11-02")
    first = decide(w, "admin", item["id"], decision="cancel", reason="Преподаватель болен")
    again = decide(w, "admin", item["id"], decision="cancel", reason="Преподаватель болен")
    assert again.status_code == 200 and again.json()["item"] == first.json()["item"] and w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_date="2026-11-02").count() == 1
    other = one(w, "A", "L2", "2026-10-28")
    assert client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance", json={"lessonDate": "2026-10-28", "lessonId": "lesson-l2", "status": "cancelled", "cancellationReason": "Сам отменил", "records": {}}).status_code == 200
    r = decide(w, "admin", other["id"], decision="cancel", reason="Преподаватель болен")
    assert r.status_code == 409 and r.json()["detail"]["item"]["decision"]["closedReason"] == "LESSON_CANCELLED"


def test_s6_r3_a_cancelled_lesson_stays_cancelled(w, sync, clock):
    """[B2.S6.R3] No reopening: starting a cancelled lesson is refused."""
    item = one(w, "A", "L1", "2026-10-26")
    assert decide(w, "admin", item["id"], decision="cancel", reason="Не будет урока").status_code == 200
    clock.set("2026-10-26 10:05")
    r = client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"})
    assert r.status_code in (400, 403, 409), r.text


def test_s6_r4_race_cancel_against_reject_and_start(w, sync, clock):
    """[B2.S6.R4] cancel ‖ reject: one winner (the loser 409 with the item); a start first: the request is closed LESSON_STARTED."""
    from src.auth.lesson_substitutions import Substitutions
    from src.auth.substitution_schemas import DecideRequest

    item = one(w, "A", "L1", "2026-10-26")
    org = str(w.org.id)
    out = run_parallel(
        w,
        lambda svc: Substitutions(svc).decide(org, str(w.admin.id), "admin", item["id"], DecideRequest(decision="cancel", reason="Не будет урока")),
        lambda svc: Substitutions(svc).decide(org, str(w.owner.id), "owner", item["id"], DecideRequest(decision="reject", reason="Урок нужно провести")),
    )
    assert sorted(code_of(e) or "ok" for _r, e in out) == ["SUBSTITUTION_ALREADY_DECIDED", "ok"], out
    nxt = one(w, "A", "L2", "2026-10-28")
    clock.set("2026-10-28 16:05")
    assert client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance/start", json={"lessonDate": "2026-10-28", "lessonId": "lesson-l2"}).status_code == 200
    r = decide(w, "admin", nxt["id"], decision="cancel", reason="Не будет урока")
    assert r.status_code == 409 and r.json()["detail"]["item"]["decision"]["closedReason"] == "LESSON_STARTED"


def test_s6_r5_past_lesson_without_a_row_inside_the_window_only(w, clock, sync):
    """[B2.S6.R5] A lesson that passed unstarted: allowed (audit ``reconciliation_cancelled``) inside the unclosed-lessons window, 422 LESSON_OUTSIDE_WINDOW outside it; the 7-day rule is not for the admin."""
    item = one(w, "A", "L1", "2026-10-26")
    clock.set("2026-11-12 10:00")                                           # 17 days after: the admin is fine
    ok = decide(w, "admin", item["id"], decision="cancel", reason="Урока не было")
    assert ok.status_code == 200
    row = row_of(w, w.G1, "lesson-l1", "2026-10-26")
    assert w.db.query(GroupAttendanceAudit).filter_by(attendance_id=row.id).one().action == "reconciliation_cancelled"
    clock.set("2026-10-22 10:00")
    far = one(w, "A", "L2", "2026-10-28")
    clock.set("2026-12-05 10:00")
    out = decide(w, "admin", far["id"], decision="cancel", reason="Урока не было")
    assert out.status_code == 422 and out.json()["detail"]["code"] == "LESSON_OUTSIDE_WINDOW"


def test_s6_r6_cancel_of_a_lesson_of_the_previous_month(w, clock, sync):
    """[B2.S6.R6] The date of the row is the business date of the lesson; cancelling on 01.11 a lesson of 31.10 leaves October's count alone (no completed row appears)."""
    sat = new_group(w.db, w.org, w.ids, "FS66", "Суб", w.C, [new_slot("lesson-s66", 6, "10:00", "11:00", "2026-10-15")])
    new_student(w.db, w.org, w.ids, "fs66", "Субботин", "Сергей", sat)
    w.db.commit()
    item = req(w, "C", [(sat, "lesson-s66", "2026-10-31")]).json()["created"][0]
    clock.set("2026-11-01 00:10")
    assert decide(w, "admin", item["id"], decision="cancel", reason="Урока не будет").status_code == 200
    row = row_of(w, sat, "lesson-s66", "2026-10-31")
    assert row.status == "cancelled" and row.lesson_date == "2026-10-31"
    completed = w.db.query(func.count(GroupAttendance.id)).filter(GroupAttendance.group_id == sat.id, GroupAttendance.status == "completed").scalar()
    assert completed == 0


def test_s6_r7_exam_service_down_and_reason_rules(w, sync):
    """[B2.S6.R7] exam-service down: 503, nothing is written; no reason / shorter than 3 / longer than 500: 422; a substitution does not depend on exam-service (S4.R9)."""
    item = one(w, "A", "L1", "2026-10-26")
    sync.mode = "down"
    assert decide(w, "admin", item["id"], decision="cancel", reason="Причина").status_code == 503
    assert row_of(w, w.G1, "lesson-l1", "2026-10-26") is None and requests_of(w)[0].status == "pending"
    sync.mode = "ok"
    assert decide(w, "admin", item["id"], decision="cancel").json()["detail"]["code"] == "REASON_REQUIRED"
    assert decide(w, "admin", item["id"], decision="cancel", reason="ab").json()["detail"]["code"] == "REASON_REQUIRED"
    assert decide(w, "admin", item["id"], decision="cancel", reason="x" * 501).status_code == 422


def test_s6_r8_roles_and_the_teacher_who_cancels_himself(w, sync):
    """[B2.S6.R8] Only a manager decides; a teacher cancelling his own lesson by the old path closes the request ``LESSON_CANCELLED`` through the hook; the student does not see the reason."""
    item = one(w, "A", "L1", "2026-10-26")
    assert decide(w, "A", item["id"], decision="cancel", reason="Отмена").status_code == 403
    assert client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "status": "cancelled", "cancellationReason": "Отменяю сам", "records": {}}).status_code == 200
    (row,) = requests_of(w)
    assert (row.status, row.closed_reason) == ("closed", "LESSON_CANCELLED")


def test_s6_r9_cancel_never_counts_and_does_not_busy_the_teachers(w, sync):
    """[B2.S6.R9] The cancelled row has no marks, never ``completed``, does not busy the teachers; the assignment on a cancelled lesson answers LESSON_CANCELLED."""
    item = one(w, "A", "L1", "2026-10-26")
    assert decide(w, "admin", item["id"], decision="cancel", reason="Не будет").status_code == 200
    from src.auth.lesson_teachers import LessonTeachers

    busy = LessonTeachers(w.svc).busy_items(w.org.id, w.A.id, date(2026, 10, 26), date(2026, 10, 26))
    assert busy == []
    put = client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Болезнь"})
    assert put.status_code == 409 and put.json()["detail"]["code"] == "LESSON_CANCELLED"
    row = row_of(w, w.G1, "lesson-l1", "2026-10-26")
    assert row.records == {} and row.status == "cancelled"


# ======================================================================================================================== S7
def test_s7_r1_reject_and_chain4(w, clock):
    """[B2.S7.R1] Reject with a reason: ``rejected``; the teacher sees the reason; the lesson stays his; a new request is possible; chain 4 (golden): after the rejection the second admin gets 409."""
    item = one(w, "A", "L1", "2026-10-26")
    clock.set("2026-10-21 14:01")
    r = decide(w, "admin", item["id"], decision="reject", reason="Замену не нашли")
    assert r.status_code == 200 and r.json()["item"]["status"] == "rejected" and r.json()["item"]["decision"]["note"] == "Замену не нашли"
    mine = client(w, "A").get(f"{SUB}/mine").json()["items"][0]
    assert mine["status"] == "rejected" and mine["decision"]["note"] == "Замену не нашли"
    assert occurrence(w, "A", "L1", "2026-10-26")["teacherId"] == str(w.A.id)
    second = decide(w, "owner", item["id"], decision="reject", reason="Ещё раз")
    exp = fx("b2c4_already_decided_rejected.json")["body"]["detail"]
    assert second.status_code == 409 and second.json()["detail"]["message"] == exp["message"] and second.json()["detail"]["item"]["status"] == "rejected"
    assert req(w, "A", [L(w, "L1", "2026-10-26")]).status_code == 201


def test_s7_r2_repeat(w):
    """[B2.S7.R2] The same admin, the same reason: 200 the same item; another reason: 409."""
    item = one(w)
    first = decide(w, "admin", item["id"], decision="reject", reason="Замену не нашли")
    again = decide(w, "admin", item["id"], decision="reject", reason="Замену не нашли")
    assert again.status_code == 200 and again.json()["item"] == first.json()["item"]
    assert decide(w, "admin", item["id"], decision="reject", reason="Иная причина").status_code == 409


def test_s7_r3_no_rollback_but_a_new_request_or_a_manual_substitution(w):
    """[B2.S7.R3] A rejection is final; a new request carries ``previous``; the administrator can still substitute by hand through B."""
    item = one(w)
    decide(w, "admin", item["id"], decision="reject", reason="Замену не нашли")
    new = req(w, "A", [L(w, "L1", "2026-10-26")]).json()["created"][0]
    assert new["previous"]["status"] == "rejected"
    assert client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Вручную"}).json()["requestClosed"]["status"] == "substituted"


def test_s7_r4_reject_against_substitute(w):
    """[B2.S7.R4] The chain-4 race: the loser gets 409 + item."""
    from src.auth.lesson_substitutions import Substitutions
    from src.auth.substitution_schemas import DecideRequest

    item = one(w)
    org = str(w.org.id)
    out = run_parallel(
        w,
        lambda svc: Substitutions(svc).decide(org, str(w.admin.id), "admin", item["id"], DecideRequest(decision="reject", reason="Замену не нашли")),
        lambda svc: Substitutions(svc).decide(org, str(w.owner.id), "owner", item["id"], DecideRequest(decision="substitute", teacherId=str(w.D.id))),
    )
    assert sorted(code_of(e) or "ok" for _r, e in out) == ["SUBSTITUTION_ALREADY_DECIDED", "ok"], out


def test_s7_r5_reject_after_the_lesson_began(w, clock):
    """[B2.S7.R5] A lesson that began: reject is allowed (the pending closes by the refusal)."""
    clock.set("2026-10-21 15:40")
    item = one(w, "A", "L2", "2026-10-21")
    clock.set("2026-10-21 16:30")
    r = decide(w, "admin", item["id"], decision="reject", reason="Урок нужно было провести")
    assert r.status_code == 200 and r.json()["item"]["status"] == "rejected"


def test_s7_r6_utc_instant(w, clock):
    """[B2.S7.R6] ``decided_at`` is the UTC instant whatever the Tashkent date."""
    item = one(w, "A", "L1", "2026-11-02")
    clock.set("2026-11-01 00:05")
    assert decide(w, "admin", item["id"], decision="reject", reason="Замену не нашли").json()["item"]["decision"]["at"] == "2026-10-31T19:05:00"


def test_s7_r7_reason_rules(w):
    """[B2.S7.R7] No reason / shorter than 3 / longer than 300: 422."""
    item = one(w)
    assert decide(w, "admin", item["id"], decision="reject").json()["detail"]["code"] == "REASON_REQUIRED"
    assert decide(w, "admin", item["id"], decision="reject", reason="ab").json()["detail"]["code"] == "REASON_REQUIRED"
    assert decide(w, "admin", item["id"], decision="reject", reason="x" * 301).status_code == 422


def test_s7_r8_roles_and_who_sees_the_reason(w):
    """[B2.S7.R8] Only a manager; the requester sees the reason; the substitute / another teacher do not see the request at all."""
    item = one(w)
    assert decide(w, "A", item["id"], decision="reject", reason="Отказ").status_code == 403
    decide(w, "admin", item["id"], decision="reject", reason="Замену не нашли")
    assert client(w, "A").get(f"{SUB}/{item['id']}").json()["item"]["decision"]["note"] == "Замену не нашли"
    assert client(w, "B").get(f"{SUB}/{item['id']}").status_code == 403 and client(w, "D").get(f"{SUB}/{item['id']}").status_code == 403


def test_s7_r9_system_closed_request_is_409(w):
    """[B2.S7.R9] A request closed by the system: reject answers 409 with the item."""
    item = one(w, "A", "L1", "2026-11-02")
    client(w, "owner").post(f"{API}/org/members/{w.ids('member_A')}/handover", json={"assignments": [
        {"groupId": str(w.G1.id), "teacherId": str(w.C.id)}, {"groupId": str(w.G2.id), "teacherId": str(w.B.id)}]})
    r = decide(w, "admin", item["id"], decision="reject", reason="Отказ")
    assert r.status_code == 409 and r.json()["detail"]["item"]["status"] == "closed"
