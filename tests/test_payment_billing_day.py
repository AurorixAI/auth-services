"""N13 P2.2: pure rules of the "one payment day" model, the amount maths, the short-period warning,
the amount in words and the cheque helpers (no database)."""
import os
from datetime import date, timedelta

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

from src.auth import payment_status as ps  # noqa: E402
from src.auth.money_words import amount_words, number_words  # noqa: E402

D = date


# ---- the centre's periods -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "day,billing_day,start,end",
    [
        # day 1: calendar months
        (D(2026, 10, 1), 1, D(2026, 10, 1), D(2026, 10, 31)),
        (D(2026, 10, 31), 1, D(2026, 10, 1), D(2026, 10, 31)),
        (D(2026, 11, 15), 1, D(2026, 11, 1), D(2026, 11, 30)),
        (D(2026, 12, 31), 1, D(2026, 12, 1), D(2026, 12, 31)),
        (D(2027, 1, 1), 1, D(2027, 1, 1), D(2027, 1, 31)),
        # day 10: 10.10 - 09.11
        (D(2026, 10, 10), 10, D(2026, 10, 10), D(2026, 11, 9)),
        (D(2026, 10, 9), 10, D(2026, 9, 10), D(2026, 10, 9)),
        (D(2026, 10, 25), 10, D(2026, 10, 10), D(2026, 11, 9)),
        # December -> January, and January -> the previous December
        (D(2026, 12, 15), 10, D(2026, 12, 10), D(2027, 1, 9)),
        (D(2027, 1, 5), 10, D(2026, 12, 10), D(2027, 1, 9)),
        (D(2027, 1, 5), 28, D(2026, 12, 28), D(2027, 1, 27)),
        # day 28: February, also in a leap year; 31.01
        (D(2027, 1, 31), 28, D(2027, 1, 28), D(2027, 2, 27)),
        (D(2027, 2, 27), 28, D(2027, 1, 28), D(2027, 2, 27)),
        (D(2028, 1, 31), 28, D(2028, 1, 28), D(2028, 2, 27)),
        (D(2028, 2, 28), 28, D(2028, 2, 28), D(2028, 3, 27)),
        # day 1 in February: 28 and 29 days
        (D(2027, 2, 14), 1, D(2027, 2, 1), D(2027, 2, 28)),
        (D(2028, 2, 14), 1, D(2028, 2, 1), D(2028, 2, 29)),
        (D(2027, 1, 31), 1, D(2027, 1, 1), D(2027, 1, 31)),
    ],
)
def test_billing_period_of_a_day(day, billing_day, start, end):
    assert ps.billing_period_start(day, billing_day) == start
    assert ps.billing_period_end(day, billing_day) == end
    # the end is the day before the next start
    assert ps.billing_period_start(end + timedelta(days=1), billing_day) == end + timedelta(days=1)
    assert ps.billing_period_end(start, billing_day) == end  # passing the start gives the same period


@pytest.mark.parametrize("bad", [0, 29, 31, -1])
def test_billing_day_is_one_to_twenty_eight(bad):
    with pytest.raises(ValueError):
        ps.billing_period_start(D(2026, 10, 15), bad)
    with pytest.raises(ValueError):
        ps.billing_period_end(D(2026, 10, 15), bad)


def test_billing_period_dates_out_of_range_raise_overflow():
    with pytest.raises(OverflowError):
        ps.billing_period_start(D(1, 1, 5), 10)  # the previous month is year 0
    with pytest.raises(OverflowError):
        ps.billing_period_end(D(9999, 12, 31), 1)  # the next start is year 10000


