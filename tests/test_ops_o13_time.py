"""LOGIC-B O13 - the two groups of time columns are mixed only through ``business_clock``.  Matrix rows [O13.R1] ... [O13.R9]."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from src.auth import business_clock as bc


def att(started=None, completed=None):
    return SimpleNamespace(started_at=started, completed_at=completed)


def test_r1_tashkent_wall_time_to_utc():
    """[O13.R1] Tashkent is UTC+5 without a change: 2026-10-21 10:00 local = 05:00 UTC."""
    assert bc.attendance_started_utc(att(started=datetime(2026, 10, 21, 10, 0))) == datetime(2026, 10, 21, 5, 0)
    assert bc.attendance_completed_utc(att(completed=datetime(2026, 10, 21, 11, 25))) == datetime(2026, 10, 21, 6, 25)
    assert bc.lesson_instant_utc(date(2026, 10, 21), 10 * 60) == datetime(2026, 10, 21, 5, 0)


def test_r2_applying_the_helper_twice_is_a_no_op_on_the_utc_value():
    """[O13.R2] A UTC value passed through the "UTC -> Tashkent -> UTC" pair comes back unchanged (and an aware value is accepted)."""
    utc = datetime(2026, 10, 21, 5, 0)
    assert bc.business_wall_to_utc(bc.utc_to_business_wall(utc)) == utc
    aware = datetime(2026, 10, 21, 10, 0, tzinfo=bc.BUSINESS_TIMEZONE)
    assert bc.business_wall_to_utc(aware) == bc.business_wall_to_utc(aware.replace(tzinfo=None)) == utc


def test_r5_duration_is_started_to_completed_not_created_at():
    """[O13.R5] A lesson started yesterday 23:50 and closed today 00:10 lasted 20 minutes - from the two Tashkent columns, never ``completed_at - created_at`` (UTC)."""
    row = att(started=datetime(2026, 10, 20, 23, 50), completed=datetime(2026, 10, 21, 0, 10))
    assert bc.attendance_duration_minutes(row) == 20
    created_at_utc = datetime(2026, 10, 20, 18, 50)                         # the same 23:50 as a UTC column
    assert (row.completed_at - created_at_utc) != timedelta(minutes=20)     # the mixed subtraction is wrong by 5 hours: exactly what the guard forbids
    assert bc.attendance_completed_utc(row) - bc.attendance_started_utc(row) == timedelta(minutes=20)


def test_r6_month_border_belongs_to_the_lesson_date():
    """[O13.R6] lesson_date 2026-10-31, completed_at 2026-11-01 00:05: the payroll month is October (``lesson_date``), the clock says the next business day."""
    lesson_date, completed = "2026-10-31", datetime(2026, 11, 1, 0, 5)
    assert lesson_date[:7] == "2026-10" and completed.date().isoformat()[:7] == "2026-11"
    assert bc.utc_to_business_date(datetime(2026, 10, 31, 19, 5)) == date(2026, 11, 1)        # 00:05 Tashkent is 19:05 UTC the day before


def test_r7_a_reconciled_lesson_has_no_start():
    """[O13.R7] ``started_at IS NULL`` (reconcile): the helper answers None, not a made-up instant."""
    assert bc.attendance_started_utc(att(started=None)) is None and bc.attendance_completed_utc(att(completed=None)) is None
    assert bc.attendance_duration_minutes(att(started=None, completed=datetime(2026, 10, 21, 11, 0))) is None


def test_the_business_clock_can_be_replaced_and_is_aware():
    """[O13.R9] (with test_time_convention_guard) ``business_now`` / ``business_today`` are the one source and tests replace them."""
    bc.set_clock(lambda: datetime(2026, 10, 21, 19, 20, tzinfo=timezone.utc))
    try:
        assert bc.business_now().hour == 0 and bc.business_today() == date(2026, 10, 22)        # 00:20 on the 22nd in Tashkent
        assert bc.utc_now() == datetime(2026, 10, 21, 19, 20)
    finally:
        bc.set_clock(None)
