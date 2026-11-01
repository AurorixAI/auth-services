"""Wave B P4 (B8): the calls to exam-service are made OUTSIDE the locks.

SV-15: cancelling a lesson asks exam-service (a) BEFORE anything is locked or written (the check, still fail-closed) and (b) AFTER the commit (the release);
while the call runs the group row is NOT locked and the cancellation is already committed.  An exam-service that does not answer the release leaves a durable
marker (``group_attendance.exam_release_pending``) and the next write to the group retries it (LD-23, no cron); a lesson that is given back loses its marker.
DM-007: archiving a group asks the impact BEFORE the group lock, and the GET ``archive-impact`` has no side effect (it never retries a release).
The probes look at the database from a SECOND connection while the fake exam-service answers: ``FOR UPDATE NOWAIT`` on the group must succeed.
"""
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from src.auth.service import AuthService
from src.core.db_timezone import utc_connect_args
from src.models.auth import Group, GroupAttendance

from tests.b2_helpers import build_d2, clock, client, decide, exams, needs_pg, pg_session, req  # noqa: F401  (fixtures)

pytestmark = needs_pg
API = "/api/v1/auth"
DAY, LESSON = "2026-10-26", "lesson-l1"           # G1, Mon 10:00-11:30, five days ahead of the clock (Wed 21.10 14:00)
NEXT = "2026-11-02"                              # the same lesson a week later


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    return build_d2(pg_session)


@pytest.fixture
def probe():
    url = os.environ["A2_AUTH_TEST_DATABASE_URL"]
    engine = create_engine(url, connect_args=utc_connect_args(url), pool_pre_ping=True)
    yield engine
    engine.dispose()


class Exam:
    """Replaces ``AuthService._sync_exam_lesson_cancellation``: records the phase and what a SECOND connection sees at that moment."""

    def __init__(self, monkeypatch, probe, group_id) -> None:
        self.calls, self.mode, self.seen = [], {}, []
        self.probe, self.group_id = probe, str(group_id)
        outer = self

        def fake(svc, *, phase, org_id, group_id, lesson_id, lesson_date, reason):
            outer.calls.append((phase, lesson_id, lesson_date))
            outer.seen.append((phase, outer.look(lesson_id, lesson_date)))
            how = outer.mode.get(phase, "ok")
            if how == "down":
                raise RuntimeError("Сервис экзаменов временно недоступен; урок не был отменён")
            if how == "live":
                raise ValueError("Экзамен уже нельзя безопасно вернуть в черновик")

        monkeypatch.setattr(AuthService, "_sync_exam_lesson_cancellation", fake)

    def look(self, lesson_id, lesson_date):
        """``{"group_free": the group row can be locked by somebody else, "status": the committed status of the lesson row (None: no row)}``."""
        with self.probe.connect() as conn:
            try:
                conn.execute(text("SET LOCAL lock_timeout = '300ms'"))
                conn.execute(text("SELECT id FROM groups WHERE id = :g FOR UPDATE NOWAIT"), {"g": self.group_id})
                free = True
            except OperationalError:
                free = False
            conn.rollback()
            status = conn.execute(text("SELECT status FROM group_attendance WHERE group_id = :g AND lesson_id = :l AND lesson_date = :d"),
                                  {"g": self.group_id, "l": lesson_id, "d": lesson_date}).scalar()
            return {"group_free": free, "status": status}


@pytest.fixture
def exam(monkeypatch, probe, w):
    return Exam(monkeypatch, probe, w.G1.id)


def cancel(w, who="admin", day=DAY, lesson=LESSON, reason="Сам отменил"):
    return client(w, who).post(f"{API}/org/groups/{w.G1.id}/attendance",
                               json={"lessonDate": day, "lessonId": lesson, "status": "cancelled", "cancellationReason": reason, "records": {}})


def row_of(w, day=DAY, lesson=LESSON):
    w.db.expire_all()
    return w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_id=lesson, lesson_date=day).first()


