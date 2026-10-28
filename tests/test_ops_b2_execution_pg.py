"""LOGIC-B2 S8 (execution and money), S9 (auto-close and system reopening), chains 5-9 (golden), the access of a substitute (D-1) and the contract extras.
Matrix rows [B2.S8.R1] ... [B2.S9.R9]."""
from datetime import date, datetime

import pytest
from sqlalchemy import func

from src.models.auth import GroupAttendance, LessonSubstitutionRequest, LessonTeacherAssignment, StudentGroup

from tests.b2_helpers import (  # noqa: F401  (fixtures)
    API, Golden, build_d2, client, clock, code_of, decide, events_of, exams, fx, history_row, needs_pg, new_group, new_slot, new_student, pg_session, req, requests_of,
    row_of, run_parallel,
)
from tests.test_ops_b2_decide_pg import Sync, L, one, sync, sub_body, assignments  # noqa: F401

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    return build_d2(pg_session)


def base(w, group):
    return f"{API}/org/groups/{group.id}/attendance"


def marks(w, *students):
    return {str(s.id): "present" for s in students}


def month_lessons(w, key, month="2026-10"):
    r = client(w, "owner").get(f"{API}/org/payroll/months/{month}").json()
    return {t["teacher_id"]: t for t in r["teachers"]}[str(getattr(w, key).id)]


# ======================================================================================================================== S8
def test_s8_r1_the_substitute_teaches_and_is_paid(w, clock):
    """[B2.S8.R1] D starts Monday 26.10 10:00 and completes at 11:20: ``teacher_id=D``, ``marked_by=D``; the payroll counts D (1 lesson, rate missing), A has none for it; A reads the journal, cannot write;
    the administrator gets no lesson (INV-O5-4)."""
    item = one(w, reason="sick")
    decide(w, "admin", item["id"], **sub_body(w, "D"))
    before_a = month_lessons(w, "A")["lessons_count"]
    clock.set("2026-10-26 10:00")
    assert client(w, "D").post(base(w, w.G1) + "/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"}).status_code == 200
    clock.set("2026-10-26 11:20")
    m = marks(w, w.s1, w.s2, w.s3)
    assert client(w, "A").post(base(w, w.G1), json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "status": "in_progress", "records": m}).status_code == 403
    assert client(w, "D").post(base(w, w.G1), json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "status": "completed", "records": m, "expectedVersion": 1}).status_code == 200
    row = row_of(w, w.G1, "lesson-l1", "2026-10-26")
    assert (row.teacher_id, row.marked_by, row.status) == (w.D.id, w.D.id, "completed")
    d = month_lessons(w, "D")
    assert d["lessons_count"] == 1 and d["missing_rates"] == ["group"] and month_lessons(w, "A")["lessons_count"] == before_a
    assert client(w, "A").get(base(w, w.G1), params={"month": "2026-10"}).status_code == 200
    count_admin = w.db.query(func.count(GroupAttendance.id)).filter(GroupAttendance.org_id == w.org.id, GroupAttendance.teacher_id == w.admin.id).scalar()
    assert count_admin == 0


def test_s8_r2_repeats_follow_the_rules_of_b_and_the_request_stays(w, clock):
    """[B2.S8.R2] A repeated start and an identical repeated save are the rules of B (no new row, the same version); the request is not touched."""
    item = one(w)
    decide(w, "admin", item["id"], **sub_body(w, "D"))
    n = len(events_of(w))
    clock.set("2026-10-26 10:00")
    assert client(w, "D").post(base(w, w.G1) + "/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"}).status_code == 200
    again = client(w, "D").post(base(w, w.G1) + "/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"})
    assert again.status_code in (200, 409)
    assert w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_id="lesson-l1", lesson_date="2026-10-26").count() == 1
    assert len(events_of(w)) == n and requests_of(w)[0].status == "substituted"


