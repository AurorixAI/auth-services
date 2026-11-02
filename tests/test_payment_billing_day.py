"""N13 P2.2: pure rules of the "one payment day" model, the amount maths,
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
