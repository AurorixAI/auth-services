"""R3 4 (LD-23): the background retry of the release of exams.

Before (wave B P4): the markers ``group_attendance.exam_release_pending`` / ``groups.exam_release_pending`` were retried only by the NEXT write to the same group (or by hand) - a group
without new writes kept its lobby (and the access code) for weeks.  Now the process of auth asks again every ``INTERVAL_SECONDS`` (15 minutes) with the same logic as
``scripts/sweep_exam_releases.py`` (``run_sweep``).  Several workers / replicas: ``pg_try_advisory_lock`` on a connection of its own - one executor at a time, the others skip the round.
The task starts with the application (``main_v2.lifespan``) and stops with it; ``EXAM_RELEASE_SWEEP_SECONDS=0`` (or ``APP_ENV=testing``) switches it off.  Each round logs the number waiting.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import business_clock as bc
from . import exam_release

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 15 * 60
FIRST_DELAY_SECONDS = 60          # a minute after the start: the migrations are over, exam-service has had time to come up
LOCK_KEY = 0x4558_414D_5245_4C01  # ``pg_try_advisory_lock`` key of the sweep (a bigint of its own)
TICK_SECONDS = 30                 # how often the loop looks at the clock / the stop flag


def interval_seconds() -> int:
    try:
        return int(os.getenv("EXAM_RELEASE_SWEEP_SECONDS", str(INTERVAL_SECONDS)))
    except ValueError:
        return INTERVAL_SECONDS


def enabled() -> bool:
    return os.getenv("APP_ENV", "production") != "testing" and interval_seconds() > 0


class Schedule:
    """When the next round is due, on the business clock (``bc.set_clock`` of the tests moves it)."""

    def __init__(self, interval: int, first_delay: int = FIRST_DELAY_SECONDS, now: Optional[datetime] = None) -> None:
        self.interval = timedelta(seconds=interval)
        self.next_at = (now or bc.utc_now()) + timedelta(seconds=first_delay)

    def due(self, now: Optional[datetime] = None) -> bool:
        return (now or bc.utc_now()) >= self.next_at

    def ran(self, now: Optional[datetime] = None) -> None:
        self.next_at = (now or bc.utc_now()) + self.interval

    def seconds_left(self, now: Optional[datetime] = None) -> float:
        return max(0.0, (self.next_at - (now or bc.utc_now())).total_seconds())


def run_sweep(db: Session) -> Tuple[int, int, int]:
    """One round: the archived groups with the marker, then the cancelled lessons with it (whole database).  Returns ``(groups cleared, groups still pending, lessons still pending)``.
    Stops at the first answer-less exam-service (one wait, not one per centre)."""
    from .group_lifecycle import GroupLifecycle
    from .models import Group, GroupAttendance
    from .service import AuthService

    svc = AuthService(db)
    cleared = GroupLifecycle(svc).retry_pending_releases()
    orgs = [org for (org,) in db.query(GroupAttendance.org_id).filter(GroupAttendance.exam_release_pending.is_(True), GroupAttendance.status == "cancelled").distinct().all()]
    db.rollback()
    for org in orgs:
        warnings = exam_release.release_pending(svc, org, limit=10_000)
        if any(w["code"] == exam_release.PENDING for w in warnings):
            break
    groups_left = db.query(Group).filter(Group.exam_release_pending.is_(True), Group.status == "archived").count()
    lessons_left = exam_release.pending_count(db)
    db.rollback()
    return cleared, groups_left, lessons_left


def sweep_once(session_factory: Optional[Callable[[], Session]] = None, lock_connection: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
    """Take the advisory lock on a connection of its own; without it - ``{"skipped": True}`` (another worker is sweeping); with it - ``run_sweep`` and the log line."""
    if session_factory is None:
        from src.core.database import SessionLocal as session_factory      # type: ignore[assignment]
    if lock_connection is None:
        from src.core.database import engine

        lock_connection = engine.connect
    with lock_connection() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        if not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar():
            logger.info("exam release sweep: another worker holds the lock, skipped")
            return {"skipped": True}
        try:
            db = session_factory()
            try:
                before = exam_release.pending_count(db)
                db.rollback()
                cleared, groups_left, lessons_left = run_sweep(db)
            finally:
                db.close()
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
    logger.info("exam release sweep: waiting before %d lessons, now %d lessons and %d archived groups (cleared %d groups)", before, lessons_left, groups_left, cleared)
    return {"skipped": False, "lessons_before": before, "lessons_left": lessons_left, "groups_left": groups_left, "groups_cleared": cleared}


async def run_forever(stop: asyncio.Event, interval: Optional[int] = None) -> None:
    """The loop of the task: a round every ``interval`` seconds (the first one a minute after the start) until ``stop`` is set."""
    schedule = Schedule(interval if interval is not None else interval_seconds(), first_delay=FIRST_DELAY_SECONDS)
    while not stop.is_set():
        if schedule.due():
            try:
                await asyncio.to_thread(sweep_once)
            except Exception:                         # never kills the loop: the next round tries again
                logger.exception("exam release sweep failed")
            schedule.ran()
        try:
            await asyncio.wait_for(stop.wait(), timeout=min(TICK_SECONDS, max(1.0, schedule.seconds_left())))
        except asyncio.TimeoutError:
            pass