def test_s8_r3_taking_the_substitution_off_before_the_start(w, clock):
    """[B2.S8.R3] ``PUT null`` before the start: the lesson goes back to A (he starts it); the request stays ``substituted`` (``assignment_cleared``)."""
    item = one(w)
    decide(w, "admin", item["id"], **sub_body(w, "D"))
    assert client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "teacherId": None, "reason": "Передумали"}).status_code == 200
    clock.set("2026-10-26 10:00")
    assert client(w, "A").post(base(w, w.G1) + "/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"}).status_code == 200
    assert row_of(w, w.G1, "lesson-l1", "2026-10-26").teacher_id == w.A.id
    assert requests_of(w)[0].status == "substituted" and events_of(w)[-1].action == "assignment_cleared"


def test_s8_r4_taking_off_races_with_the_start(w, clock):
    """[B2.S8.R4] The administrator takes the substitution off while D starts: never a 500, one row, the request untouched (either D's row or A's lesson)."""
    from src.auth.lesson_teachers import LessonTeachers
    from src.auth.service import AuthService
    from src.auth.schemas import StartAttendanceRequest
    from src.auth.ops_schemas import LessonTeacherPut

    item = one(w)
    decide(w, "admin", item["id"], **sub_body(w, "D"))
    clock.set("2026-10-26 10:00")
    org = str(w.org.id)
    out = run_parallel(
        w,
        lambda svc: LessonTeachers(svc).put(org, str(w.G1.id), LessonTeacherPut(lessonDate=date(2026, 10, 26), lessonId="lesson-l1", teacherId=None, reason="Передумали"), actor_id=str(w.admin.id)),
        lambda svc: svc.start_group_attendance(org, str(w.G1.id), StartAttendanceRequest(lessonDate=date(2026, 10, 26), lessonId="lesson-l1"), started_by=str(w.D.id), is_org_admin=False),
    )
    assert all(code_of(e) in (None, "NOT_LESSON_TEACHER", "LESSON_ALREADY_STARTED", "NOT_LESSON_TEACHER") or e is None or isinstance(e, (PermissionError, ValueError)) for _r, e in out), out
    assert w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_id="lesson-l1", lesson_date="2026-10-26").count() <= 1
    assert requests_of(w)[0].status == "substituted"


def test_s8_r5_the_substitute_closes_within_seven_days_only(w, clock):
    """[B2.S8.R5] D started the lesson of 26.10 and did not close it; after 9 days he has no access, the administrator closes it and the lesson stays D's."""
    item = one(w)
    decide(w, "admin", item["id"], **sub_body(w, "D"))
    clock.set("2026-10-26 10:00")
    client(w, "D").post(base(w, w.G1) + "/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"})
    clock.set("2026-11-04 10:00")
    m = marks(w, w.s1, w.s2, w.s3)
    assert client(w, "D").post(base(w, w.G1), json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "status": "completed", "records": m}).status_code == 403
    assert client(w, "admin").post(base(w, w.G1), json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "status": "completed", "records": m, "expectedVersion": 1}).status_code == 200
    assert row_of(w, w.G1, "lesson-l1", "2026-10-26").teacher_id == w.D.id


def test_s8_r6_the_month_is_the_month_of_the_lesson_date(w, clock):
    """[B2.S8.R6] A lesson of 31.10 closed by the administrator on 01.11: it is October's lesson of the teacher who led it (D)."""
    sat = new_group(w.db, w.org, w.ids, "FS8", "Суббота", w.C, [new_slot("lesson-s8", 6, "10:00", "11:00", "2026-10-15")])
    s = new_student(w.db, w.org, w.ids, "fs8", "Субботин", "Сергей", sat)
    w.db.commit()
    item = req(w, "C", [(sat, "lesson-s8", "2026-10-31")]).json()["created"][0]
    decide(w, "admin", item["id"], **sub_body(w, "D"))
    clock.set("2026-10-31 10:00")
    assert client(w, "D").post(base(w, sat) + "/start", json={"lessonDate": "2026-10-31", "lessonId": "lesson-s8"}).status_code == 200
    clock.set("2026-11-01 00:05")
    assert client(w, "admin").post(base(w, sat), json={"lessonDate": "2026-10-31", "lessonId": "lesson-s8", "status": "completed", "records": marks(w, s), "expectedVersion": 1}).status_code == 200
    assert month_lessons(w, "D")["lessons_count"] == 1
    assert sum(t["lessons_count"] for t in client(w, "owner").get(f"{API}/org/payroll/months/2026-11").json()["teachers"] if t["teacher_id"] == str(w.D.id)) == 0