def test_the_check_runs_before_any_lock_and_the_release_after_the_commit_and_outside_the_group_lock(w, exam):
    r = cancel(w)
    assert r.status_code == 200, r.text
    assert [p for p, _ in exam.seen] == ["cancellation-check", "release"]
    check, release = exam.seen[0][1], exam.seen[1][1]
    assert check == {"group_free": True, "status": None}, "the check is asked before the lesson is written and with the group NOT locked"
    assert release == {"group_free": True, "status": "cancelled"}, "the release is asked after the commit and with the group NOT locked"
    assert row_of(w).exam_release_pending is False, "released: the marker is cleared"
    assert r.json()["warnings"] == []


def test_exam_service_down_at_the_release_cancels_anyway_marks_the_row_and_the_next_write_retries(w, exam):
    exam.mode["release"] = "down"
    r = cancel(w)
    assert r.status_code == 200, r.text
    assert row_of(w).status == "cancelled" and row_of(w).exam_release_pending is True
    assert [x["code"] for x in r.json()["warnings"]] == ["EXAM_RELEASE_PENDING"]
    # the next write to the group (another cancellation) asks again: the old marker is retried FIRST, then the new one
    exam.mode["release"] = "ok"
    n = len(exam.calls)
    r2 = cancel(w, day=NEXT, reason="Ещё один")
    assert r2.status_code == 200 and r2.json()["warnings"] == []
    released = [c for c in exam.calls[n:] if c[0] == "release"]
    assert sorted(c[2] for c in released) == [DAY, NEXT]
    assert row_of(w).exam_release_pending is False and row_of(w, NEXT).exam_release_pending is False


def test_the_check_stays_fail_closed_nothing_is_written_when_exam_service_does_not_answer_it(w, exam):
    exam.mode["cancellation-check"] = "down"
    r = cancel(w)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "EXAM_SERVICE_UNAVAILABLE"
    assert row_of(w) is None
    assert [p for p, _ in exam.seen] == ["cancellation-check"], "no release is asked for a lesson that was not cancelled"


def test_a_live_exam_refuses_the_cancellation_before_it_is_written(w, exam):
    exam.mode["cancellation-check"] = "live"
    r = cancel(w)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "LESSON_HAS_LIVE_EXAM"
    assert row_of(w) is None


def test_an_exam_that_cannot_be_released_any_more_clears_the_marker_with_a_warning(w, exam):
    """The check passed, a student started the exam meanwhile: exam-service refuses the release (409).  Nothing can be released: the marker is cleared (no endless retry), the owner is told."""
    exam.mode["release"] = "live"
    r = cancel(w)
    assert r.status_code == 200
    assert [x["code"] for x in r.json()["warnings"]] == ["EXAM_NOT_RELEASED"]
    assert row_of(w).exam_release_pending is False


def test_a_cancelled_lesson_given_back_loses_its_marker_and_is_not_released_afterwards(w, exam):
    exam.mode["release"] = "down"
    assert cancel(w).status_code == 200 and row_of(w).exam_release_pending is True
    back = client(w, "admin").post(f"{API}/org/groups/{w.G1.id}/lessons/restore", json={"lessonId": LESSON, "lessonDate": DAY, "reason": "Передумали"})
    assert back.status_code == 200, back.text
    assert row_of(w) is None
    exam.mode["release"] = "ok"
    n = len(exam.calls)
    assert cancel(w, day=NEXT, reason="Другой").status_code == 200
    assert [c[2] for c in exam.calls[n:] if c[0] == "release"] == [NEXT], "a lesson that is back is not released: its row (and the marker) is gone"


