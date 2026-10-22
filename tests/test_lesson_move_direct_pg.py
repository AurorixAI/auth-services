"""#124: the administrator moves a future lesson from the lesson sheet WITHOUT a teacher's request (matrix M1..M15) + the precheck of «Отменить урок» (X1..X5).
Spec: artifacts/partner_doc_check/logicB2_spec.md, «Дополнение 22.10»."""
from datetime import date

import pytest

from src.auth.lesson_substitutions import Substitutions, moved_one_off_keys
from src.models.auth import GroupAttendance, GroupAttendanceAudit, GroupScheduleOneOffLesson, LessonTeacherAssignment

from tests.b2_helpers import (  # noqa: F401  (fixtures)
    API, build_d2, client, clock, code_of, decide, events_of, exams, needs_pg, pg_session, req, requests_of, row_of, run_parallel,
)
from tests.test_ops_b2_decide_pg import L, Sync, one, sync, w  # noqa: F401

pytestmark = needs_pg
BODY = dict(newDate="2026-10-29", startTime="16:00", endTime="17:30", reason="Перенос по просьбе центра")


def move(w, who="admin", key="L2", day="2026-10-28", group=None, **kw):
    g, lesson, _ = L(w, key, day)
    body = {"lessonId": lesson, "lessonDate": day, **{**BODY, **kw}}
    return client(w, who).post(f"{API}/org/groups/{(group or g).id}/lesson-move", json=body)


def check(w, who="admin", key="L2", day="2026-10-28", **q):
    g, lesson, _ = L(w, key, day)
    params = {"lessonId": lesson, "lessonDate": day, "newDate": "2026-10-29", "startTime": "16:00", "endTime": "17:30", **q}
    return client(w, who).get(f"{API}/org/groups/{g.id}/lesson-move-check", params=params)


def nothing_written(w, key="L2", day="2026-10-28"):
    g, lesson, _ = L(w, key, day)
    assert requests_of(w) == [] and row_of(w, g, lesson, day) is None
    assert w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=g.id).count() == 0


# ------------------------------------------------------------------------------------------------------ M
def test_m1_a_move_without_a_request_follows_the_decision_path(w, sync):
    """[M1] The record, the cancelled original, the one-off lesson, the events, the label of the student, the neutrality of package A."""
    r = move(w)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["item"]["status"] == "rescheduled" and out["item"]["origin"] == "system"
    assert out["result"]["kind"] == "reschedule" and out["result"]["cancelledVersion"] == 1
    new = out["result"]["newLesson"]
    assert (new["date"], new["startTime"], new["endTime"]) == ("2026-10-29", "16:00", "17:30") and new["id"].startswith("one-off-")
    row = row_of(w, w.G1, "lesson-l2", "2026-10-28")
    assert (row.status, row.version, row.teacher_id, row.marked_by) == ("cancelled", 1, w.A.id, w.admin.id)
    assert row.cancellation_reason == "Перенесён на 29.10, 16:00–17:30: Перенос по просьбе центра"
    (rec,) = requests_of(w)
    assert (rec.status, rec.origin, rec.requester_id, rec.decided_by, rec.comment, rec.moved_to_lesson_id) == (
        "rescheduled", "system", w.A.id, w.admin.id, BODY["reason"], new["id"])
    assert [e.action for e in events_of(w, rec.id)] == ["requested", "rescheduled"]
    assert events_of(w, rec.id)[0].snapshot["trigger"] == "manager_move"
    audit = w.db.query(GroupAttendanceAudit).filter_by(attendance_id=row.id).one()
    assert audit.action == "cancelled" and audit.details["substitution_request_id"] == str(rec.id)
    assert (str(w.G1.id), new["id"]) in moved_one_off_keys(w.db, [w.G1.id])
    notices = client(w, "admin").get(f"{API}/org/students/{w.s1.id}/attendance", params={"month": "2026-10"}).json()["notices"]
    assert any(n["kind"] == "rescheduled" and n["date"] == "2026-10-28" and n["movedTo"]["date"] == "2026-10-29" for n in notices)
    occ = client(w, "admin").get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-10-28", "date_to": "2026-10-28"}).json()
    assert next(o for o in occ if o["id"] == "lesson-l2")["substitutionOutcome"]["kind"] == "rescheduled"
    assert [x for x in requests_of(w, "pending")] == []