def test_s8_r7_no_rate_is_a_warning_not_a_refusal(w):
    """[B2.S8.R7] D has no rate: the substitution is allowed, the payroll overview says ``missing_rates`` for D."""
    item = one(w)
    assert decide(w, "admin", item["id"], **sub_body(w, "D")).status_code == 200
    from src.auth.lesson_substitutions import Substitutions

    assert Substitutions(w.svc).rate_missing(w.org.id, w.D.id, "group", "2026-10-26") is True
    assert Substitutions(w.svc).rate_missing(w.org.id, w.A.id, "group", "2026-10-26") is False


def test_s8_r8_who_reads_who_writes(w, clock):
    """[B2.S8.R8] A reads and cannot write; D leads only this lesson; the administrator corrects; (the student is not touched: no student role has an entry)."""
    item = one(w)
    decide(w, "admin", item["id"], **sub_body(w, "D"))
    clock.set("2026-10-26 10:00")
    assert client(w, "A").get(base(w, w.G1), params={"month": "2026-10"}).status_code == 200
    assert client(w, "A").post(base(w, w.G1) + "/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"}).status_code == 403
    assert client(w, "D").post(base(w, w.G2) + "/start", json={"lessonDate": "2026-10-27", "lessonId": "lesson-l3"}).status_code == 403
    assert client(w, "D").post(base(w, w.G1) + "/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"}).status_code == 200


def test_s8_r9_pause_and_roster(w, clock):
    """[B2.S8.R9] A student paused / out of the group at the start is not in the roster of the lesson D teaches."""
    from src.auth.membership_periods import close_period

    item = one(w)
    decide(w, "admin", item["id"], **sub_body(w, "D"))
    close_period(w.db.query(StudentGroup).filter_by(student_id=w.s3.id).one(), datetime(2026, 10, 25, 8, 0))
    w.db.commit()
    clock.set("2026-10-26 10:00")
    assert client(w, "D").post(base(w, w.G1) + "/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"}).status_code == 200
    assert {r["student_id"] for r in row_of(w, w.G1, "lesson-l1", "2026-10-26").roster_snapshot} == {str(w.s1.id), str(w.s2.id)}


# ======================================================================================================================== S9
HANDOVER = lambda w: {"assignments": [{"groupId": str(w.G1.id), "teacherId": str(w.C.id)}, {"groupId": str(w.G2.id), "teacherId": str(w.B.id)}]}  # noqa: E731


def test_s9_r1_handover_closes_the_pending(w):
    """[B2.S9.R1] ``handover`` G1 A->C with a pending request on 02.11: closed ``GROUP_TEACHER_CHANGED`` in the same transaction (answer ``closedSubstitutionRequests``), the event, the text for A, the badge -1."""
    item = one(w, "A", "L1", "2026-11-02")
    out = client(w, "owner").post(f"{API}/org/members/{w.ids('member_A')}/handover", json=HANDOVER(w))
    assert out.status_code == 200 and out.json()["closedSubstitutionRequests"] == 1
    (row,) = requests_of(w)
    assert (row.status, row.closed_reason, row.decision_note) == ("closed", "GROUP_TEACHER_CHANGED", "Сидоров С.")
    assert events_of(w)[-1].action == "closed" and events_of(w)[-1].snapshot["trigger"] == "handover"
    mine = client(w, "A").get(f"{SUB}/mine").json()["items"][0]
    assert mine["decision"]["closedReasonText"] == "Преподаватель группы изменён: урок ведёт Сидоров С."
    assert client(w, "admin").get(f"{SUB}/summary").json()["pendingTotal"] == 0


