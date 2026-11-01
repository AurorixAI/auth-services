"""Wave B P4, the circle of R3: the owner's trail (5), the lazy retry that no longer stops at one refusal (6), the background task (4), the small ones (10, 11).

The fake exam-service is ``AuthService._sync_exam_lesson_cancellation`` (the seam of every test of the cancellation); the database is looked at from the test's own session.
"""
import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.auth import business_clock as bc
from src.auth import exam_release, exam_release_worker
from src.auth.exam_release import ExamAnswerError
from src.auth.service import AuthService
from src.models.auth import Group, GroupAttendance, GroupAudit

from tests.b2_helpers import build_d2, clock, client, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.test_wb_p4_exam_calls_pg import API, DAY, LESSON, NEXT, cancel, probe, row_of, w  # noqa: F401  (fixtures)

pytestmark = needs_pg


class Fake:
    """``plan[(phase, lesson_date)]`` = ``ok`` | ``down`` | an exception to raise; default ``ok``."""

    def __init__(self, monkeypatch) -> None:
        self.calls, self.plan = [], {}

        def fake(svc, *, phase, org_id, group_id, lesson_id, lesson_date, reason):
            self.calls.append((phase, lesson_date))
            how = self.plan.get((phase, lesson_date), self.plan.get((phase, None), "ok"))
            if how == "down":
                raise RuntimeError("Сервис экзаменов временно недоступен; урок не был отменён")
            if isinstance(how, BaseException):
                raise how

        monkeypatch.setattr(AuthService, "_sync_exam_lesson_cancellation", fake)


@pytest.fixture
def fake(monkeypatch, w):
    return Fake(monkeypatch)


def trail(w):
    w.db.expire_all()
    rows = w.db.query(GroupAudit).filter(GroupAudit.group_id == w.G1.id, GroupAudit.action.in_([exam_release.ACTION_PENDING, exam_release.ACTION_DONE])).order_by(GroupAudit.created_at, GroupAudit.id).all()
    return [(r.action, r.snapshot["outcome"], r.snapshot["message"]) for r in rows]


# ---- R3 5: the trail for the owner -----------------------------------------------------------------------------------------------------------------------------


def test_r3_5_a_cancellation_whose_exam_is_not_released_leaves_a_trail_once_and_the_end_of_the_retry_another(w, fake):
    fake.plan[("release", None)] = "down"
    assert cancel(w).status_code == 200
    expected = "Экзамен по уроку 26.10 не освобождён (сервис экзаменов недоступен) — повторим автоматически"
    assert trail(w) == [(exam_release.ACTION_PENDING, "pending", expected)]
    # the retry fails again: no second row for the same lesson
    assert [x["code"] for x in exam_release.release_pending(AuthService(w.db), w.org.id)] == ["EXAM_RELEASE_PENDING"]
    assert len(trail(w)) == 1
    # exam-service is back: the next retry releases it and says so
    fake.plan.clear()
    assert exam_release.release_pending(AuthService(w.db), w.org.id) == []
    assert trail(w) == [(exam_release.ACTION_PENDING, "pending", expected), (exam_release.ACTION_DONE, "released", "Экзамен по уроку 26.10 освобождён")]
    assert row_of(w).exam_release_pending is False


def test_r3_5_a_release_that_went_through_at_once_writes_no_trail(w, fake):
    assert cancel(w).status_code == 200
    assert trail(w) == [] and row_of(w).exam_release_pending is False


def test_r3_5_a_refusal_of_exam_service_is_told_too(w, fake):
    fake.plan[("release", None)] = ValueError("Экзамен уже нельзя безопасно вернуть в черновик")
    assert cancel(w).status_code == 200
    assert [(a, o) for a, o, _ in trail(w)] == [(exam_release.ACTION_DONE, "refused")]


# ---- R3 6: one lesson that exam-service cannot handle does not block the others ----------------------------------------------------------------------------------


