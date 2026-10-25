"""P1.2: every substitution is in the journal «Замены» - one path with LOGIC-B2 and #124 / P1.1.  Spec: artifacts/p1/P1_2_DIRECT_SUBSTITUTION_JOURNAL_SPEC.md (scenarios S1..S8 = the table 1..8).
World D2 of LOGIC-B2, today Wed 21.10.2026 14:00: A leads G1 «9-А» (lesson-l1 Mon 10:00, lesson-l2 Wed 16:00) and G2 (lesson-l3 Tue 10:00), B leads G3, C leads G4, D is a free colleague."""
from datetime import date

import pytest

from src.auth.lesson_teachers import LessonTeachers
from src.auth.ops_schemas import LessonTeacherPut
from src.models.auth import LessonTeacherAssignment, PayrollRate

from tests.b2_helpers import API, client, clock, code_of, decide, events_of, needs_pg, pg_session, requests_of, row_of  # noqa: F401  (fixtures)
from tests.ops_helpers import run_parallel
from tests.test_integration_ab_pg import money, pay, member, sheet_of
from tests.test_ops_b2_decide_pg import L, Sync, one, sync, w  # noqa: F401

pytestmark = needs_pg
LT = API + "/org/groups/{gid}/lesson-teacher"
MON = "2026-10-26"
REASON = "Болезнь у основного преподавателя"


def put(w, who, teacher, *, group=None, lesson="lesson-l1", day=MON, reason=REASON, **extra):
    """PUT lesson-teacher; ``teacher`` = a world attribute ('D') or None (take the substitution off)."""
    body = {"lessonDate": day, "lessonId": lesson, "teacherId": str(getattr(w, teacher).id) if teacher else None, "reason": reason, **extra}
    return client(w, who).put(LT.format(gid=(group or w.G1).id), json=body)


def journal(w, status="decided", **params):
    r = client(w, "admin").get(f"{API}/org/substitutions", params={"status": status, "dateFrom": "2026-09-01", "dateTo": "2026-12-31", **params})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def active(w, lesson="lesson-l1", day=MON, group=None):
    w.db.expire_all()
    return w.db.query(LessonTeacherAssignment).filter_by(group_id=(group or w.G1).id, lesson_id=lesson, lesson_date=day, cancelled_at=None).all()


def by_substitute(w, *names):
    """The records of the lesson in the order of the substitutes (the test clock is frozen: the creation time does not order them)."""
    recs = {r.substitute_id: r for r in requests_of(w)}
    return [recs[getattr(w, n).id] for n in names]


def actions(w, request):
    return [e.action for e in events_of(w, request.id)]


# ------------------------------------------------------------------------------------------------------ S1
def test_s1_a_substitution_set_from_the_sheet_without_a_request_is_in_the_decided_journal(w, sync):
    """[1] The record: origin system, the main teacher as requester, decided ``substituted`` by the administrator in the same transaction; the journal, its filters and its search find it."""
    r = put(w, "admin", "D")
    assert r.status_code == 200, r.text
    assert r.json()["teacherId"] == str(w.D.id) and r.json()["isSubstitution"] is True and r.json()["requestClosed"] is None          # no request was closed: there was none
    (rec,) = requests_of(w)
    (asg,) = active(w)
    assert (rec.status, rec.origin, rec.requester_id, rec.substitute_id, rec.assignment_id) == ("substituted", "system", w.A.id, w.D.id, asg.id)
    assert (rec.decided_by, rec.decided_via, rec.reason_code, rec.comment, rec.previous_request_id) == (w.admin.id, "lesson_teacher_put", "other", REASON, None)
    assert (rec.lesson_id, rec.lesson_date, rec.start_time, rec.end_time, rec.group_id) == ("lesson-l1", MON, "10:00", "11:30", w.G1.id)
    requested, substituted = events_of(w, rec.id)
    assert (requested.action, requested.actor_id, requested.snapshot["trigger"]) == ("requested", w.admin.id, "manager_assign")
    assert (substituted.action, substituted.actor_id, substituted.snapshot["via"], substituted.snapshot["teacherId"]) == (
        "substituted", w.admin.id, "lesson_teacher_put", [str(w.A.id), str(w.D.id)])
    (item,) = journal(w)
    assert item["id"] == str(rec.id) and item["status"] == "substituted" and item["origin"] == "system" and item["previous"] is None
    assert item["teacher"]["id"] == str(w.A.id) and item["group"]["name"] == w.G1.name and item["comment"] == REASON
    decision = item["decision"]
    assert (decision["kind"], decision["via"], decision["by"]["id"], decision["substitute"]["id"]) == ("substitute", "lesson_teacher_put", str(w.admin.id), str(w.D.id))
    assert journal(w, status="pending") == []                                  # nothing waits for a decision
    for params in ({"groupId": str(w.G1.id)}, {"teacherId": str(w.A.id)}, {"teacherId": str(w.D.id)}, {"search": w.G1.name}, {"search": w.A.last_name}, {"search": w.D.last_name}):
        assert [i["id"] for i in journal(w, **params)] == [str(rec.id)], params
    for params in ({"groupId": str(w.G3.id)}, {"teacherId": str(w.B.id)}, {"search": w.B.last_name}):
        assert journal(w, **params) == [], params


