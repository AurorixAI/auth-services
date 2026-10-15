"""N13 P2: pure payment-status rules (no database)."""
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

from src.auth import payment_status as ps  # noqa: E402
from src.auth.payment_status import PaymentRecord, StatusSettings, compute_status  # noqa: E402

TODAY = date(2026, 10, 15)
TASHKENT = ZoneInfo("Asia/Tashkent")


def cal(valid_to, paid_on=date(2026, 9, 1), valid_from=None, created=0):
    return PaymentRecord(
        paid_on=paid_on,
        valid_from=valid_from or paid_on,
        valid_to=valid_to,
        created_at=datetime(2026, 9, 1) + timedelta(seconds=created),
    )


def les(n=12, valid_from=date(2026, 9, 1), paid_on=None, created=0):
    return PaymentRecord(
        paid_on=paid_on or valid_from,
        valid_from=valid_from,
        lessons_count=n,
        created_at=datetime(2026, 9, 1) + timedelta(seconds=created),
    )


def days(n):
    return TODAY + timedelta(days=n)


def status(payments, lessons=(), fee=450000, **settings):
    return compute_status(
        today=TODAY, monthly_fee=fee, payments=payments, lesson_dates=lessons,
        settings=StatusSettings(**settings) if settings else StatusSettings(),
    )


# ------------------------------------------------------------ calendar ----


@pytest.mark.parametrize(
    "offset,expected,overdue,left",
    [
        (-1, ps.EXPIRED, 1, None),
        (-30, ps.EXPIRED, 30, None),
        (0, ps.ENDS_TODAY, None, 0),
        (1, ps.EXPIRING, None, 1),
        (3, ps.EXPIRING, None, 3),
        (4, ps.ACTIVE, None, 4),
    ],
)
def test_calendar_boundaries(offset, expected, overdue, left):
    result = status([cal(days(offset))])
    assert (result.status, result.days_overdue, result.days_left) == (expected, overdue, left)
    assert result.period_mode == ps.CALENDAR and result.paid_until == days(offset)


def test_no_payments_is_none_and_zero_fee_is_free():
    assert status([]).status == ps.NONE
    assert status([], fee=None).status == ps.NONE
    assert status([], fee=0).status == ps.FREE
    assert status([], fee=0).period_mode is None


def test_zero_fee_with_payments_follows_the_dates():
    assert status([cal(days(-2))], fee=0).status == ps.EXPIRED


def test_latest_valid_to_wins_over_payment_order():
    # the later-entered payment ends earlier: the max valid_to still decides
    result = status([cal(days(10), created=0), cal(days(2), paid_on=date(2026, 10, 1), created=5)])
    assert result.paid_until == days(10) and result.status == ps.ACTIVE


def test_expiring_threshold_is_configurable():
    assert status([cal(days(5))], expiring_days=5).status == ps.EXPIRING
    assert status([cal(days(5))], expiring_days=4).status == ps.ACTIVE
    assert status([cal(days(1))], expiring_days=0).status == ps.ACTIVE
    assert status([cal(days(0))], expiring_days=0).status == ps.ENDS_TODAY


def test_buckets():
    cases = {
        ps.EXPIRED: ps.BUCKET_DUE, ps.ENDS_TODAY: ps.BUCKET_DUE, ps.EXPIRING: ps.BUCKET_EXPIRING,
        ps.NONE: ps.BUCKET_NONE, ps.ACTIVE: ps.BUCKET_ACTIVE, ps.FREE: ps.BUCKET_ACTIVE,
    }
    assert set(cases) == set(ps.STATUSES)
    for st, bucket in cases.items():
        assert ps.bucket_of(st) == bucket
        assert ps.in_bucket(st, bucket) and ps.in_bucket(st, ps.BUCKET_ALL)
        assert not ps.in_bucket(st, next(b for b in ps.BUCKETS[:4] if b != bucket))


# ------------------------------------------------------- Tashkent date ----


