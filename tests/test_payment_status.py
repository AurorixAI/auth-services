"""N13 P2: pure payment helpers (no database): carried-out payments, the hand-out of lessons, buckets, business date."""
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

from src.auth import payment_status as ps  # noqa: E402
from src.auth.payment_status import PaymentRecord  # noqa: E402

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


def test_buckets():
    cases = {
        ps.EXPIRED: ps.BUCKET_DUE, ps.ENDS_TODAY: ps.BUCKET_DUE, ps.EXPIRING: ps.BUCKET_EXPIRING,
        ps.NONE: ps.BUCKET_NONE, ps.ACTIVE: ps.BUCKET_ACTIVE, ps.FREE: ps.BUCKET_ACTIVE,
    }
    assert set(cases) | {ps.PAUSED} == set(ps.STATUSES)
    assert ps.bucket_of(ps.PAUSED) == ps.BUCKET_PAUSED                      # LOGIC-A: a paused student is on the "all" tab only
    tabs = (ps.BUCKET_DUE, ps.BUCKET_EXPIRING, ps.BUCKET_NONE, ps.BUCKET_ACTIVE)
    assert ps.in_bucket(ps.PAUSED, ps.BUCKET_ALL) and not any(ps.in_bucket(ps.PAUSED, b) for b in tabs)
    for st, bucket in cases.items():
        assert ps.bucket_of(st) == bucket
        assert ps.in_bucket(st, bucket) and ps.in_bucket(st, ps.BUCKET_ALL)
        assert not ps.in_bucket(st, next(b for b in tabs if b != bucket))


# ------------------------------------------------------- Tashkent date ----


def test_business_date_flips_at_19_00_utc():
    before = datetime(2026, 10, 14, 18, 59, tzinfo=timezone.utc)
    after = datetime(2026, 10, 14, 19, 0, tzinfo=timezone.utc)
    assert ps.business_date(before, TASHKENT) == date(2026, 10, 14)
    assert ps.business_date(after, TASHKENT) == date(2026, 10, 15)
    # the default timezone of the service is Tashkent as well
    assert ps.business_date(after) == date(2026, 10, 15)
    assert ps.business_date(datetime(2026, 10, 14, 18, 59)) == date(2026, 10, 14)  # naive = UTC


# -------------------------------------------------------------- lessons ----


def test_lessons_before_valid_from_are_not_handed_out():
    pay = les(2, valid_from=date(2026, 10, 1))
    before = [date(2026, 9, 20), date(2026, 9, 27), date(2026, 9, 30)]
    assert ps.allocate_lessons_detail([pay], before) == [(pay, [])]
    assert ps.allocate_lessons_detail([pay], before + [date(2026, 10, 1), date(2026, 10, 8)])[0][1] == [date(2026, 10, 1), date(2026, 10, 8)]


def test_lessons_fifo_over_two_payments():
    first = les(3, valid_from=date(2026, 9, 1), created=0)
    second = les(3, valid_from=date(2026, 9, 20), paid_on=date(2026, 9, 20), created=10)
    lessons = [date(2026, 9, d) for d in (2, 5, 8, 12, 16, 22, 25)]
    # first takes 2,5,8; second takes 22,25 (only two on/after 20.09, 12 and 16 stay unused)
    detail = ps.allocate_lessons_detail([first, second], lessons)
    assert detail == [(first, [date(2026, 9, d) for d in (2, 5, 8)]), (second, [date(2026, 9, 22), date(2026, 9, 25)])]
    # same input in another list order gives the same answer (FIFO is by valid_from)
    assert ps.allocate_lessons_detail([second, first], lessons) == detail


def test_fifo_spillover_takes_lessons_after_valid_from_only():
    a = les(2, valid_from=date(2026, 9, 1), created=0)
    b = les(2, valid_from=date(2026, 9, 1), paid_on=date(2026, 9, 2), created=1)
    lessons = [date(2026, 9, d) for d in (3, 6, 9, 12, 15)]
    detail = ps.allocate_lessons_detail([a, b], lessons)
    assert [taken for _, taken in detail] == [[date(2026, 9, 3), date(2026, 9, 6)], [date(2026, 9, 9), date(2026, 9, 12)]]


def test_unused_lessons_before_a_payment_are_not_carried_over():
    # payment A (valid from 1 Sep) is not exhausted; B starts later and cannot take A's lessons
    a = les(10, valid_from=date(2026, 9, 1), created=0)
    b = les(1, valid_from=date(2026, 10, 10), paid_on=date(2026, 10, 10), created=1)
    detail = ps.allocate_lessons_detail([a, b], [date(2026, 9, 5), date(2026, 10, 12)])
    # A takes both lessons (FIFO, both on/after 1 Sep); B finds nothing left
    assert [len(taken) for _, taken in detail] == [2, 0]