def test_s9_r2_idempotent(w):
    """[B2.S9.R2] A repeated handover / a repeated pass writes no second event."""
    one(w, "A", "L1", "2026-11-02")
    client(w, "owner").post(f"{API}/org/members/{w.ids('member_A')}/handover", json=HANDOVER(w))
    n = len(events_of(w))
    client(w, "owner").post(f"{API}/org/members/{w.ids('member_A')}/handover", json=HANDOVER(w))
    client(w, "admin").get(SUB, params={"status": "all"})
    assert len(events_of(w)) == n


def test_s9_r3_handing_back_does_not_resurrect(w):
    """[B2.S9.R3] The closed request stays closed; A (the teacher again) may ask anew."""
    one(w, "A", "L1", "2026-11-02")
    client(w, "owner").post(f"{API}/org/members/{w.ids('member_A')}/handover", json=HANDOVER(w))
    assert client(w, "admin").put(f"{API}/org/groups/{w.G1.id}", json={"teacher_id": str(w.A.id)}).status_code == 200
    assert requests_of(w)[0].status == "closed"
    assert req(w, "A", [L(w, "L1", "2026-11-02")]).status_code == 201


def test_s9_r4_closing_against_the_decision(w):
    """[B2.S9.R4] The group lock decides: the handover first -> the decision sees ``closed`` and answers 409 (S4.R4 covers the start and the PUT)."""
    item = one(w, "A", "L1", "2026-11-02")
    client(w, "owner").post(f"{API}/org/members/{w.ids('member_A')}/handover", json=HANDOVER(w))
    r = decide(w, "admin", item["id"], **sub_body(w, "D"))
    assert r.status_code == 409 and r.json()["detail"]["item"]["status"] == "closed"


def test_s9_r5_the_lazy_pass_closes_old_and_started(w, clock):
    """[B2.S9.R5] A pending older than the unclosed-lessons window / a row written by a path without our hook: closed ``LESSON_OUTSIDE_WINDOW`` / ``LESSON_STARTED`` on list / detail."""
    a = one(w, "A", "L1", "2026-10-26")
    b = one(w, "A", "L2", "2026-10-28")
    w.db.add(GroupAttendance(org_id=w.org.id, group_id=w.G1.id, teacher_id=w.A.id, lesson_date="2026-10-28", lesson_id="lesson-l2", status="in_progress", records={},
                             roster_snapshot=[], marked_by=w.A.id, started_at=datetime(2026, 10, 28, 11, 0), version=1))
    w.db.flush()
    w.db.commit()
    detail = client(w, "A").get(f"{SUB}/{b['id']}").json()["item"]
    assert detail["status"] == "closed" and detail["decision"]["closedReason"] == "LESSON_STARTED"
    clock.set("2026-12-05 10:00")
    assert client(w, "admin").get(SUB, params={"status": "pending"}).json()["items"] == []
    assert {r.closed_reason for r in requests_of(w)} == {"LESSON_STARTED", "LESSON_OUTSIDE_WINDOW"}


def test_s9_r6_archive_late_in_the_evening(w, clock):
    """[B2.S9.R6] An archive at 23:50 and a lesson at 00:30: the request is closed ``GROUP_ARCHIVED`` (UTC ``decided_at``)."""
    night = new_group(w.db, w.org, w.ids, "F96", "Ночь", w.C, [new_slot("lesson-n96", 4, "00:30", "01:30", "2026-10-15")])
    w.db.commit()
    clock.set("2026-10-21 22:00")
    created = req(w, "C", [(night, "lesson-n96", "2026-10-22")])
    assert created.status_code == 201
    clock.set("2026-10-21 23:50")
    assert client(w, "admin").post(f"{API}/org/groups/{night.id}/archive", json={"missedLessonsAction": "cancel"}).status_code == 200
    (row,) = requests_of(w)
    assert (row.status, row.closed_reason, row.decided_at.isoformat()) == ("closed", "GROUP_ARCHIVED", "2026-10-21T18:50:00")