def test_m2_an_open_request_of_the_lesson_is_decided_by_the_same_path(w, sync):
    """[M2] A teacher's pending request on the lesson: the move decides THAT request (no system record)."""
    item = one(w, "A", "L2", "2026-10-28")
    r = move(w)
    assert r.status_code == 200 and r.json()["item"]["id"] == item["id"] and r.json()["item"]["status"] == "rescheduled"
    (rec,) = requests_of(w)
    assert rec.origin == "teacher" and rec.status == "rescheduled" and rec.decided_via == "request"
    assert [e.action for e in events_of(w, rec.id)] == ["requested", "rescheduled"]


def test_m3_a_repeat_is_the_same_answer_and_no_second_lesson(w, sync):
    """[M3] Double click / retry: 200, the same item, one one-off lesson; the same lesson with another slot: 409 with the winner."""
    first = move(w)
    again = move(w)
    assert again.status_code == 200 and again.json()["item"] == first.json()["item"] and again.json()["result"]["newLesson"] == first.json()["result"]["newLesson"]
    assert w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=w.G1.id).count() == 1 and len(requests_of(w)) == 1
    other = move(w, newDate="2026-10-30")
    assert other.status_code == 409 and other.json()["detail"]["code"] == "SUBSTITUTION_ALREADY_DECIDED"
    assert other.json()["detail"]["item"]["decision"]["movedTo"]["date"] == "2026-10-29"
    again_owner = move(w, "owner")
    assert again_owner.status_code == 409          # another administrator repeating is not "the same decision"


def test_m4_two_administrators_at_once_and_a_lesson_cancelled_meanwhile(w, sync):
    """[M4] One winner, the loser 409 SUBSTITUTION_ALREADY_DECIDED; a lesson cancelled by a decision: 409 LESSON_CANCELLED."""
    from src.auth.substitution_schemas import LessonMoveRequest

    org = str(w.org.id)

    def go(who, role, day):
        return lambda svc: Substitutions(svc).move_lesson(org, str(getattr(w, who).id), role, str(w.G1.id), LessonMoveRequest(
            lessonId="lesson-l2", lessonDate=date(2026, 10, 28), newDate=day, startTime="16:00", endTime="17:30", reason="Перенос"))

    out = run_parallel(w, go("admin", "admin", date(2026, 10, 29)), go("owner", "owner", date(2026, 10, 30)))
    assert sorted(code_of(e) or "ok" for _r, e in out) == ["SUBSTITUTION_ALREADY_DECIDED", "ok"], out
    assert w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=w.G1.id).count() == 1 and len(requests_of(w)) == 1
    item = one(w, "A", "L1", "2026-11-02")
    assert decide(w, "admin", item["id"], decision="cancel", reason="Нет замены").status_code == 200
    r = move(w, key="L1", day="2026-11-02")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "LESSON_CANCELLED"


def test_m5_the_expected_slot_guards_against_a_stale_sheet(w, sync):
    """[M5] The sheet showed another time than the schedule has now: 409 LESSON_CHANGED with the current time, nothing written."""
    r = move(w, expectedStartTime="11:00", expectedEndTime="12:00")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "LESSON_CHANGED" and r.json()["detail"]["current"] == {"startTime": "16:00", "endTime": "17:30"}
    nothing_written(w)
    ok = move(w, expectedStartTime="16:00", expectedEndTime="17:30")
    assert ok.status_code == 200, ok.text


def test_m6_a_started_or_past_lesson_cannot_be_moved(w, clock, sync):
    """[M6] The planned start has passed / the journal has a row: 409 LESSON_ALREADY_STARTED."""
    clock.set("2026-10-26 10:30")
    r = move(w, key="L1", day="2026-10-26", newDate="2026-10-27", startTime="10:00", endTime="11:30")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "LESSON_ALREADY_STARTED"
    clock.set("2026-10-21 14:00")
    w.db.add(GroupAttendance(org_id=w.org.id, group_id=w.G1.id, teacher_id=w.A.id, lesson_date="2026-10-28", lesson_id="lesson-l2", status="in_progress",
                             records={}, roster_snapshot=[], marked_by=w.A.id, version=1))
    w.db.commit()
    again = move(w)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "LESSON_ALREADY_STARTED"
    assert requests_of(w) == []