# ------------------------------------------------------------------------------------------------------ S2
def test_s2_a_waiting_request_is_decided_by_the_assignment_one_record_not_two(w, sync):
    """[2] A asked «Не смогу провести»; the administrator sets D from the sheet: THIS request is decided (``requestClosed``); no second record is made."""
    ask = one(w, "A", "L1", MON)
    r = put(w, "admin", "D")
    assert r.status_code == 200, r.text
    assert r.json()["requestClosed"] == {"id": ask["id"], "status": "substituted"}
    (rec,) = requests_of(w)
    assert (str(rec.id), rec.origin, rec.status, rec.substitute_id, rec.decided_by, rec.decided_via) == (ask["id"], "teacher", "substituted", w.D.id, w.admin.id, "lesson_teacher_put")
    assert actions(w, rec) == ["requested", "substituted"]
    (item,) = journal(w)
    assert item["id"] == ask["id"] and item["origin"] == "teacher" and item["reasonCode"] == "sick" and item["decision"]["substitute"]["id"] == str(w.D.id)


def test_s2_the_decision_of_the_page_makes_no_second_record(w, sync):
    """[2] The path of the Замены page (``decide``) runs the same assignment and leaves the one request as it is."""
    ask = one(w, "A", "L1", MON)
    assert decide(w, "admin", ask["id"], decision="substitute", teacherId=str(w.D.id)).status_code == 200
    (rec,) = requests_of(w)
    assert (rec.origin, rec.status, rec.decided_via, rec.assignment_id) == ("teacher", "substituted", "request", active(w)[0].id)
    assert actions(w, rec) == ["requested", "substituted"]


# ------------------------------------------------------------------------------------------------------ S3
def test_s3_a_replaced_by_b_the_old_record_is_the_history_the_new_one_is_the_one_in_force(w, sync):
    """[3] D, then C: two records of the lesson, each ``substituted``; the first got ``assignment_cleared`` and stays; the second points to it; ONE of them stands on an active assignment."""
    assert put(w, "admin", "D").status_code == 200
    assert put(w, "owner", "C", reason="Уточнили замену").status_code == 200
    first, second = by_substitute(w, "D", "C")
    assert (first.status, second.status) == ("substituted", "substituted")
    assert (second.origin, second.requester_id, second.previous_request_id, second.decided_by, second.comment) == ("system", w.A.id, first.id, w.owner.id, "Уточнили замену")
    assert actions(w, first) == ["requested", "substituted", "assignment_cleared"] and actions(w, second) == ["requested", "substituted"]
    (asg,) = active(w)
    assert asg.teacher_id == w.C.id
    assert [r.id for r in (first, second) if r.assignment_id == asg.id] == [second.id]                       # one record in force
    items = {i["id"]: i for i in journal(w)}
    assert set(items) == {str(first.id), str(second.id)}
    assert items[str(second.id)]["previous"] == {"id": str(first.id), "status": "substituted", "teacherName": items[str(first.id)]["teacher"]["name"],
                                                 "substituteName": items[str(first.id)]["decision"]["substitute"]["name"]}
    assert items[str(first.id)]["status"] == "substituted" and items[str(first.id)]["decision"]["substitute"]["id"] == str(w.D.id)


