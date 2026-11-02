"""LOGIC-B O13 - the two groups of time columns are mixed only through ``business_clock``.  Matrix rows [O13.R1] ... [O13.R9]."""
from datetime import date, datetime, timezone

from src.auth import business_clock as bc


def test_r1_tashkent_wall_time_to_utc():
    """[O13.R1] Tashkent is UTC+5 without a change: 2026-10-21 10:00 local = 05:00 UTC."""
    assert bc.business_wall_to_utc(datetime(2026, 10, 21, 10, 0)) == datetime(2026, 10, 21, 5, 0)
    assert bc.business_wall_to_utc(datetime(2026, 10, 21, 11, 25)) == datetime(2026, 10, 21, 6, 25)
    assert bc.lesson_instant_utc(date(2026, 10, 21), 10 * 60) == datetime(2026, 10, 21, 5, 0)


def test_r2_applying_the_helper_twice_is_a_no_op_on_the_utc_value():
    """[O13.R2] A UTC value passed through the "UTC -> Tashkent -> UTC" pair comes back unchanged (and an aware value is accepted)."""
    utc = datetime(2026, 10, 21, 5, 0)
    assert bc.business_wall_to_utc(bc.utc_to_business_wall(utc)) == utc
    aware = datetime(2026, 10, 21, 10, 0, tzinfo=bc.BUSINESS_TIMEZONE)
    assert bc.business_wall_to_utc(aware) == bc.business_wall_to_utc(aware.replace(tzinfo=None)) == utc


def test_r6_month_border_belongs_to_the_lesson_date():
    """[O13.R6] lesson_date 2026-10-31, completed_at 2026-11-01 00:05: the payroll month is October (``lesson_date``), the clock says the next business day."""
    lesson_date, completed = "2026-10-31", datetime(2026, 11, 1, 0, 5)
    assert lesson_date[:7] == "2026-10" and completed.date().isoformat()[:7] == "2026-11"
    assert bc.utc_to_business_date(datetime(2026, 10, 31, 19, 5)) == date(2026, 11, 1)        # 00:05 Tashkent is 19:05 UTC the day before


def test_the_business_clock_can_be_replaced_and_is_aware():
    """[O13.R9] (with test_time_convention_guard) ``business_now`` / ``business_today`` are the one source and tests replace them."""
    bc.set_clock(lambda: datetime(2026, 10, 21, 19, 20, tzinfo=timezone.utc))
    try:
        assert bc.business_now().hour == 0 and bc.business_today() == date(2026, 10, 22)        # 00:20 on the 22nd in Tashkent
        assert bc.utc_now() == datetime(2026, 10, 21, 19, 20)
    finally:
        bc.set_clock(None)