def test_m7_the_new_slot_rules(w, sync):
    """[M7] In the past / too far / the same slot."""
    assert move(w, newDate="2026-10-20").json()["detail"]["code"] == "MOVE_IN_PAST"
    assert move(w, newDate="2026-12-31").json()["detail"]["code"] == "MOVE_TOO_FAR"
    assert move(w, newDate="2026-10-28", startTime="16:00", endTime="17:30").json()["detail"]["code"] == "MOVE_SAME_SLOT"
    nothing_written(w)


def test_m8_busy_teacher_or_group_writes_nothing(w, sync):
    """[M8] A teaches 7-Б at Tue 27.10 10:00-11:30 (chain 2): TEACHER_BUSY; a one-off lesson of the group at the target: GROUP_BUSY; the record is rolled back too."""
    busy = move(w, newDate="2026-10-27", startTime="10:00", endTime="11:30")
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "TEACHER_BUSY"
    nothing_written(w)
    w.db.add(GroupScheduleOneOffLesson(id=f"one-off-ex-{w.salt}", org_id=w.org.id, group_id=w.G1.id, lesson_date="2026-11-03",
                                       payload={"dayOfWeek": 2, "startTime": "12:00", "endTime": "13:00", "subject": "Математика", "room": None, "format": "offline", "color": "blue"},
                                       created_by=w.admin.id))
    w.db.commit()
    gb = move(w, newDate="2026-11-03", startTime="12:30", endTime="13:30")
    assert gb.status_code == 409 and gb.json()["detail"]["code"] == "GROUP_BUSY" and gb.json()["detail"]["total"] == 1
    assert requests_of(w) == [] and row_of(w, w.G1, "lesson-l2", "2026-10-28") is None


def test_m9_exam_service_down_or_a_live_exam_writes_nothing(w, sync):
    """[M9] Fail-closed: 503 / 409 and not even the system record stays."""
    sync.mode = "down"
    r = move(w)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "EXAM_SERVICE_UNAVAILABLE"
    nothing_written(w)
    sync.mode = "live"
    r = move(w)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "LESSON_HAS_LIVE_EXAM"
    nothing_written(w)
    sync.mode = "ok"
    assert move(w).status_code == 200
    assert [c[0] for c in sync.calls if c[0] == "release"] == ["release"]


def test_m10_reason_and_fields(w, sync):
    """[M10] Reason < 3 / > 500; no new date or time."""
    assert move(w, reason="ab").json()["detail"]["code"] == "REASON_REQUIRED"
    assert move(w, reason=" ").json()["detail"]["code"] == "REASON_REQUIRED"
    assert move(w, reason="x" * 501).status_code == 422
    assert move(w, newDate=None).json()["detail"]["code"] == "MOVE_FIELDS_REQUIRED"
    assert move(w, startTime=None).json()["detail"]["code"] == "MOVE_FIELDS_REQUIRED"
    nothing_written(w)


def test_m11_archived_group_unknown_lesson_and_cancelled_lesson(w, sync):
    """[M11]"""
    ghost = move(w, key="L2", day="2026-10-28", group=w.G2)
    assert ghost.status_code == 404 and ghost.json()["detail"]["code"] == "LESSON_NOT_FOUND"
    unknown = client(w, "admin").post(f"{API}/org/groups/{w.G1.id}/lesson-move", json={"lessonId": "nope", "lessonDate": "2026-10-28", **BODY})
    assert unknown.status_code == 404 and unknown.json()["detail"]["code"] == "LESSON_NOT_FOUND"
    nobody = client(w, "admin").post(f"{API}/org/groups/00000000-0000-0000-0000-000000000000/lesson-move", json={"lessonId": "lesson-l2", "lessonDate": "2026-10-28", **BODY})
    assert nobody.status_code == 404 and nobody.json()["detail"]["code"] == "GROUP_NOT_FOUND"
    w.G1.status = "archived"
    w.db.commit()
    r = move(w)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "GROUP_ARCHIVED"
    nothing_written(w)


def test_m12_a_lesson_without_a_teacher(w, sync):
    """[M12]"""
    w.G1.teacher_id = None
    w.db.commit()
    r = move(w)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "LESSON_HAS_NO_TEACHER"
    nothing_written(w)