def test_business_date_flips_at_19_00_utc():
    before = datetime(2026, 10, 14, 18, 59, tzinfo=timezone.utc)
    after = datetime(2026, 10, 14, 19, 0, tzinfo=timezone.utc)
    assert ps.business_date(before, TASHKENT) == date(2026, 10, 14)
    assert ps.business_date(after, TASHKENT) == date(2026, 10, 15)
    # the default timezone of the service is Tashkent as well
    assert ps.business_date(after) == date(2026, 10, 15)
    assert ps.business_date(datetime(2026, 10, 14, 18, 59)) == date(2026, 10, 14)  # naive = UTC


def test_status_flips_with_the_tashkent_midnight():
    paid_until = date(2026, 10, 14)
    pay = [cal(paid_until)]
    at = lambda h, m: ps.business_date(datetime(2026, 10, 14, h, m, tzinfo=timezone.utc), TASHKENT)
    assert compute_status(today=at(18, 59), monthly_fee=1, payments=pay).status == ps.ENDS_TODAY
    result = compute_status(today=at(19, 0), monthly_fee=1, payments=pay)
    assert (result.status, result.days_overdue) == (ps.EXPIRED, 1)


# -------------------------------------------------------------- lessons ----


def lesson_days(*offsets):
    return [TODAY - timedelta(days=o) for o in offsets]


def test_lessons_remaining_thresholds():
    pay = [les(12)]
    assert status(pay, lesson_days(*range(1, 13))).status == ps.EXPIRED          # 0 left
    r = status(pay, lesson_days(*range(1, 10)))                                   # 3 left
    assert (r.status, r.lessons_left, r.lessons_used, r.lessons_total) == (ps.EXPIRING, 3, 9, 12)
    assert status(pay, lesson_days(*range(1, 9))).status == ps.ACTIVE            # 4 left
    r = status(pay, [])
    assert (r.status, r.lessons_left) == (ps.ACTIVE, 12) and r.days_left is None and r.paid_until is None
    assert status(pay, lesson_days(*range(1, 9)), expiring_lessons=4).status == ps.EXPIRING


def test_lessons_are_never_ends_today():
    for used in range(0, 14):
        assert status([les(12)], lesson_days(*range(1, used + 1))).status != ps.ENDS_TODAY


def test_lessons_before_valid_from_consume_nothing():
    pay = [les(2, valid_from=date(2026, 10, 1))]
    before = [date(2026, 9, 20), date(2026, 9, 27), date(2026, 9, 30)]
    r = status(pay, before)
    assert (r.lessons_used, r.lessons_left, r.status) == (0, 2, ps.EXPIRING)
    r = status(pay, before + [date(2026, 10, 1), date(2026, 10, 8)])
    assert (r.lessons_used, r.lessons_left, r.status) == (2, 0, ps.EXPIRED)


def test_lessons_fifo_over_two_payments():
    first = les(3, valid_from=date(2026, 9, 1), created=0)
    second = les(3, valid_from=date(2026, 9, 20), paid_on=date(2026, 9, 20), created=10)
    lessons = [date(2026, 9, d) for d in (2, 5, 8, 12, 16, 22, 25)]
    # first takes 2,5,8; second takes 22,25 (only two on/after 20.09, 12 and 16 stay unused)
    r = status([first, second], lessons)
    assert (r.lessons_total, r.lessons_used, r.lessons_left) == (6, 5, 1)
    assert r.status == ps.EXPIRING
    # same input in another list order gives the same answer (FIFO is by valid_from)
    assert status([second, first], lessons) == r
    total, used = ps.allocate_lessons([first, second], lessons)
    assert (total, used) == (6, 5)


def test_fifo_spillover_takes_lessons_after_valid_from_only():
    a = les(2, valid_from=date(2026, 9, 1), created=0)
    b = les(2, valid_from=date(2026, 9, 1), paid_on=date(2026, 9, 2), created=1)
    lessons = [date(2026, 9, d) for d in (3, 6, 9, 12, 15)]
    r = status([a, b], lessons)
    assert (r.lessons_total, r.lessons_used, r.lessons_left, r.status) == (4, 4, 0, ps.EXPIRED)