def test_billing_periods_that_intersect_a_range():
    assert ps.billing_periods(D(2026, 10, 15), D(2026, 10, 20), 1) == [(D(2026, 10, 1), D(2026, 10, 31))]
    assert ps.billing_periods(D(2026, 10, 15), D(2026, 11, 3), 1) == [
        (D(2026, 10, 1), D(2026, 10, 31)), (D(2026, 11, 1), D(2026, 11, 30))]
    assert ps.billing_periods(D(2026, 12, 20), D(2027, 1, 5), 10) == [
        (D(2026, 12, 10), D(2027, 1, 9))]
    assert ps.billing_periods(D(2026, 12, 5), D(2027, 1, 12), 10) == [
        (D(2026, 11, 10), D(2026, 12, 9)), (D(2026, 12, 10), D(2027, 1, 9)), (D(2027, 1, 10), D(2027, 2, 9))]
    assert ps.billing_periods(D(2026, 10, 5), D(2026, 10, 4), 1) == []
    # ends exactly on a period boundary: the next period does not intersect
    assert ps.billing_periods(D(2026, 10, 1), D(2026, 10, 31), 1) == [(D(2026, 10, 1), D(2026, 10, 31))]
    assert len(ps.billing_periods(D(2026, 1, 1), D(2026, 12, 31), 1)) == 12


@pytest.mark.parametrize(
    "valid_from,valid_to,billing_day,partial",
    [
        (D(2026, 10, 1), D(2026, 10, 31), 1, False),
        (D(2026, 10, 1), D(2026, 11, 30), 1, False),   # two whole periods
        (D(2026, 10, 15), D(2026, 10, 31), 1, True),   # came on the 15th
        (D(2026, 10, 1), D(2026, 10, 20), 1, True),    # left early
        (D(2026, 10, 15), D(2026, 11, 30), 1, True),
        (D(2026, 10, 10), D(2026, 11, 9), 10, False),
        (D(2026, 10, 10), D(2026, 11, 8), 10, True),
        (D(2026, 10, 5), D(2026, 11, 4), 10, True),    # an old "from the payment date" period under day 10
        (D(2026, 12, 10), D(2027, 1, 9), 10, False),   # December -> January
        (D(2028, 2, 1), D(2028, 2, 29), 1, False),     # leap February
        (D(2027, 2, 1), D(2027, 2, 28), 1, False),
        (D(2028, 2, 1), D(2028, 2, 28), 1, True),
        (D(2027, 1, 28), D(2027, 2, 27), 28, False),
        (D(2026, 10, 15), None, 1, False),             # a lessons package is never partial
    ],
)
def test_is_partial_period(valid_from, valid_to, billing_day, partial):
    assert ps.is_partial_period(valid_from, valid_to, billing_day) is partial


# ---- default period -----------------------------------------------------------------------------


def period(paid_on, previous_until, joined, billing_day):
    return ps.default_fixed_day_period(paid_on, previous_until, joined, billing_day)


def test_default_period_late_payment_closes_the_debt_first():
    # paid 02.11, the old period ended 31.10: the new one is November (nothing to skip)
    assert period(D(2026, 11, 2), D(2026, 10, 31), None, 1) == (D(2026, 11, 1), D(2026, 11, 30))
    # the old period ended 30.10: the debt starts on 31.10, the payment covers the rest of that period
    assert period(D(2026, 11, 2), D(2026, 10, 30), None, 1) == (D(2026, 10, 31), D(2026, 10, 31))
    # a long gap: no clamp to the period of the payment date any more, the debt is closed period by period
    assert period(D(2026, 11, 2), D(2026, 7, 31), None, 1) == (D(2026, 8, 1), D(2026, 8, 31))
    assert period(D(2026, 11, 12), D(2026, 10, 9), None, 10) == (D(2026, 10, 10), D(2026, 11, 9))
    # day 10, paid until 08.10, pays on 21.10: 09.10 is the last day of the old period 10.09-09.10
    assert period(D(2026, 10, 21), D(2026, 10, 8), None, 10) == (D(2026, 10, 9), D(2026, 10, 9))
    # day 10, paid until 20.08, pays on 21.10: debt starts inside the period 10.08-09.09
    assert period(D(2026, 10, 21), D(2026, 8, 20), None, 10) == (D(2026, 8, 21), D(2026, 9, 9))
    # day 1, paid until 12.10 (a partial period), pays on 21.10: the rest of October
    assert period(D(2026, 10, 21), D(2026, 10, 12), None, 1) == (D(2026, 10, 13), D(2026, 10, 31))