def test_s3_two_assignments_at_the_same_moment_leave_one_in_force(w):
    """[3] Two administrators set D and C at once: the group lock serialises them; one active assignment, two records, one of them in force."""
    def call(teacher):
        def go(svc):
            body = LessonTeacherPut(lessonDate=date(2026, 10, 26), lessonId="lesson-l1", teacherId=str(getattr(w, teacher).id), reason=REASON)
            return LessonTeachers(svc).put(str(w.org.id), w.G1.id, body, actor_id=str(w.admin.id))
        return go

    results = run_parallel(w, call("D"), call("C"))
    assert all(err is None for _r, err in results), results
    (asg,) = active(w)
    recs = requests_of(w)
    assert len(recs) == 2 and [r.id for r in recs if r.assignment_id == asg.id] == [next(r.id for r in recs if r.substitute_id == asg.teacher_id)]
    assert sum(1 for r in recs if r.previous_request_id is None) == 1 and {r.previous_request_id for r in recs} - {None} == {next(r.id for r in recs if r.previous_request_id is None)}


# ------------------------------------------------------------------------------------------------------ S4
def test_s4_taking_the_substitution_off_closes_the_record_as_removed_by_the_administrator(w, sync):
    """[4] The record is ``closed`` / ``ASSIGNMENT_CLEARED`` with the author and the time; the main teacher leads again; a new substitution later is a new record after it."""
    assert put(w, "admin", "D").status_code == 200
    (rec,) = requests_of(w)
    clock_free = put(w, "owner", None, reason="Основной преподаватель вернулся")
    assert clock_free.status_code == 200, clock_free.text
    assert clock_free.json()["teacherId"] == str(w.A.id) and clock_free.json()["isSubstitution"] is False
    (rec,) = requests_of(w)
    assert (rec.status, rec.closed_reason, rec.decided_by, rec.decided_via, rec.substitute_id) == ("closed", "ASSIGNMENT_CLEARED", w.owner.id, "lesson_teacher_put", w.D.id)
    assert rec.decided_at is not None and rec.decision_note is None
    assert actions(w, rec) == ["requested", "substituted", "assignment_cleared", "closed"]
    assert events_of(w, rec.id)[-1].actor_id == w.owner.id and events_of(w, rec.id)[-1].snapshot["closedReason"] == "ASSIGNMENT_CLEARED"
    assert active(w) == []
    (item,) = journal(w)
    decision = item["decision"]
    assert (item["status"], decision["kind"], decision["closedReason"], decision["closedReasonText"], decision["by"]["id"], decision["via"]) == (
        "closed", "closed", "ASSIGNMENT_CLEARED", "Замена снята", str(w.owner.id), "lesson_teacher_put")
    assert decision["substitute"]["id"] == str(w.D.id) and decision["at"]
    mine = client(w, "A").get(f"{API}/org/schedule/occurrences", params={"date_from": MON, "date_to": MON, "teacher_id": str(w.A.id)}).json()
    assert [(o["id"], o["isSubstitution"]) for o in mine if o["id"] == "lesson-l1"] == [("lesson-l1", False)]
    assert put(w, "admin", "C").status_code == 200                                      # a new substitution after the removal: a new record that follows the closed one
    closed, new = by_substitute(w, "D", "C")
    assert (closed.status, new.status, new.previous_request_id) == ("closed", "substituted", closed.id)
    assert sum(1 for i in journal(w) if i["status"] == "substituted") == 1