def test_the_decision_of_a_request_cancels_with_the_same_order_of_calls(w, exam):
    """The road of «Замены»: the request of a teacher is decided «Отменить урок» (cancel_lesson): check before the locks, release after the commit."""
    made = req(w, "A", [(w.G1, LESSON, DAY)])
    assert made.status_code == 201, made.text
    request_id = made.json()["created"][0]["id"]
    done = decide(w, "admin", request_id, decision="cancel", reason="Отмена по болезни")
    assert done.status_code == 200, done.text
    assert [p for p, _ in exam.seen] == ["cancellation-check", "release"]
    assert exam.seen[0][1] == {"group_free": True, "status": None}
    assert exam.seen[1][1] == {"group_free": True, "status": "cancelled"}
    assert [x["code"] for x in done.json()["result"]["warnings"]] == []


def test_the_decision_of_a_request_with_exam_service_down_at_the_release_keeps_the_cancellation_and_warns(w, exam):
    made = req(w, "A", [(w.G1, LESSON, DAY)])
    exam.mode["release"] = "down"
    done = decide(w, "admin", made.json()["created"][0]["id"], decision="cancel", reason="Отмена по болезни")
    assert done.status_code == 200, done.text
    assert [x["code"] for x in done.json()["result"]["warnings"]] == ["EXAM_RELEASE_PENDING"]
    assert row_of(w).status == "cancelled" and row_of(w).exam_release_pending is True


def test_a_move_checks_before_the_locks_and_releases_after_the_commit(w, exam):
    r = client(w, "admin").post(f"{API}/org/groups/{w.G1.id}/lesson-move",
                                json={"lessonId": LESSON, "lessonDate": DAY, "newDate": "2026-10-28", "startTime": "14:00", "endTime": "15:30", "reason": "Перенос по просьбе"})
    assert r.status_code == 200, r.text
    assert [p for p, _ in exam.seen] == ["cancellation-check", "release"]
    assert exam.seen[0][1]["group_free"] is True and exam.seen[1][1] == {"group_free": True, "status": "cancelled"}


# ---- DM-007: the archive of a group -------------------------------------------------------------------------------------------------------------------------


class Bridge:
    """Replaces ``exam_bridge._post``: records the path and what a SECOND connection sees (is the group locked?) while exam-service 'answers'."""

    def __init__(self, monkeypatch, probe) -> None:
        from src.auth import exam_bridge

        self.bridge, self.probe, self.calls, self.release = exam_bridge, probe, [], "ok"
        monkeypatch.setattr(exam_bridge, "_post", self._post)

    def _post(self, path, payload):
        with self.probe.connect() as conn:
            try:
                conn.execute(text("SET LOCAL lock_timeout = '300ms'"))
                conn.execute(text("SELECT id FROM groups WHERE id = :g FOR UPDATE NOWAIT"), {"g": payload["group_id"]})
                free = True
            except OperationalError:
                free = False
            conn.rollback()
            status = conn.execute(text("SELECT status FROM groups WHERE id = :g"), {"g": payload["group_id"]}).scalar()
        self.calls.append((path.rsplit("/", 1)[-1], payload["group_id"], free, status))
        if path.endswith("/archive-release") and self.release == "down":
            raise self.bridge.ExamBridgeUnavailable("down at release")
        return {"exams": [{"examId": "EX1", "title": "Контрольная", "status": "lobby", "canRelease": True}], "releasedExamIds": ["EX1"]}


@pytest.fixture
def bridge(monkeypatch, probe, w):
    yield Bridge(monkeypatch, probe)
    # a test of this file may leave a group with the marker: the sweep of ANOTHER test file counts the markers of the whole database
    w.db.rollback()
    w.db.query(Group).filter(Group.org_id == w.org.id, Group.exam_release_pending.is_(True)).update({Group.exam_release_pending: False}, synchronize_session=False)
    w.db.commit()


def empty_group(w, name):
    from tests.ops_helpers import new_group, new_slot

    group = new_group(w.db, w.org, w.ids, "E" + name, name, w.C, [new_slot("lesson-f", 5, "15:00", "16:00", "2026-10-22")])
    w.db.commit()
    return group