def test_default_period_too_old_previous_payment_degrades_to_the_first_payment_rule():
    # previous + 1 would be before paid_on minus 3 years: valid_from_error would refuse it, the suggestion must not
    assert period(D(2026, 10, 21), D(2023, 10, 19), None, 1) == (D(2026, 10, 1), D(2026, 10, 31))
    assert period(D(2026, 10, 21), D(2023, 10, 19), D(2020, 1, 1), 10) == (D(2026, 10, 10), D(2026, 11, 9))
    # exactly on the bound it is still used
    assert period(D(2026, 10, 21), D(2023, 10, 20), None, 1) == (D(2023, 10, 21), D(2023, 10, 31))


def test_default_period_prepayment_continues_without_a_gap():
    # still covered until 31.10, paid on 25.10: next is November
    assert period(D(2026, 10, 25), D(2026, 10, 31), None, 1) == (D(2026, 11, 1), D(2026, 11, 30))
    # two periods ahead: continues after the last paid day, not from the payment date
    assert period(D(2026, 10, 25), D(2026, 11, 30), None, 1) == (D(2026, 12, 1), D(2026, 12, 31))
    assert period(D(2026, 12, 20), D(2027, 1, 9), None, 10) == (D(2027, 1, 10), D(2027, 2, 9))
    # a previous partial period that ended mid-month: pay on the 5th, anchor is the 1st
    assert period(D(2026, 10, 5), D(2026, 10, 20), None, 1) == (D(2026, 10, 21), D(2026, 10, 31))


def test_default_period_first_payment_of_a_student_who_came_mid_period():
    # came on 15.10, pays the same day or two days later: 15.10 - 31.10
    assert period(D(2026, 10, 15), None, D(2026, 10, 15), 1) == (D(2026, 10, 15), D(2026, 10, 31))
    assert period(D(2026, 10, 17), None, D(2026, 10, 15), 1) == (D(2026, 10, 15), D(2026, 10, 31))
    # came on 25.09 and pays on 03.10: the whole of October (September is for the administrator to edit)
    assert period(D(2026, 10, 3), None, D(2026, 9, 25), 1) == (D(2026, 10, 1), D(2026, 10, 31))
    # day 10: came on 15.10 -> 15.10 - 09.11; came on 05.10, pays 12.10 -> 10.10 - 09.11
    assert period(D(2026, 10, 15), None, D(2026, 10, 15), 10) == (D(2026, 10, 15), D(2026, 11, 9))
    assert period(D(2026, 10, 12), None, D(2026, 10, 5), 10) == (D(2026, 10, 10), D(2026, 11, 9))
    # came on the first day of the period
    assert period(D(2026, 10, 1), None, D(2026, 10, 1), 1) == (D(2026, 10, 1), D(2026, 10, 31))
    # no joining date: the payment date
    assert period(D(2026, 10, 15), None, None, 1) == (D(2026, 10, 15), D(2026, 10, 31))


def test_default_period_respects_the_joining_date_also_when_old_payments_exist():
    # left and came back on the 15th: old payment ended 30.09, the new period starts on the return day
    assert period(D(2026, 10, 17), D(2026, 9, 30), D(2026, 10, 15), 1) == (D(2026, 10, 15), D(2026, 10, 31))
    assert period(D(2026, 10, 15), D(2026, 7, 31), D(2026, 10, 15), 10) == (D(2026, 10, 15), D(2026, 11, 9))
    # returned in the next period after the old payment ended
    assert period(D(2026, 11, 20), D(2026, 10, 31), D(2026, 11, 20), 1) == (D(2026, 11, 20), D(2026, 11, 30))
    # a continuing student (joined long ago) is not affected by the joining date: late payment, prepayment, a gap
    long_ago = D(2020, 1, 1)
    assert period(D(2026, 11, 2), D(2026, 10, 31), long_ago, 1) == (D(2026, 11, 1), D(2026, 11, 30))
    assert period(D(2026, 10, 25), D(2026, 10, 31), long_ago, 1) == (D(2026, 11, 1), D(2026, 11, 30))
    assert period(D(2026, 11, 2), D(2026, 7, 31), long_ago, 1) == (D(2026, 8, 1), D(2026, 8, 31))
    # joined before the previous period ended (a transfer membership): the carry-over's end dominates
    assert period(D(2026, 10, 25), D(2026, 11, 9), D(2026, 10, 12), 10) == (D(2026, 11, 10), D(2026, 12, 9))