def test_mode_is_decided_by_the_latest_payment():
    old_calendar = cal(days(-60), paid_on=date(2026, 8, 1))
    new_lessons = les(12, valid_from=date(2026, 10, 1), paid_on=date(2026, 10, 1), created=5)
    assert ps.billing_mode([old_calendar, new_lessons]) == ps.BILLING_PACKAGE
    # and back: an older lessons payment is ignored once a calendar payment is the latest
    older_lessons = les(1, valid_from=date(2026, 8, 1), paid_on=date(2026, 8, 1))
    newer_calendar = cal(days(20), paid_on=date(2026, 10, 1), created=5)
    assert ps.billing_mode([older_lessons, newer_calendar]) == ps.BILLING_CALENDAR
    assert ps.billing_mode([]) is None


def test_latest_payment_ties_break_by_created_at():
    same_day = date(2026, 10, 1)
    c = PaymentRecord(paid_on=same_day, valid_from=same_day, valid_to=days(-5), created_at=datetime(2026, 10, 1, 10))
    l = PaymentRecord(paid_on=same_day, valid_from=same_day, lessons_count=12, created_at=datetime(2026, 10, 1, 11))
    assert ps.billing_mode([c, l]) == ps.BILLING_PACKAGE
    assert ps.billing_mode([l, c]) == ps.BILLING_PACKAGE


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


# ---- a payment that moved away with the student (N13 P2.1) ---------------------------


def at(day, hour=15, minute=0):
    """Tashkent wall time as the naive UTC instant the stamp is kept in (UTC+5)."""
    return datetime.combine(day, datetime.min.time()) + timedelta(hours=hour, minutes=minute) - timedelta(hours=5)


def moved(record, day, hour=15):
    return PaymentRecord(
        paid_on=record.paid_on, valid_from=record.valid_from, valid_to=record.valid_to,
        lessons_count=record.lessons_count, created_at=record.created_at,
        carried_out_at=at(day, hour),
    )


def test_calendar_payment_that_moved_ends_on_the_transfer_day_included():
    p = moved(cal(days(20), valid_from=days(-10)), days(0))
    assert p.end == days(0) and not p.covers_nothing
    # moved after its end: nothing changes; never extends it
    assert moved(cal(days(3), valid_from=days(-10)), days(9)).end == days(3)


def test_the_transfer_day_is_the_tashkent_date_of_the_instant():
    # 23:30 in Tashkent is still the same business day, although it is 18:30 UTC; 00:30 is the next one
    assert moved(cal(days(20), valid_from=days(-10)), days(0), hour=23).end == days(0)
    late_night = PaymentRecord(paid_on=days(-10), valid_from=days(-10), valid_to=days(20), carried_out_at=at(days(1), 0, 30))
    assert late_night.carried_out_day == days(1) and late_night.end == days(1)


def test_calendar_payment_that_moved_before_it_began_covers_nothing():
    p = moved(cal(days(30), paid_on=days(0), valid_from=days(5)), days(0))
    assert p.covers_nothing
    assert ps.payments_in_force([p]) == []
    # the carry-over (a plain record from T) is the only coverage
    carry = cal(days(30), paid_on=days(0), valid_from=days(5), created=5)
    assert ps.payments_in_force([p, carry]) == [carry]
    # a payment that starts on the transfer day itself still covers that day (a lesson before the instant)
    assert not moved(cal(days(30), paid_on=days(0), valid_from=days(0)), days(0)).covers_nothing


def test_lessons_payment_that_moved_takes_only_lessons_up_to_the_transfer_day():
    package = moved(les(6, valid_from=days(-20)), days(-3))
    dates = [days(-10), days(-8), days(-3), days(-1)]            # the last one is after the move
    detail = ps.allocate_lessons_detail([package], dates)
    assert detail[0][1] == [days(-10), days(-8), days(-3)]       # the lessons of the transfer day itself are still its own


def test_lessons_return_leaves_original_minus_consumed():
    # 6 lessons, 2 used in A before the transfer on day -3, 3 carried back: the carry-over takes none of A's old lessons
    original = moved(les(6, valid_from=days(-20)), days(-3))
    carry_back = les(3, valid_from=days(0), paid_on=days(-20), created=9)
    dates = [days(-10), days(-8)]                                 # A's own lessons
    detail = ps.allocate_lessons_detail([original, carry_back], dates)
    assert [len(taken) for _, taken in detail] == [2, 0]
    # lessons held in A after the student came back are taken by the carry-over
    detail = ps.allocate_lessons_detail([original, carry_back], dates + [days(1)])
    assert [len(taken) for _, taken in detail] == [2, 1]


def test_lessons_payment_moved_on_its_first_day():
    package = moved(les(6, valid_from=days(0)), days(0))
    taken = lambda payment, lessons: ps.allocate_lessons_detail([payment], lessons)[0][1]
    assert taken(package, [days(0)]) == [days(0)]                 # a lesson that morning is still its own
    assert taken(package, [days(1)]) == []