def test_archive_asks_the_impact_before_the_group_lock_and_releases_after_the_commit(w, bridge):
    group = empty_group(w, "Пустая")
    r = client(w, "admin").post(f"{API}/org/groups/{group.id}/archive", json={"reason": "Закрыта"})
    assert r.status_code == 200, r.text
    impact, release = bridge.calls
    assert impact[0] == "archive-impact" and impact[2] is True and impact[3] == "active", "the impact is asked with the group NOT locked"
    assert release[0] == "archive-release" and release[2] is True and release[3] == "archived", "the release is asked after the commit, group NOT locked"


def test_the_get_archive_impact_has_no_side_effect_even_when_a_release_is_pending(w, bridge):
    group = empty_group(w, "Ждёт")
    bridge.release = "down"
    r = client(w, "admin").post(f"{API}/org/groups/{group.id}/archive", json={"reason": "Закрыта"})
    assert r.status_code == 200 and r.json()["examReleasePending"] is True
    w.db.expire_all()
    assert w.db.get(Group, group.id).exam_release_pending is True
    bridge.release = "ok"
    n = len(bridge.calls)
    got = client(w, "admin").get(f"{API}/org/groups/{group.id}/archive-impact")
    assert got.status_code == 200
    assert bridge.calls[n:] == [], "a GET writes nothing and calls nobody"
    w.db.expire_all()
    assert w.db.get(Group, group.id).exam_release_pending is True


def test_the_next_archive_of_the_centre_retries_the_pending_release_of_an_earlier_one(w, bridge):
    first, second = empty_group(w, "Первая"), empty_group(w, "Вторая")
    bridge.release = "down"
    assert client(w, "admin").post(f"{API}/org/groups/{first.id}/archive", json={"reason": "Закрыта"}).json()["examReleasePending"] is True
    bridge.release = "ok"
    assert client(w, "admin").post(f"{API}/org/groups/{second.id}/archive", json={"reason": "Закрыта"}).status_code == 200
    w.db.expire_all()
    assert w.db.get(Group, first.id).exam_release_pending is False and w.db.get(Group, second.id).exam_release_pending is False


MISSED = {"lessonDate": "2026-10-15", "lessonId": "lesson-l5", "cancellationReason": "Праздник"}       # G4, Thu 16:00: never started, six days ago


def missed_row(w):
    w.db.expire_all()
    return w.db.query(GroupAttendance).filter_by(group_id=w.G4.id, lesson_id="lesson-l5", lesson_date="2026-10-15").first()


def test_the_missed_lesson_road_checks_before_the_lock_and_releases_after_the_commit(w, monkeypatch, probe):
    """«Урока не было» (reconcile/cancel) of a past lesson that never started: the same order of calls as every other road of the cancellation."""
    exam = Exam(monkeypatch, probe, w.G4.id)
    r = client(w, "admin").post(f"{API}/org/groups/{w.G4.id}/attendance/reconcile/cancel", json=MISSED)
    assert r.status_code == 200, r.text
    assert [p for p, _ in exam.seen] == ["cancellation-check", "release"]
    assert exam.seen[0][1] == {"group_free": True, "status": None}
    assert exam.seen[1][1] == {"group_free": True, "status": "cancelled"}
    assert missed_row(w).exam_release_pending is False


def test_the_missed_lesson_road_with_exam_service_down_at_the_release_keeps_the_cancellation_and_warns(w, monkeypatch, probe):
    exam = Exam(monkeypatch, probe, w.G4.id)
    exam.mode["release"] = "down"
    r = client(w, "admin").post(f"{API}/org/groups/{w.G4.id}/attendance/reconcile/cancel", json=MISSED)
    assert r.status_code == 200, r.text
    assert [x["code"] for x in r.json()["warnings"]] == ["EXAM_RELEASE_PENDING"]
    assert missed_row(w).exam_release_pending is True
