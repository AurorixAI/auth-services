"""Money-1 Г-7: ONE rounding of the money a student owes (``payment_status.prorated``): a part of a period never costs more than the whole, is a whole number of
thousands wherever the price is, and a stay of no day without a lesson costs nothing.  Numbers: MONEY1_G1_REVIEW N10, ДЫРА-11, ДЫРА-07."""
from datetime import timedelta

import pytest

from src.auth import billing_ledger as bl
from src.auth import payment_status as ps
from tests.pay2_ledger_helpers import D, flat, make, weekly


def led(**kw):
    return bl.run_ledger(make(**kw))


@pytest.mark.parametrize("price", [450_500, 300_000, 499_999, 500_000, 1_000, 1])
@pytest.mark.parametrize("whole", [1, 4, 12, 13, 21, 30, 31])
def test_a_part_is_never_more_than_the_whole_never_less_than_a_smaller_part_and_round_where_the_price_is(price, whole):
    parts = [ps.prorated(price, k, whole) for k in range(0, whole + 1)]
    assert parts[0] == 0 and parts[-1] == price
    assert all(a <= b <= price for a, b in zip(parts, parts[1:]))
    if price % 1000 == 0:
        assert all(p % 1000 == 0 for p in parts)


def test_n10_a_part_that_holds_every_lesson_of_the_month_costs_the_price_not_a_thousand_more():
    # 450 500 a month, Tuesdays (03, 10, 17, 24.11: 4 of 4), he joined 02.11 - the partial cell used to be 451 000, more than the full 450 500
    l = led(today=D(2026, 11, 20), stays=[bl.Stay(D(2026, 11, 2))], price_at=flat(450_500), lesson_days=weekly({1}),
            regimes=[bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)])
    first = l.segments[0]
    assert (first.basis, first.lessons_in_part, first.lessons_in_period) == ("lessons", 4, 4)
    assert first.amount == 450_500


def test_dyra11_a_partial_package_is_a_whole_number_of_thousands_and_a_whole_one_is_the_price():
    # 500 000 / 12 lessons, 5 held: 208 333 used to be charged
    counted = [bl.CountedLesson(D(2026, 10, 1) + timedelta(days=i), f"l{i}") for i in range(5)]
    five = led(today=D(2026, 10, 20), stays=[bl.Stay(D(2026, 9, 1))], price_at=flat(500_000),
               regimes=[bl.Regime(mode=bl.LESSONS, package_size=12)], counted=counted)
    assert five.segments[0].amount == 208_000 and five.debt == 208_000
    counted += [bl.CountedLesson(D(2026, 10, 6) + timedelta(days=i), f"m{i}") for i in range(7)]
    full = led(today=D(2026, 10, 20), stays=[bl.Stay(D(2026, 9, 1))], price_at=flat(500_000),
               regimes=[bl.Regime(mode=bl.LESSONS, package_size=12)], counted=counted)
    assert full.segments[0].amount == 500_000


def test_dyra07_added_and_removed_the_same_day_without_a_lesson_owes_nothing():
    # 300 000 a month of the student's own days: 1/31 = 10 000 used to be a debt for a day that had no lesson
    ghost = led(today=D(2026, 10, 20), stays=[bl.Stay(D(2026, 10, 10), D(2026, 10, 10))], price_at=flat(300_000), lesson_days=weekly({0}))   # 10.10.2026 is a Saturday
    assert ghost.debt == 0 and ghost.charged == 0


def test_dyra07_the_same_day_with_a_lesson_is_still_a_day_of_the_student():
    came = led(today=D(2026, 10, 20), stays=[bl.Stay(D(2026, 10, 10), D(2026, 10, 10))], price_at=flat(300_000), lesson_days=weekly({5}))
    assert came.debt == 10_000