def test_default_period_edges_december_february_and_day_28():
    assert period(D(2026, 12, 31), None, D(2026, 12, 31), 1) == (D(2026, 12, 31), D(2026, 12, 31))
    assert period(D(2027, 1, 3), D(2026, 12, 31), None, 1) == (D(2027, 1, 1), D(2027, 1, 31))
    assert period(D(2028, 2, 10), D(2028, 1, 31), None, 1) == (D(2028, 2, 1), D(2028, 2, 29))
    assert period(D(2027, 2, 10), D(2027, 1, 31), None, 1) == (D(2027, 2, 1), D(2027, 2, 28))
    assert period(D(2027, 1, 31), None, D(2027, 1, 31), 28) == (D(2027, 1, 31), D(2027, 2, 27))
    assert period(D(2028, 2, 28), D(2028, 2, 27), None, 28) == (D(2028, 2, 28), D(2028, 3, 27))


# ---- amounts ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "numerator,denominator,expected",
    [
        (500_000 * 6, 12, 250_000),
        (500_000 * 5, 12, 208_000),            # 208 333 -> 208 000
        (500_000 * 7, 12, 292_000),            # 291 666 -> 292 000
        (450_000 * 1, 3, 150_000),
        (1_500, 1, 2_000),                     # a half goes up
        (2_500, 1, 3_000),
        (1_499, 1, 1_000),
        (0, 12, 0),
        (333_333 * 1, 1, 333_000),
        (1_000_000_000_000_001, 1, 1_000_000_000_000_000),
    ],
)
def test_round_1000_half_up(numerator, denominator, expected):
    assert ps.round_1000(numerator, denominator) == expected


def lessons_of(days):
    return lambda start, end: [d for d in days if start <= d <= end]


def weekdays(start, end, iso_days):
    out, day = [], start
    while day <= end:
        if day.isoweekday() in iso_days:
            out.append(day)
        day += timedelta(days=1)
    return out


MWF = {1, 3, 5}
OCTOBER = weekdays(D(2026, 10, 1), D(2026, 10, 31), MWF)  # Mon/Wed/Fri of October 2026: 13 lessons


def test_full_periods_cost_the_fee_and_ask_nothing():
    calls = []
    parts = ps.price_fixed_day(
        D(2026, 10, 1), D(2026, 11, 30), 1, 500_000, lambda a, b: calls.append((a, b)) or [])
    assert [p.amount for p in parts] == [500_000, 500_000] and calls == []
    assert all(p.basis == "full" and p.is_full and p.lessons is None for p in parts)
    assert ps.quote_basis(parts) == "full"


def test_partial_period_is_prorated_by_lessons():
    assert len(OCTOBER) == 13
    parts = ps.price_fixed_day(D(2026, 10, 15), D(2026, 10, 31), 1, 500_000, lessons_of(OCTOBER))
    (part,) = parts
    in_part = [d for d in OCTOBER if d >= D(2026, 10, 15)]
    assert (part.lessons, part.period_lessons, part.basis) == (len(in_part), 13, "lessons")
    assert part.amount == ps.round_1000(500_000 * len(in_part), 13)
    assert (part.start, part.end, part.period_start, part.period_end) == (
        D(2026, 10, 15), D(2026, 10, 31), D(2026, 10, 1), D(2026, 10, 31))
    assert not part.is_full and ps.quote_basis(parts) == "lessons"