def test_unused_lessons_before_a_payment_are_not_carried_over():
    # payment A (valid from 1 Sep) is not exhausted; B starts later and cannot take A's lessons
    a = les(10, valid_from=date(2026, 9, 1), created=0)
    b = les(1, valid_from=date(2026, 10, 10), paid_on=date(2026, 10, 10), created=1)
    r = status([a, b], [date(2026, 9, 5), date(2026, 10, 12)])
    # A takes both lessons (FIFO, both on/after 1 Sep); B finds nothing left
    assert (r.lessons_used, r.lessons_left) == (2, 9)


def test_mode_is_decided_by_the_latest_payment():
    old_calendar = cal(days(-60), paid_on=date(2026, 8, 1))
    new_lessons = les(12, valid_from=date(2026, 10, 1), paid_on=date(2026, 10, 1), created=5)
    r = status([old_calendar, new_lessons], [])
    assert r.period_mode == ps.LESSONS and r.status == ps.ACTIVE and r.paid_until is None
    # and back: an older lessons payment is ignored once a calendar payment is the latest
    older_lessons = les(1, valid_from=date(2026, 8, 1), paid_on=date(2026, 8, 1))
    newer_calendar = cal(days(20), paid_on=date(2026, 10, 1), created=5)
    r = status([older_lessons, newer_calendar], lesson_days(1, 2, 3))
    assert r.period_mode == ps.CALENDAR and r.status == ps.ACTIVE and r.lessons_total is None


def test_latest_payment_ties_break_by_created_at():
    same_day = date(2026, 10, 1)
    c = PaymentRecord(paid_on=same_day, valid_from=same_day, valid_to=days(-5), created_at=datetime(2026, 10, 1, 10))
    l = PaymentRecord(paid_on=same_day, valid_from=same_day, lessons_count=12, created_at=datetime(2026, 10, 1, 11))
    assert status([c, l], []).period_mode == ps.LESSONS
    assert status([l, c], []).period_mode == ps.LESSONS


# ------------------------------------------- defaults, errors, warnings ----


@pytest.mark.parametrize(
    "start,expected",
    [
        ((2026, 10, 5), (2026, 11, 4)),
        ((2026, 10, 1), (2026, 10, 31)),     # the 1st: last day of the same month
        ((2026, 2, 1), (2026, 2, 28)),
        ((2028, 2, 1), (2028, 2, 29)),
        ((2026, 12, 1), (2026, 12, 31)),
        ((2026, 12, 15), (2027, 1, 14)),
        ((2026, 1, 31), (2026, 2, 28)),
        ((2026, 1, 30), (2026, 2, 28)),
        ((2026, 1, 29), (2026, 2, 28)),
        ((2026, 1, 28), (2026, 2, 27)),
        ((2028, 1, 31), (2028, 2, 29)),      # leap year
        ((2028, 2, 29), (2028, 3, 28)),
        ((2026, 3, 31), (2026, 4, 30)),
        ((2026, 11, 30), (2026, 12, 29)),
        ((2026, 5, 31), (2026, 6, 30)),
    ],
)
def test_add_month_minus_day(start, expected):
    assert ps.add_month_minus_day(date(*start)) == date(*expected)


def test_add_month_minus_day_never_raises_anything_but_overflow():
    assert ps.add_month_minus_day(date(9999, 12, 1)) == date(9999, 12, 31)
    with pytest.raises(OverflowError):
        ps.add_month_minus_day(date(9999, 12, 31))
    assert ps.add_month_minus_day(date.min.replace(day=2)) == date(1, 2, 1)


def test_default_valid_from():
    paid = date(2026, 10, 5)
    assert ps.default_valid_from("calendar", paid, None) == paid
    assert ps.default_valid_from("calendar", paid, date(2026, 10, 10)) == date(2026, 10, 11)
    assert ps.default_valid_from("calendar", paid, date(2026, 10, 4)) == paid  # day after == paid_on
    assert ps.default_valid_from("calendar", paid, date(2026, 9, 1)) == paid
    assert ps.default_valid_from("lessons", paid, date(2026, 10, 10)) == paid