def test_r3_6_an_answer_with_a_code_about_one_lesson_is_cleared_and_the_run_goes_on(w, fake):
    fake.plan[("release", None)] = "down"
    assert cancel(w).status_code == 200 and cancel(w, day=NEXT, reason="Другой").status_code == 200
    assert row_of(w).exam_release_pending is True and row_of(w, NEXT).exam_release_pending is True
    fake.plan.clear()
    fake.plan[("release", DAY)] = ExamAnswerError(422, "exam-service: 422")          # its verdict about THAT lesson
    warnings = exam_release.release_pending(AuthService(w.db), w.org.id)
    assert [x["code"] for x in warnings] == ["EXAM_NOT_RELEASED"]
    assert row_of(w).exam_release_pending is False and row_of(w, NEXT).exam_release_pending is False, "the second lesson was released although the first was refused"
    assert [c[1] for c in fake.calls[-2:]] == [DAY, NEXT]


def test_r3_6_a_5xx_is_the_trouble_of_the_moment_it_stops_and_keeps_the_markers(w, fake):
    fake.plan[("release", None)] = "down"
    assert cancel(w).status_code == 200 and cancel(w, day=NEXT, reason="Другой").status_code == 200
    n = len(fake.calls)
    fake.plan[("release", None)] = ExamAnswerError(503, "exam-service: 503")
    warnings = exam_release.release_pending(AuthService(w.db), w.org.id)
    assert [x["code"] for x in warnings] == ["EXAM_RELEASE_PENDING"] and len(fake.calls) == n + 1
    assert row_of(w).exam_release_pending is True and row_of(w, NEXT).exam_release_pending is True


# ---- R3 4: the background task ----------------------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def frozen():
    def set_to(local: datetime):
        bc.set_clock(lambda: local.replace(tzinfo=bc.BUSINESS_TIMEZONE))
    yield set_to
    bc.set_clock(None)


def test_r3_4_the_schedule_follows_the_business_clock(frozen):
    frozen(datetime(2026, 11, 2, 12, 0))
    schedule = exam_release_worker.Schedule(15 * 60)
    assert not schedule.due() and schedule.seconds_left() == exam_release_worker.FIRST_DELAY_SECONDS
    frozen(datetime(2026, 11, 2, 12, 1))
    assert schedule.due()
    schedule.ran()
    frozen(datetime(2026, 11, 2, 12, 15))
    assert not schedule.due() and schedule.seconds_left() == 60
    frozen(datetime(2026, 11, 2, 12, 16))
    assert schedule.due()


def test_r3_4_a_round_releases_the_waiting_lessons_logs_the_number_and_leaves_nothing(w, fake, probe, caplog):
    fake.plan[("release", None)] = "down"
    assert cancel(w).status_code == 200 and row_of(w).exam_release_pending is True
    fake.plan.clear()
    with caplog.at_level("INFO", logger="src.auth.exam_release_worker"):
        out = exam_release_worker.sweep_once(session_factory=lambda: Session(probe), lock_connection=probe.connect)
    assert out["skipped"] is False and out["lessons_before"] >= 1 and out["lessons_left"] == 0
    assert any("exam release sweep: waiting before" in r.getMessage() for r in caplog.records)
    assert row_of(w).exam_release_pending is False
    assert [o for _, o, _ in trail(w)] == ["pending", "released"]


def test_r3_4_a_second_executor_skips_the_round_while_the_first_holds_the_lock(w, fake, probe):
    """Several workers of auth: ``pg_try_advisory_lock`` - one executor; the other skips and touches nothing."""
    fake.plan[("release", None)] = "down"
    assert cancel(w).status_code == 200
    fake.plan.clear()
    n = len(fake.calls)
    with probe.connect() as holder:
        holder = holder.execution_options(isolation_level="AUTOCOMMIT")
        assert holder.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": exam_release_worker.LOCK_KEY}).scalar()
        try:
            out = exam_release_worker.sweep_once(session_factory=lambda: Session(probe), lock_connection=probe.connect)
        finally:
            holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": exam_release_worker.LOCK_KEY})
    assert out == {"skipped": True} and len(fake.calls) == n and row_of(w).exam_release_pending is True
    again = exam_release_worker.sweep_once(session_factory=lambda: Session(probe), lock_connection=probe.connect)       # the lock was let go: the next round works
    assert again["skipped"] is False and row_of(w).exam_release_pending is False


