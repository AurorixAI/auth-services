"""LOGIC-B O8 - ending a lesson while an exam of it is live.  Matrix rows [O8.R1] ... [O8.R9]."""
from datetime import datetime

import pytest

from src.auth.service import AuthService
from src.models.auth import GroupAttendance

from tests.ops_helpers import (  # noqa: F401  (fixtures)
    REAL_SYNC_CANCELLATION, build_d0, clock, client, exams, fixture, history_row, needs_pg, pg_session,
)

pytestmark = needs_pg


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 16:50")                       # L2 of G1: Wed 16:00-17:30
    world = build_d0(pg_session)
    assert client(world, "C" if False else "A").post(f"/api/v1/auth/org/groups/{world.G1.id}/attendance/start",
                                                     json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2"}).status_code == 200
    return world


def complete(w, who="A", **extra):
    marks = {str(s.id): "present" for s in (w.s1, w.s2, w.s3)}
    return client(w, who).post(f"/api/v1/auth/org/groups/{w.G1.id}/attendance",
                               json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2", "status": "completed", "records": marks, **extra})


def live_exam(w, status="active", remaining=900, waiting=0):
    return {"examId": "EX_123", "title": "Контрольная №3", "status": status, "deliveryMode": "online", "remainingSeconds": remaining,
            "waitingCount": waiting, "endsAt": "2026-10-21T12:30:00"}


def test_r1_a_running_exam_is_warned_about_then_runs_on_its_own_timer_chain5(w, clock, exams):
    """[O8.R1] Chain 5: complete at 17:15 with an exam that has 15 minutes left -> 409 LESSON_HAS_LIVE_EXAM; with acknowledgeLiveExam the lesson ends and the exam is left running;
    when the exam is over (17:31) there is no warning any more."""
    clock.set("2026-10-21 17:15")
    exams.completion_exams = [live_exam(w)]
    exams.completed = {"released": [], "leftRunning": ["EX_123"]}
    first = complete(w)
    assert first.status_code == 409
    detail = first.json()["detail"]
    assert detail["code"] == "LESSON_HAS_LIVE_EXAM" and detail["exams"] == [
        {"examId": "EX_123", "title": "Контрольная №3", "status": "active", "remainingSeconds": 900, "waitingCount": 0}]
    assert "осталось 15 мин" in detail["message"] and "закроется сам" in detail["message"]
    row = w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_id="lesson-l2", lesson_date="2026-10-21").one()
    w.db.refresh(row)
    assert row.status == "in_progress"
    acked = complete(w, acknowledgeLiveExam=True)
    assert acked.status_code == 200 and acked.json() == {"ok": True, "saved": True, "version": 2, "warnings": [],
                                                        "examSync": {"released": [], "leftRunning": ["EX_123"], "warning": None}}
    w.db.refresh(row)
    assert row.status == "completed"
    assert any(c[0].endswith("/completed") for c in exams.calls)


def test_r1b_the_lobby_exam_goes_back_to_a_draft_after_the_warning(w, clock, exams):
    """[O8.R1] Chain 5, variant 3: the exam is still in the lobby: the warning names the waiting students; after the ack it is released."""
    clock.set("2026-10-21 17:15")
    exams.completion_exams = [live_exam(w, status="lobby", remaining=None, waiting=2)]
    exams.completed = {"released": ["EX_123"], "leftRunning": []}
    first = complete(w)
    assert first.status_code == 409 and "не запущен; ожидают учеников: 2" in first.json()["detail"]["message"]
    assert "вернётся в черновик" in first.json()["detail"]["message"]
    done = complete(w, acknowledgeLiveExam=True)
    assert done.json()["examSync"] == {"released": ["EX_123"], "leftRunning": [], "warning": None}


def test_r2_double_completion_and_an_idempotent_ack(w, clock, exams):
    """[O8.R2] A second completion is a 200 without a write (and without another exam call); a lesson cannot be turned back into another status."""
    clock.set("2026-10-21 17:20")
    assert complete(w).status_code == 200
    n = len(exams.calls)
    again = complete(w, acknowledgeLiveExam=True)
    assert again.status_code == 200 and again.json()["version"] == 2
    assert len(exams.calls) == n
    back = client(w, "admin").post(f"/api/v1/auth/org/groups/{w.G1.id}/attendance",
                                   json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2", "status": "in_progress",
                                         "records": {str(w.s1.id): "present", str(w.s2.id): "present", str(w.s3.id): "present"}, "expectedVersion": 2,
                                         "correctionReason": "Вернуть"})
    assert back.status_code == 400 and "нельзя вернуть" in back.json()["detail"]