def test_validation_errors():
    ok = dict(today=TODAY, paid_on=TODAY, valid_from=TODAY, valid_to=days(30))
    assert ps.validation_errors(**ok) == []
    assert ps.validation_errors(**{**ok, "paid_on": days(1)})
    assert ps.validation_errors(**{**ok, "paid_on": date(2023, 10, 14)})
    assert ps.validation_errors(**{**ok, "paid_on": date(2023, 10, 15), "valid_from": date(2023, 10, 15), "valid_to": date(2023, 11, 14)}) == []
    assert ps.validation_errors(**{**ok, "valid_to": days(-1)})
    assert ps.validation_errors(**{**ok, "valid_to": TODAY + timedelta(days=400)}) == []
    assert ps.validation_errors(**{**ok, "valid_to": TODAY + timedelta(days=401)})
    assert ps.validation_errors(**{**ok, "valid_to": None}) == []


def codes(**kw):
    base = dict(today=TODAY, paid_on=TODAY, valid_from=TODAY, valid_to=days(29), existing=[])
    return [w.code for w in ps.payment_warnings(**{**base, **kw})]


def test_warnings():
    assert codes() == []
    assert codes(existing=[cal(days(5), paid_on=days(-20), valid_from=days(-25))]) == ["overlaps_previous"]
    assert codes(existing=[cal(days(-1), paid_on=days(-30), valid_from=days(-31))]) == []   # adjacent
    assert codes(existing=[cal(days(-2), paid_on=days(-30), valid_from=days(-31))]) == ["gap_before"]
    assert codes(valid_to=TODAY + timedelta(days=61)) == []                                   # 62 days
    assert codes(valid_to=TODAY + timedelta(days=62)) == ["period_over_62_days"]              # 63 days
    assert codes(paid_on=TODAY - timedelta(days=60)) == []
    assert codes(paid_on=TODAY - timedelta(days=61)) == ["paid_over_60_days_ago"]
    assert codes(valid_to=None, existing=[cal(days(5), paid_on=days(-20), valid_from=days(-25))]) == []
    message = ps.payment_warnings(**{"today": TODAY, "paid_on": days(-90), "valid_from": TODAY,
                                      "valid_to": None, "existing": []})[0].message
    assert "60" in message


@pytest.mark.parametrize(
    "mark,excuse,expected",
    [
        ("present", None, True), ("late", None, True), ("absent", None, True),
        ("excused", None, True), ("excused", "pending", True), ("excused", "rejected", True),
        ("excused", "confirmed", False), ("present", "confirmed", True),
        (None, None, False), ("", None, False), ("unknown", None, False),
    ],
)
def test_lesson_counts(mark, excuse, expected):
    assert ps.lesson_counts(mark=mark, excuse_status=excuse) is expected


def test_valid_from_bounds():
    paid = TODAY
    ok = dict(today=TODAY, paid_on=paid, valid_to=None)
    assert ps.validation_errors(valid_from=paid, **ok) == []
    assert ps.validation_errors(valid_from=ps._years_ago(paid, 3), **ok) == []
    assert ps.validation_errors(valid_from=ps._years_ago(paid, 3) - timedelta(days=1), **ok)
    assert ps.validation_errors(valid_from=paid + timedelta(days=400), **ok) == []
    assert ps.validation_errors(valid_from=paid + timedelta(days=401), **ok)
    assert ps.valid_from_error(paid, paid) is None
    assert ps.valid_from_error(paid, paid + timedelta(days=401))


@pytest.mark.parametrize("extreme", [date.min, date.max, date(9999, 12, 30), date(1, 1, 2)])
def test_extreme_dates_are_errors_not_exceptions(extreme):
    for paid, frm, to in [(extreme, extreme, None), (TODAY, extreme, None), (TODAY, TODAY, extreme),
                          (extreme, TODAY, extreme)]:
        assert ps.validation_errors(today=TODAY, paid_on=paid, valid_from=frm, valid_to=to)
    assert ps.validation_errors(today=date(2026, 2, 28), paid_on=date(2026, 2, 28), valid_from=date(2026, 2, 28), valid_to=None) == []
    # today itself near the edges of the calendar must not raise either
    assert ps.validation_errors(today=date.min, paid_on=date.min, valid_from=date.min, valid_to=None) == []
    assert ps.payment_warnings(today=TODAY, paid_on=date.min, valid_from=date.min, valid_to=date.max, existing=[])