def test_s4_taking_off_a_substitution_that_was_decided_from_a_request_keeps_the_decision_of_b2(w, sync):
    """[4] The decision of a teacher's request is final (B2): the request stays ``substituted`` and gets the event; only the record the sheet made is closed."""
    ask = one(w, "A", "L1", MON)
    assert decide(w, "admin", ask["id"], decision="substitute", teacherId=str(w.D.id)).status_code == 200
    assert put(w, "admin", None).status_code == 200
    (rec,) = requests_of(w)
    assert rec.status == "substituted" and rec.closed_reason is None and actions(w, rec) == ["requested", "substituted", "assignment_cleared"]


def test_s4_a_member_removed_with_a_substitution_set_from_the_sheet_reopens_it_as_chain_6(w, sync):
    """[4] Chain 6 of B2 is as it was: the substitute leaves the centre -> his record keeps its status with ``assignment_cleared`` and the lesson waits for a new decision (a pending system record)."""
    assert put(w, "admin", "D").status_code == 200
    removed = client(w, "owner").delete(f"{API}/org/members/{w.ids('member_D')}")
    assert removed.status_code == 200, removed.text
    first, reopened = sorted(requests_of(w), key=lambda r: r.previous_request_id is not None)
    assert first.status == "substituted" and actions(w, first) == ["requested", "substituted", "assignment_cleared"]
    assert (reopened.status, reopened.origin, reopened.requester_id, reopened.previous_request_id) == ("pending", "system", w.A.id, first.id)


# ------------------------------------------------------------------------------------------------------ S5
def test_s5_the_same_assignment_twice_is_idempotent(w, sync):
    """[5] A double click: the second answer is the first one, nothing new is written - one record, two events, one assignment."""
    first = put(w, "admin", "D")
    assert first.status_code == 200
    before = (len(requests_of(w)), len(events_of(w)))
    again = put(w, "admin", "D")
    assert again.status_code == 200 and again.json()["teacherId"] == str(w.D.id) and again.json()["requestClosed"] is None
    assert (len(requests_of(w)), len(events_of(w))) == before == (1, 2)
    assert len(active(w)) == 1 and len(journal(w)) == 1


def test_s5_two_identical_assignments_at_the_same_moment_make_one_record(w):
    """[5] Two clicks in parallel: the group lock lets the second one find the assignment of the first."""
    def go(svc):
        body = LessonTeacherPut(lessonDate=date(2026, 10, 26), lessonId="lesson-l1", teacherId=str(w.D.id), reason=REASON)
        return LessonTeachers(svc).put(str(w.org.id), w.G1.id, body, actor_id=str(w.admin.id))

    results = run_parallel(w, go, go)
    assert all(err is None for _r, err in results), results
    assert len(active(w)) == 1 and len(requests_of(w)) == 1 and [e.action for e in events_of(w)] == ["requested", "substituted"]