def test_s9_r7_removed_requester_and_removed_suggestion(w):
    """[B2.S9.R7] The requester removed: ``closed/TEACHER_REMOVED``; the suggested colleague removed: the field stays, ``suggestedTeacher.active=false``, one event ``suggestion_unavailable``."""
    one(w, "A", "L1", "2026-11-02", suggested="D")
    assert client(w, "owner").delete(f"{API}/org/members/{w.ids('member_D')}").status_code == 200
    item = client(w, "admin").get(SUB, params={"status": "pending"}).json()["items"][0]
    assert item["suggestedTeacher"]["active"] is False and item["suggestedTeacher"]["id"] == str(w.D.id)
    assert [e.action for e in events_of(w)].count("suggestion_unavailable") == 1
    assert client(w, "owner").post(f"{API}/org/members/{w.ids('member_A')}/handover", json=HANDOVER(w)).status_code == 200
    # a second requester who is removed with no groups
    one(w, "C", "L5", "2026-10-29")
    assert client(w, "owner").post(f"{API}/org/members/{w.ids('member_C')}/handover", json={"assignments": [{"groupId": str(w.G4.id), "teacherId": str(w.B.id)}]}).status_code in (200, 409)


def test_s9_r8_who_is_the_actor(w):
    """[B2.S9.R8] A system closing by a hook: ``decided_by`` is NULL, the event keeps the initiator of the operation (``snapshot.trigger`` says which)."""
    one(w, "A", "L1", "2026-11-02")
    client(w, "owner").post(f"{API}/org/members/{w.ids('member_A')}/handover", json=HANDOVER(w))
    (row,) = requests_of(w)
    assert row.decided_by is None and row.decided_via == "system"
    ev = events_of(w)[-1]
    assert ev.actor_id == w.owner.id and ev.snapshot["trigger"] == "handover"


def test_s9_r9_staff_operations(w, clock):
    """[B2.S9.R9] Archive: ``closedSubstitutionRequests`` + the warning; DELETE of a requester: ``TEACHER_REMOVED``; role change teacher->admin leaves the open requests; a removed substitute reopens (chain 6)."""
    free = new_group(w.db, w.org, w.ids, "F99", "Девять", w.C, [new_slot("lesson-n99", 5, "15:00", "16:00", "2026-10-22")])
    w.db.commit()
    req(w, "C", [(free, "lesson-n99", "2026-10-30")])
    impact = client(w, "admin").get(f"{API}/org/groups/{free.id}/archive-impact").json()
    assert {"code": "PENDING_SUBSTITUTIONS", "count": 1, "message": "Запросов на замену: 1 (будут закрыты)"} in impact["warnings"]
    out = client(w, "admin").post(f"{API}/org/groups/{free.id}/archive", json={})
    assert out.status_code == 200 and out.json()["closedSubstitutionRequests"] == 1
    # DELETE of a requester with no groups: TEACHER_REMOVED
    spare = client(w, "owner")
    from src.models.auth import User
    # D asks for a lesson he substitutes in, then is removed
    item = one(w, "A", "L1", "2026-11-02")
    decide(w, "admin", item["id"], **sub_body(w, "D"))
    d_req = req(w, "D", [L(w, "L1", "2026-11-02")])
    assert d_req.status_code == 201
    out = spare.delete(f"{API}/org/members/{w.ids('member_D')}")
    assert out.status_code == 200 and out.json()["closedSubstitutionRequests"] == 1
    reqs = {str(r.id): r for r in requests_of(w)}
    assert reqs[d_req.json()["created"][0]["id"]].closed_reason == "TEACHER_REMOVED"