def test_partial_period_example_from_the_spec_six_of_twelve():
    days = [D(2026, 10, d) for d in (1, 3, 5, 7, 9, 11, 15, 17, 19, 21, 23, 25)]   # 12 lessons, 6 on or after the 15th
    (part,) = ps.price_fixed_day(D(2026, 10, 15), D(2026, 10, 31), 1, 500_000, lessons_of(days))
    assert (part.lessons, part.period_lessons, part.amount) == (6, 12, 250_000)


def test_a_range_over_two_periods_prices_each_one():
    nov = weekdays(D(2026, 11, 1), D(2026, 11, 30), MWF)
    parts = ps.price_fixed_day(D(2026, 10, 15), D(2026, 11, 30), 1, 500_000, lessons_of(OCTOBER + nov))
    assert [p.basis for p in parts] == ["lessons", "full"]
    assert parts[1].amount == 500_000 and parts[1].is_full
    # a payment that ends mid-period prices that last period by lessons too
    parts = ps.price_fixed_day(D(2026, 10, 1), D(2026, 11, 15), 1, 500_000, lessons_of(OCTOBER + nov))
    assert [p.basis for p in parts] == ["full", "lessons"]


def test_cancelled_lessons_are_not_in_the_lesson_list_so_they_do_not_count():
    # the service removes cancelled lessons before the list reaches the maths: dropping one changes the ratio
    kept = [d for d in OCTOBER if d != D(2026, 10, 21)]
    (a,) = ps.price_fixed_day(D(2026, 10, 15), D(2026, 10, 31), 1, 500_000, lessons_of(OCTOBER))
    (b,) = ps.price_fixed_day(D(2026, 10, 15), D(2026, 10, 31), 1, 500_000, lessons_of(kept))
    assert (a.lessons, a.period_lessons) == (b.lessons + 1, b.period_lessons + 1)


def test_period_without_lessons_falls_back_to_calendar_days():
    (part,) = ps.price_fixed_day(D(2026, 10, 15), D(2026, 10, 31), 1, 310_000, lessons_of([]))
    assert part.basis == "days" and part.lessons is None and part.period_lessons is None
    assert part.amount == ps.round_1000(310_000 * 17, 31) == 170_000
    assert ps.quote_basis([part]) == "days"


def test_lessons_basis_wins_over_days_in_a_mixed_quote():
    parts = [
        ps.QuotePart(D(2026, 10, 15), D(2026, 10, 31), D(2026, 10, 1), D(2026, 10, 31), None, None, 1000, "days"),
        ps.QuotePart(D(2026, 11, 1), D(2026, 11, 10), D(2026, 11, 1), D(2026, 11, 30), 2, 12, 1000, "lessons"),
    ]
    assert ps.quote_basis(parts) == "lessons"


def test_a_student_with_no_lessons_left_in_the_period_costs_zero():
    only_early = [D(2026, 10, 1), D(2026, 10, 3)]
    (part,) = ps.price_fixed_day(D(2026, 10, 20), D(2026, 10, 31), 1, 500_000, lessons_of(only_early))
    assert (part.lessons, part.period_lessons, part.amount) == (0, 2, 0)


# ---- the short-period warning ---------------------------------------------------------------------


TODAY = D(2026, 10, 15)


def codes(valid_from, valid_to, amount, fee):
    return [w.code for w in ps.payment_warnings(
        today=TODAY, paid_on=TODAY, valid_from=valid_from, valid_to=valid_to, existing=[],
        amount=amount, monthly_fee=fee)]


def test_short_period_at_full_amount_warns():
    assert codes(D(2026, 10, 15), D(2026, 10, 20), 450_000, 450_000) == ["short_period_full_amount"]   # 6 days
    assert codes(D(2026, 10, 15), D(2026, 10, 20), 500_000, 450_000) == ["short_period_full_amount"]   # more than the fee
    assert codes(D(2026, 10, 15), D(2026, 10, 15), 450_000, 450_000) == ["short_period_full_amount"]   # one day
    message = ps.payment_warnings(
        today=TODAY, paid_on=TODAY, valid_from=TODAY, valid_to=TODAY, existing=[], amount=5, monthly_fee=5)[0].message
    assert message == "Короткий период при полной сумме — проверьте даты"