def test_m13_a_lesson_with_a_substitute_moves_and_the_substitution_is_released(w, sync):
    """[M13] The system record is on the teacher of the lesson (the substitute); the assignment is cancelled with the original."""
    item = one(w, "A", "L2", "2026-10-28")
    assert decide(w, "admin", item["id"], decision="substitute", teacherId=str(w.B.id)).status_code == 200
    w.db.expire_all()
    assert w.db.query(LessonTeacherAssignment).filter_by(group_id=w.G1.id, lesson_id="lesson-l2", lesson_date="2026-10-28", cancelled_at=None).count() == 1
    r = move(w)
    assert r.status_code == 200, r.text
    w.db.expire_all()
    assert w.db.query(LessonTeacherAssignment).filter_by(group_id=w.G1.id, lesson_id="lesson-l2", lesson_date="2026-10-28", cancelled_at=None).count() == 0
    (last,) = [x for x in requests_of(w) if x.origin == "system"]
    assert last.origin == "system" and last.requester_id == w.B.id and last.status == "rescheduled" and str(last.previous_request_id) == item["id"]


def test_m14_only_the_owner_and_the_admin(w, sync):
    """[M14]"""
    for who in ("A", "B", "D"):
        assert move(w, who).status_code == 403, who
        assert check(w, who).status_code == 403, who
    assert move(w, "owner").status_code == 200
    nothing = [r for r in requests_of(w) if r.decided_by not in (w.owner.id,)]
    assert nothing == []


def test_m15_over_the_month_border(w, sync):
    """[M15]"""
    r = move(w, key="L1", day="2026-10-26", newDate="2026-11-03", startTime="14:00", endTime="15:00")
    assert r.status_code == 200, r.text
    assert r.json()["result"]["newLesson"]["date"] == "2026-11-03" and row_of(w, w.G1, "lesson-l1", "2026-10-26").status == "cancelled"


def test_move_check_by_lesson_equals_the_check_by_request(w, sync):
    """The live check of the sheet = the check of the request (same conflicts, same shape)."""
    item = one(w, "A", "L2", "2026-10-28")
    for params in ({"newDate": "2026-10-27", "startTime": "10:00", "endTime": "11:30"}, {}):
        by_lesson = check(w, **params)
        by_request = client(w, "admin").get(f"{API}/org/substitutions/{item['id']}/move-check", params={"newDate": params.get("newDate", "2026-10-29"),
                                                                                                      "startTime": params.get("startTime", "16:00"), "endTime": params.get("endTime", "17:30")})
        assert by_lesson.status_code == 200 and by_lesson.json() == by_request.json(), params
    assert check(w, newDate="2026-10-27", startTime="10:00", endTime="11:30").json()["ok"] is False
    assert check(w).json()["ok"] is True
    assert len(requests_of(w)) == 1                       # the check writes nothing


# ------------------------------------------------------------------------------------------------------ X
def binding(w, who="admin", key="L2", day="2026-10-28", **q):
    g, lesson, _ = L(w, key, day)
    return client(w, who).get(f"{API}/org/groups/{g.id}/lesson-exam-binding", params={"lessonId": lesson, "lessonDate": day, **q})


def test_x_the_precheck_of_cancel(w, exams):
    """[X1..X5] none / lobby / active / exam-service down / foreign group, unknown lesson, teacher; equals ``examBinding`` of the request detail."""
    r = binding(w)
    assert r.status_code == 200 and r.json() == {"state": "none", "title": None, "canCancel": True}
    exams.completion_exams = [{"examId": "e1", "title": "Контрольная №3", "status": "lobby"}]
    assert binding(w).json() == {"state": "ok", "title": "Контрольная №3", "canCancel": True}
    exams.completion_exams = [{"examId": "e1", "title": "Контрольная №3", "status": "active"}]
    assert binding(w).json() == {"state": "ok", "title": "Контрольная №3", "canCancel": False}
    item = one(w, "A", "L2", "2026-10-28")
    detail = client(w, "admin").get(f"{API}/org/substitutions/{item['id']}").json()["examBinding"]
    assert detail == binding(w).json()
    exams.unavailable = True
    assert binding(w).json() == {"state": "unavailable", "title": None, "canCancel": False}
    exams.unavailable = False
    assert binding(w, key="L2", day="2026-10-28", lessonId="nope").status_code == 404
    gid = L(w, "L2", "2026-10-28")[0].id
    assert client(w, "admin").get(f"{API}/org/groups/00000000-0000-0000-0000-000000000000/lesson-exam-binding",
                                  params={"lessonId": "lesson-l2", "lessonDate": "2026-10-28"}).json()["detail"]["code"] == "GROUP_NOT_FOUND"
    assert binding(w, "A").status_code == 403 and binding(w, "D").status_code == 403 and gid
    assert exams.calls and all(c[0].endswith("/completion-check") for c in exams.calls)