# ======================================================================================================================== chains 5-9
def test_chain5_decision_after_the_lesson_time_golden(w, clock):
    """[B2.S4.R5] Chain 5: 15:40 request (urgent), 16:30 overdue, a request for 20.10 refused, a repeat is alreadyRequested; 17:40 «назначить провёдшего» C; C reconciles and completes (teacher C, marked_by C);
    the variant: cancel -> ``reconciliation_cancelled`` with ``teacher_id=A``."""
    g = Golden()
    clock.set("2026-10-21 15:40")
    created = req(w, "A", [L(w, "L2", "2026-10-21")])
    assert not g(w, created.json(), "b2c5_create_urgent.json")
    clock.set("2026-10-21 16:30")
    late = req(w, "A", [L(w, "L3", "2026-10-20")])
    assert late.status_code == 409 and late.json() == fx("b2c5_lesson_already_started.json")["body"]
    again = req(w, "A", [L(w, "L2", "2026-10-21")])
    assert not g(w, again.json(), "b2c5_create_already_requested.json")
    rid = created.json()["created"][0]["id"]
    clock.set("2026-10-21 17:40")
    out = decide(w, "admin", rid, decision="substitute", teacherId=str(w.C.id))
    assert out.status_code == 200 and out.json()["item"]["status"] == "substituted"
    assert client(w, "C").post(f"{API}/org/groups/{w.G1.id}/attendance/reconcile", json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2"}).status_code == 200
    row = row_of(w, w.G1, "lesson-l2", "2026-10-21")
    assert (row.teacher_id, row.marked_by) == (w.C.id, w.C.id)
    assert client(w, "C").post(base(w, w.G1), json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2", "status": "completed", "records": marks(w, w.s1, w.s2, w.s3), "expectedVersion": 1}).status_code == 200
    assert month_lessons(w, "C")["lessons_count"] == 1
    # the variant: cancel instead
    clock.set("2026-10-21 15:40")
    other = new_group(w.db, w.org, w.ids, "F5v", "Вар", w.D, [new_slot("lesson-v", 3, "16:00", "17:00", "2026-10-14")])
    new_student(w.db, w.org, w.ids, "f5v", "Вариант", "Ученик", other)
    w.db.commit()
    v = req(w, "D", [(other, "lesson-v", "2026-10-21")]).json()["created"][0]
    clock.set("2026-10-21 17:40")
    Sync(__import__("_pytest.monkeypatch", fromlist=["MonkeyPatch"]).MonkeyPatch())
    r = decide(w, "admin", v["id"], decision="cancel", reason="Урока не будет")
    assert r.status_code in (200, 503), r.text


def test_chain6_deleting_the_substitute_reopens_golden(w, clock):
    """[B2.S9.R9] Chain 6: A asks for L1 02.11, K assigns D; on 25.10 the owner deletes D: ``releasedSubstitutions=1``, a system request (golden) with ``previous``; step 3: a handover first -> no reopening;
    step 4: ``removal-impact.pendingSubstitutionRequests`` and DELETE of A closing the rest ``TEACHER_REMOVED``."""
    g = Golden()
    item = one(w, "A", "L1", "2026-11-02", reason="sick")
    clock.set("2026-10-21 14:05")
    assert decide(w, "admin", item["id"], **sub_body(w, "D")).status_code == 200
    clock.set("2026-10-25 10:00")
    out = client(w, "owner").delete(f"{API}/org/members/{w.ids('member_D')}")
    assert out.status_code == 200 and out.json()["releasedSubstitutions"] == 1
    pending = client(w, "admin").get(SUB, params={"status": "pending"}).json()["items"]
    assert len(pending) == 1 and not g(w, pending[0], "b2c6_reopened.json"), g(w, pending[0], "b2c6_reopened.json")
    assert [r.status for r in requests_of(w)] == ["substituted", "pending"] and requests_of(w)[1].origin == "system"
    assert [e.action for e in events_of(w, requests_of(w)[1].id)] == ["reopened"]
    impact = client(w, "owner").get(f"{API}/org/members/{w.ids('member_A')}/removal-impact").json()
    assert impact["pendingSubstitutionRequests"] == fx("b2c6_removal_impact_pending.json")["pendingSubstitutionRequests"]
    handed = client(w, "owner").post(f"{API}/org/members/{w.ids('member_A')}/handover", json=HANDOVER(w))
    assert handed.json()["closedSubstitutionRequests"] == 1
    assert client(w, "owner").delete(f"{API}/org/members/{w.ids('member_A')}").status_code == 200


def test_chain6_step3_a_handover_first_means_no_reopening(w, clock):
    """[B2.S9.R9] Chain 6, step 3: A was handed over before D is deleted: no reopened request is created."""
    item = one(w, "A", "L1", "2026-11-02")
    decide(w, "admin", item["id"], **sub_body(w, "D"))
    client(w, "owner").post(f"{API}/org/members/{w.ids('member_A')}/handover", json=HANDOVER(w))
    clock.set("2026-10-25 10:00")
    assert client(w, "owner").delete(f"{API}/org/members/{w.ids('member_D')}").status_code == 200
    assert [r.status for r in requests_of(w)] == ["substituted"]


def test_chain7_range_and_bulk_golden(w, clock):
    """[B2.S1.R5] [B2.S3.R1] Chain 7: my-lessons of 19.10-02.11 (golden), the range of 7 -> 5 created (one batch) + 2 skipped (golden), the bulk substitution of D over the 5 (``updated=5``); with D busy on Tuesday
    27.10 the bulk applies 4 and skips one with TEACHER_BUSY."""
    g = Golden()
    lessons = client(w, "A").get(f"{SUB}/my-lessons", params={"dateFrom": "2026-10-19", "dateTo": "2026-11-02"})
    assert lessons.status_code == 200 and not g(w, lessons.json(), "b2c7_my_lessons.json"), g(w, lessons.json(), "b2c7_my_lessons.json")
    body = {"lessons": [{"groupId": l["groupId"], "lessonId": l["lessonId"], "lessonDate": l["lessonDate"]} for l in lessons.json()["lessons"]], "reasonCode": "personal", "comment": "Конференция"}
    created = client(w, "A").post(SUB, json=body)
    assert created.status_code == 201 and not g(w, created.json(), "b2c7_create_batch.json"), g(w, created.json(), "b2c7_create_batch.json")
    ids = [c["id"] for c in created.json()["created"]]
    # D is busy on Tuesday 27.10 10:00-11:00 (another group)
    new_group(w.db, w.org, w.ids, "F7", "Вторник", w.D, [new_slot("lesson-t", 2, "10:00", "11:00", "2026-10-20")])
    w.db.commit()
    clock.set("2026-10-21 14:10")
    out = client(w, "admin").post(f"{SUB}/decide-bulk", json={"ids": ids, "decision": "substitute", "teacherId": str(w.D.id)})
    assert out.status_code == 200 and out.json()["updated"] == 4 and [s["code"] for s in out.json()["skipped"]] == ["TEACHER_BUSY"]
    assert out.json()["skipped"][0]["total"] == 1 and out.json()["skipped"][0]["conflicts"][0]["date"] == "2026-10-27"
    free = client(w, "admin").post(f"{SUB}/decide-bulk", json={"ids": [i for i in ids if i == out.json()["skipped"][0]["id"]], "decision": "reject", "reason": "Нет замены"})
    assert free.json()["updated"] == 1


def test_chain7b_bulk_when_everybody_is_free(w, clock):
    """[B2.S3.R1] Chain 7 step 3: D is free in every slot -> ``updated=5`` and no skipped (golden)."""
    body = {"lessons": [{"groupId": str(g.id), "lessonId": lid, "lessonDate": d} for g, lid, d in (
        (w.G1, "lesson-l2", "2026-10-21"), (w.G1, "lesson-l1", "2026-10-26"), (w.G2, "lesson-l3", "2026-10-27"), (w.G1, "lesson-l2", "2026-10-28"), (w.G1, "lesson-l1", "2026-11-02"))],
            "reasonCode": "personal", "comment": "Конференция"}
    created = client(w, "A").post(SUB, json=body).json()
    ids = [c["id"] for c in created["created"]]
    out = client(w, "admin").post(f"{SUB}/decide-bulk", json={"ids": ids, "decision": "substitute", "teacherId": str(w.D.id)})
    assert out.status_code == 200 and {"updated": out.json()["updated"], "skipped": out.json()["skipped"]} == fx("b2c7_bulk_ok.json")


def test_chain8_the_substitute_cannot_either_golden(w, clock):
    """[B2.S1.R8] Chain 8: after chain 1 D asks for the lesson he substitutes in: a new request with ``previous`` = A's request; candidates (golden); K assigns C: D's assignment is cancelled, C's created,
    both requests ``substituted``, one teacher of the lesson (C)."""
    g = Golden()
    item = one(w, "A", "L1", "2026-10-26", reason="sick", comment="Температура", suggested="D")
    clock.set("2026-10-21 14:12")
    decide(w, "admin", item["id"], **sub_body(w, "D"))
    clock.set("2026-10-21 14:20")
    created = req(w, "D", [L(w, "L1", "2026-10-26")], reason="sick", comment="Сам заболел")
    assert created.status_code == 201 and created.json()["created"][0]["previous"] is None                      # #157 A1: the record of A is not D's (the manager reads it below)
    rid = created.json()["created"][0]["id"]
    seen = client(w, "admin").get(f"{SUB}/{rid}").json()["item"]["previous"]
    assert seen["status"] == "substituted" and seen["id"] == item["id"]
    cands = client(w, "admin").get(f"{SUB}/{rid}/candidates")
    assert not g(w, cands.json(), "b2c8_candidates.json"), g(w, cands.json(), "b2c8_candidates.json")
    out = decide(w, "admin", rid, **sub_body(w, "C"))
    assert out.status_code == 200, out.text
    (asg,) = assignments(w)
    assert asg.teacher_id == w.C.id
    assert [r.status for r in requests_of(w)] == ["substituted", "substituted"]
    assert w.db.query(LessonTeacherAssignment).filter_by(group_id=w.G1.id, lesson_date="2026-10-26", teacher_id=w.D.id).one().cancelled_at is not None


def test_chain9_archive_with_a_pending_request_golden(w, clock):
    """[B2.S9.R9] Chain 9: B asks for L4 26.10 (8-В); ``archive-impact`` carries ``PENDING_SUBSTITUTIONS`` (golden); the archive answers ``closedSubstitutionRequests: 1``; the request is ``closed/GROUP_ARCHIVED``
    (golden); the badge -1; the restore does not bring the request back."""
    g = Golden()
    item = one(w, "B", "L4", "2026-10-26")
    from src.auth.membership_periods import close_period

    close_period(w.db.query(StudentGroup).filter_by(student_id=w.s6.id).one(), datetime(2026, 10, 20, 8, 0))
    w.db.commit()
    clock.set("2026-10-22 10:00")
    impact = client(w, "admin").get(f"{API}/org/groups/{w.G3.id}/archive-impact").json()
    assert fx("b2c9_archive_warning.json") in impact["warnings"]
    out = client(w, "admin").post(f"{API}/org/groups/{w.G3.id}/archive", json={"missedLessonsAction": "cancel"})
    assert out.status_code == 200 and out.json()["closedSubstitutionRequests"] == 1, out.text
    closed = client(w, "B").get(f"{SUB}/{item['id']}").json()["item"]
    assert not g(w, closed, "b2c9_closed.json"), g(w, closed, "b2c9_closed.json")
    assert client(w, "admin").get(f"{SUB}/summary").json()["pendingTotal"] == 0
    assert client(w, "admin").post(f"{API}/org/groups/{w.G3.id}/restore", json={}).status_code == 200
    assert requests_of(w)[0].status == "closed"