def test_short_period_does_not_warn_when_it_is_fine():
    assert codes(D(2026, 10, 15), D(2026, 10, 21), 450_000, 450_000) == []        # exactly 7 days
    assert codes(D(2026, 10, 15), D(2026, 10, 20), 200_000, 450_000) == []        # a prorated amount
    assert codes(D(2026, 10, 15), D(2026, 10, 20), 450_000, None) == []           # no fee set
    assert codes(D(2026, 10, 15), D(2026, 10, 20), 0, 0) == []                    # a free membership
    assert codes(D(2026, 10, 15), None, 450_000, 450_000) == []                   # a lessons package
    assert [w.code for w in ps.payment_warnings(                                  # old callers pass neither
        today=TODAY, paid_on=TODAY, valid_from=TODAY, valid_to=TODAY, existing=[])] == []


# ---- the amount in words --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "amount,words",
    [
        (0, "Ноль сум"),
        (1, "Один сум"),
        (2, "Два сум"),
        (5, "Пять сум"),
        (11, "Одиннадцать сум"),
        (14, "Четырнадцать сум"),
        (20, "Двадцать сум"),
        (21, "Двадцать один сум"),
        (100, "Сто сум"),
        (101, "Сто один сум"),
        (112, "Сто двенадцать сум"),
        (999, "Девятьсот девяносто девять сум"),
        (1000, "Одна тысяча сум"),
        (1001, "Одна тысяча один сум"),
        (2000, "Две тысячи сум"),
        (3000, "Три тысячи сум"),
        (5000, "Пять тысяч сум"),
        (11000, "Одиннадцать тысяч сум"),
        (12000, "Двенадцать тысяч сум"),
        (21000, "Двадцать одна тысяча сум"),
        (22000, "Двадцать две тысячи сум"),
        (25000, "Двадцать пять тысяч сум"),
        (101000, "Сто одна тысяча сум"),
        (111000, "Сто одиннадцать тысяч сум"),
        (250000, "Двести пятьдесят тысяч сум"),
        (450000, "Четыреста пятьдесят тысяч сум"),
        (500000, "Пятьсот тысяч сум"),
        (999999, "Девятьсот девяносто девять тысяч девятьсот девяносто девять сум"),
        (1000000, "Один миллион сум"),
        (1250000, "Один миллион двести пятьдесят тысяч сум"),
        (2000000, "Два миллиона сум"),
        (2000002, "Два миллиона два сум"),
        (5000000, "Пять миллионов сум"),
        (11000000, "Одиннадцать миллионов сум"),
        (21000000, "Двадцать один миллион сум"),
        (1001000, "Один миллион одна тысяча сум"),
        (1000000000, "Один миллиард сум"),
        (10000000000, "Десять миллиардов сум"),
        (10_000_000_000 + 500, "Десять миллиардов пятьсот сум"),
        (999_999_999_999,
         "Девятьсот девяносто девять миллиардов девятьсот девяносто девять миллионов "
         "девятьсот девяносто девять тысяч девятьсот девяносто девять сум"),
    ],
)
def test_amount_in_words(amount, words):
    assert amount_words(amount) == words


def test_amount_in_words_rejects_what_it_cannot_say():
    for bad in (-1, 1_000_000_000_000):
        with pytest.raises(ValueError):
            amount_words(bad)
    with pytest.raises(TypeError):
        amount_words(1.5)
    with pytest.raises(TypeError):
        amount_words(True)


def test_number_words_is_lower_case_and_complete_for_every_unit():
    assert number_words(0) == "ноль" and number_words(40) == "сорок"
    # every number below 1000 reads without a double space, a leading/trailing space or an upper-case letter
    for n in range(1, 1000):
        text = number_words(n)
        assert text == text.strip() and "  " not in text and text == text.lower(), (n, text)