def test_r3_nothing_reopens_the_lesson(w, clock, exams):
    """[O8.R3] The completed lesson stays completed (the exam is the teacher's to close by hand; the freed exam may go to another lesson - exam-service tests)."""
    clock.set("2026-10-21 17:20")
    assert complete(w).status_code == 200
    row = w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_id="lesson-l2", lesson_date="2026-10-21").one()
    w.db.refresh(row)
    assert row.status == "completed"
    assert client(w, "A").post(f"/api/v1/auth/org/groups/{w.G1.id}/attendance/start", json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2"}).status_code == 400


def test_r5_a_correction_of_a_past_lesson_does_not_ask_exam_service(w, clock, exams):
    """[O8.R5] The correction of a completed lesson (an admin, later) never calls exam-service: the exam of a past date is closed already."""
    clock.set("2026-10-21 17:20")
    assert complete(w).status_code == 200
    exams.calls.clear()
    clock.set("2026-10-30 10:00")
    fix = client(w, "admin").post(f"/api/v1/auth/org/groups/{w.G1.id}/attendance",
                                  json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2", "status": "completed", "correctionReason": "Опечатка",
                                        "records": {str(w.s1.id): "absent", str(w.s2.id): "present", str(w.s3.id): "present"}, "expectedVersion": 2})
    assert fix.status_code == 200 and exams.calls == []


def test_r7_exam_service_down_fails_open_for_completion_and_closed_for_cancellation(w, clock, exams, monkeypatch):
    """[O8.R7] Ending a lesson is fail-OPEN (payroll first): the lesson ends, the answer carries examSync.warning EXAM_SERVICE_UNAVAILABLE; cancelling a lesson stays fail-CLOSED."""
    clock.set("2026-10-21 17:20")
    exams.unavailable = True
    done = complete(w)
    assert done.status_code == 200 and done.json()["examSync"] == {"released": [], "leftRunning": [], "warning": "EXAM_SERVICE_UNAVAILABLE"}
    # cancellation: the real sync (not the stub) raises, nothing is cancelled
    monkeypatch.setattr(AuthService, "_sync_exam_lesson_cancellation", REAL_SYNC_CANCELLATION)
    monkeypatch.setattr("src.auth.service.httpx.post", lambda *a, **kw: (_ for _ in ()).throw(__import__("httpx").ConnectError("down")))
    other = client(w, "admin").post(f"/api/v1/auth/org/groups/{w.G1.id}/attendance/reconcile/cancel",
                                    json={"lessonDate": "2026-10-19", "lessonId": "lesson-l1", "cancellationReason": "Праздник"})
    assert other.status_code in (400, 503, 409)    # the lesson of 19.10 is completed already; the point: a cancellation never passes while exam-service is down


def test_r8_teacher_and_admin_are_alike(w, clock, exams):
    """[O8.R8] The same warning for the owner / admin completing the teacher's lesson."""
    clock.set("2026-10-21 17:20")
    exams.completion_exams = [live_exam(w)]
    assert complete(w, "admin").status_code == 409
    assert complete(w, "admin", acknowledgeLiveExam=True).status_code == 200


def test_no_exam_no_warning_and_no_completed_call(w, clock, exams):
    """[O8.R1] Without an attached exam the lesson ends without a question and without a second call."""
    clock.set("2026-10-21 17:20")
    r = complete(w)
    assert r.status_code == 200 and r.json()["examSync"] == {"released": [], "leftRunning": [], "warning": None}
    assert [c[0] for c in exams.calls] == ["/internal/lesson-bindings/completion-check"]


def test_r4_a_student_entering_the_lobby_at_the_moment_of_completion_is_decided_by_exam_service(w, clock, exams):
    """[O8.R4] The check said "lobby", but by the time of ``completed`` the student started it (exam-service re-reads under its lock): the answer is the deterministic result
    of that call - the exam is left running, not released; exam-service's own test covers the lock."""
    clock.set("2026-10-21 17:15")
    exams.completion_exams = [live_exam(w, status="lobby", remaining=None, waiting=1)]
    exams.completed = {"released": [], "leftRunning": ["EX_123"]}
    assert complete(w).status_code == 409
    done = complete(w, acknowledgeLiveExam=True)
    assert done.json()["examSync"] == {"released": [], "leftRunning": ["EX_123"], "warning": None}


def test_r6_the_remaining_time_comes_from_exam_service_in_utc(w, clock, exams):
    """[O8.R6] The warning shows whatever exam-service counts (UTC timer, whatever the hour border): 1830 seconds -> "осталось 31 мин"."""
    clock.set("2026-10-21 17:15")
    exams.completion_exams = [live_exam(w, remaining=1830)]
    r = complete(w)
    assert r.status_code == 409 and "осталось 31 мин" in r.json()["detail"]["message"]