def test_r3_4_the_loop_runs_rounds_by_the_clock_and_stops_on_the_flag(monkeypatch, frozen):
    frozen(datetime(2026, 11, 2, 12, 0))
    monkeypatch.setattr(exam_release_worker, "FIRST_DELAY_SECONDS", 0)
    monkeypatch.setattr(exam_release_worker, "TICK_SECONDS", 0.05)
    rounds = []

    def fake_round():
        rounds.append(bc.utc_now())
        if len(rounds) == 2:
            stop.set()                      # the second round asks the loop to stop

    monkeypatch.setattr(exam_release_worker, "sweep_once", fake_round)

    async def main():
        global stop
        stop = asyncio.Event()
        task = asyncio.create_task(exam_release_worker.run_forever(stop, interval=900))
        await asyncio.sleep(0.3)
        assert len(rounds) == 1, "the second round is 15 minutes away"
        frozen(datetime(2026, 11, 2, 12, 16))
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(main())
    assert len(rounds) == 2


def test_r3_4_the_task_is_off_in_testing_and_with_zero_seconds(monkeypatch):
    assert exam_release_worker.enabled() is False                      # APP_ENV=testing
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("EXAM_RELEASE_SWEEP_SECONDS", "0")
    assert exam_release_worker.enabled() is False
    monkeypatch.setenv("EXAM_RELEASE_SWEEP_SECONDS", "900")
    assert exam_release_worker.enabled() is True


def test_r3_4_the_application_starts_and_stops_the_task_with_its_lifespan(monkeypatch):
    import src.api.main_v2 as main_v2

    started = {}

    async def fake_loop(stop, interval=None):
        started["on"] = True
        await stop.wait()
        started["stopped"] = True

    monkeypatch.setattr(exam_release_worker, "enabled", lambda: True)
    monkeypatch.setattr(exam_release_worker, "run_forever", fake_loop)
    monkeypatch.setattr("src.core.db_timezone.verify_database_timezone", lambda engine: None)
    monkeypatch.setattr("src.core.startup_guard.check_public_urls", lambda: None)
    monkeypatch.setattr("alembic.command.upgrade", lambda cfg, rev: None)

    async def go():
        async with main_v2.lifespan(main_v2.app):
            await asyncio.sleep(0.05)
            assert started == {"on": True}
        assert started == {"on": True, "stopped": True}

    asyncio.run(go())


# ---- R3 10 / 11 -------------------------------------------------------------------------------------------------------------------------------------------------------


def test_r3_10_the_count_of_the_sweep_counts_only_cancelled_rows(w, fake):
    fake.plan[("release", None)] = "down"
    assert cancel(w).status_code == 200
    base = exam_release.pending_count(w.db)
    row = row_of(w)
    row.status = "in_progress"                                      # a marker that nothing will ever take (the row is not cancelled any more)
    w.db.commit()
    assert exam_release.pending_count(w.db) == base - 1
    row.status = "cancelled"
    w.db.commit()


def test_r3_11_a_failure_of_the_release_never_turns_a_committed_day_off_cancellation_into_a_refusal(w, monkeypatch):
    from tests.test_ops_b2_decide_pg import Sync

    Sync(monkeypatch)
    monkeypatch.setattr(exam_release, "release_pending", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    r = client(w, "owner").post(f"{API}/org/days-off", json={"dateFrom": "2026-10-28", "dateTo": "2026-10-28", "reason": "Хайит"})
    assert r.status_code == 201, r.text
    assert [x["lessonId"] for x in r.json()["cancelled"]] == ["lesson-l2"], r.text
    assert row_of(w, day="2026-10-28", lesson="lesson-l2") is not None