# ------------------------------------------------------------------------------------------------------ S6
def test_s6_payroll_pays_the_substitute_one_lesson_and_the_main_teacher_none(w, clock, sync):
    """[6] Computed: 600 000 / 12 * 30 % = 15 000 per student, three students.  The substitution set from the sheet (with its journal record) is paid ONCE, to D; A has no mark on that day;
    nothing about the record is a second source of the lesson."""
    money(w, w.G1, 600_000)
    for s in (w.s1, w.s2, w.s3):
        pay(w, member(w, s, w.G1), 600_000, date(2026, 10, 1))
    w.db.add(PayrollRate(org_id=w.org.id, teacher_id=w.D.id, lesson_type="group", percent=30, valid_from=date(2026, 9, 1)))
    w.db.commit()
    sheet_a_before = sheet_of(w, w.A)
    assert put(w, "admin", "D", lesson="lesson-l2", day="2026-10-21").status_code == 200
    assert len(requests_of(w)) == 1 and requests_of(w)[0].substitute_id == w.D.id
    d, base = client(w, "D"), f"{API}/org/groups/{w.G1.id}/attendance"
    clock.set("2026-10-21 16:05")
    assert d.post(base + "/start", json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2"}).status_code == 200
    clock.set("2026-10-21 17:20")
    marks = {str(s.id): "present" for s in (w.s1, w.s2, w.s3)}
    done = d.post(base, json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2", "status": "completed", "records": marks, "expectedVersion": 1})
    assert done.status_code == 200, done.text
    sd, sa = sheet_of(w, w.D), sheet_of(w, w.A)
    d_marks = [(line["student_id"], m["date"], line["amount"]) for line in sd["lines"] for m in line["marks"]]
    assert sorted(d_marks) == sorted((str(s.id), "2026-10-21", 15_000) for s in (w.s1, w.s2, w.s3)) and sd["total_amount"] == 45_000          # one lesson, once
    assert "2026-10-21" not in {m["date"] for line in sa["lines"] for m in line["marks"]}
    assert sa["total_amount"] == sheet_a_before["total_amount"]                              # A: the same as before the substitution
    assert len(requests_of(w)) == 1                                                          # the held lesson changed no record


# ------------------------------------------------------------------------------------------------------ S7
def test_s7_the_rules_of_the_assignment_are_as_they_were_nothing_widened_and_a_refusal_writes_no_record(w, clock, sync):
    """[7] The window, a cancelled lesson, a stranger as the target: the same refusals as before and no record.  A lesson of the past inside the window and one that has started
    are still allowed - and are in the journal like any other."""
    assert put(w, "admin", "D", day="2026-08-31").status_code == 422                       # older than the window of unclosed lessons
    assert put(w, "admin", "D", day="2026-08-31").json()["detail"]["code"] == "LESSON_OUTSIDE_WINDOW"
    stranger = client(w, "admin").put(LT.format(gid=w.G1.id), json={"lessonDate": MON, "lessonId": "lesson-l1", "teacherId": str(w.foreign_admin.id), "reason": REASON})
    assert stranger.status_code == 422 and stranger.json()["detail"]["code"] == "TARGET_NOT_TEACHER"
    gone = one(w, "A", "L2", "2026-10-28")
    assert decide(w, "admin", gone["id"], decision="cancel", reason="Праздник").status_code == 200
    cancelled = put(w, "admin", "D", lesson="lesson-l2", day="2026-10-28")
    assert cancelled.status_code == 409 and cancelled.json()["detail"]["code"] == "LESSON_CANCELLED"
    assert [r.id for r in requests_of(w)] == [requests_of(w)[0].id] and requests_of(w)[0].status == "cancelled"        # only the request that was decided before
    past = put(w, "admin", "D", day="2026-10-19")                                           # held on 19.10: the row changes hands, as before
    assert past.status_code == 200 and past.json()["attendanceUpdated"] is True
    held = [r for r in requests_of(w) if r.lesson_date == "2026-10-19"]
    assert len(held) == 1 and (held[0].status, held[0].origin, held[0].substitute_id, held[0].requester_id) == ("substituted", "system", w.D.id, w.A.id)
    clock.set("2026-10-26 10:05")                                                           # Mon: the lesson has started (a row in progress)
    assert client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance/start", json={"lessonDate": MON, "lessonId": "lesson-l1"}).status_code == 200
    started = put(w, "admin", "C")
    assert started.status_code == 200 and started.json()["attendanceUpdated"] is True
    assert [r.substitute_id for r in requests_of(w) if r.lesson_date == MON] == [w.C.id]


# ------------------------------------------------------------------------------------------------------ S8
def test_s8_another_centre_and_a_teacher_are_refused_as_before_and_write_nothing(w, sync):
    """[8] A teacher (even the one of the lesson): 403; an administrator of another centre: 404 GROUP_NOT_FOUND; no record, no assignment."""
    from tests.pg_world import make_client

    assert put(w, "A", "A").status_code == 403
    assert put(w, "A", "D").status_code == 403
    assert put(w, "D", "D").status_code == 403
    foreign = make_client(w, user=w.foreign_admin, org="other")
    out = foreign.put(LT.format(gid=w.G1.id), json={"lessonDate": MON, "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": REASON})
    assert out.status_code == 404 and out.json()["detail"]["code"] == "GROUP_NOT_FOUND"
    assert requests_of(w) == [] and active(w) == [] and events_of(w) == []
    assert journal(w) == []
    assert journal(w, status="pending") == []