# ---- a payment that moved away with the student (N13 P2.1) ---------------------------


def moved(record, day):
    return PaymentRecord(
        paid_on=record.paid_on, valid_from=record.valid_from, valid_to=record.valid_to,
        lessons_count=record.lessons_count, created_at=record.created_at, carried_out_at=day,
    )


def test_calendar_payment_that_moved_ends_the_day_before():
    p = moved(cal(days(20), valid_from=days(-10)), days(0))
    assert p.end == days(-1) and not p.covers_nothing
    result = compute_status(today=TODAY, monthly_fee=None, payments=[p])
    assert (result.status, result.paid_until, result.days_overdue) == (ps.EXPIRED, days(-1), 1)
    # moved after its end: nothing changes; never extends it
    assert moved(cal(days(3), valid_from=days(-10)), days(9)).end == days(3)


def test_calendar_payment_that_moved_before_it_began_covers_nothing():
    p = moved(cal(days(30), paid_on=days(0), valid_from=days(5)), days(0))
    assert p.covers_nothing
    assert compute_status(today=TODAY, monthly_fee=None, payments=[p]).status == ps.NONE
    # the carry-over (a plain record from T) is the only coverage
    carry = cal(days(30), paid_on=days(0), valid_from=days(5), created=5)
    result = compute_status(today=TODAY, monthly_fee=None, payments=[p, carry])
    assert (result.status, result.paid_until) == (ps.ACTIVE, days(30))


def test_return_covers_again_only_through_the_new_carry_over():
    original = moved(cal(days(20), valid_from=days(-10)), days(0))
    back = cal(days(20), paid_on=original.paid_on, valid_from=days(0), created=9)
    result = compute_status(today=TODAY, monthly_fee=None, payments=[original, back])
    assert (result.status, result.paid_until) == (ps.ACTIVE, days(20))
    without = compute_status(today=TODAY, monthly_fee=None, payments=[original])
    assert without.status == ps.EXPIRED


def test_lessons_payment_that_moved_takes_only_lessons_before_the_day():
    package = moved(les(6, valid_from=days(-20)), days(-3))
    dates = [days(-10), days(-8), days(-3), days(-1)]            # the last two are after it moved
    detail = ps.allocate_lessons_detail([package], dates)
    assert detail[0][1] == [days(-10), days(-8)]
    assert ps.allocate_lessons([package], dates) == (2, 2)       # capacity = what it took: nothing left
    assert compute_status(today=TODAY, monthly_fee=None, payments=[package], lesson_dates=dates).lessons_left == 0
    assert ps.lesson_capacity(package, 2) == 2 and ps.lesson_capacity(les(6), 2) == 6


def test_lessons_return_leaves_original_minus_consumed():
    # 6 lessons, 2 used in A before the transfer on day -3, 1 used in B, 3 carried back: A shows 3 left
    original = moved(les(6, valid_from=days(-20)), days(-3))
    carry_back = les(3, valid_from=days(0), paid_on=days(-20), created=9)
    dates = [days(-10), days(-8)]                                 # A's own lessons
    total, used = ps.allocate_lessons([original, carry_back], dates)
    assert (total, used) == (5, 2) and total - used == 3
    # lessons held in A after the student came back are taken by the carry-over
    total, used = ps.allocate_lessons([original, carry_back], dates + [days(1)])
    assert (total, used) == (5, 3)


def test_lessons_payment_moved_on_its_first_day_takes_nothing():
    package = moved(les(6, valid_from=days(0)), days(0))
    assert ps.allocate_lessons([package], [days(0), days(1)]) == (0, 0)


def test_warnings_ignore_the_part_of_a_moved_payment_after_the_move():
    gone = moved(cal(days(20), valid_from=days(-10)), days(0))
    # a new period starting today overlaps nothing: the old one ended yesterday
    codes = [w.code for w in ps.payment_warnings(
        today=TODAY, paid_on=TODAY, valid_from=days(0), valid_to=days(30), existing=[gone])]
    assert "overlaps_previous" not in codes and "gap_before" not in codes
