"""PAY-2 stream A: edge cases of ``billing_ledger.run_ledger`` (design §7 table)."""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.auth import billing_ledger as bl
from src.auth import payment_status as ps

from src.auth.billing_ledger import round_1000
from tests.pay2_ledger_helpers import D, T0, credit, e1, flat, make, outflow, weekly

FIXED1 = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)


def led(**kw):
    return bl.run_ledger(make(**kw))


# ---- floor of days -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("money,days_paid", [(0, 0), (1, 0), (16_666, 0), (16_667, 1), (483_333, 28),
                                              (483_334, 29), (499_999, 29), (500_000, 30)])
def test_a_day_is_paid_only_when_paid_in_full(money, days_paid):
    # price 500 000 over 30 days (15.09-14.10): one day = 16 666.67
    ledger = bl.run_ledger(make(
        today=D(2026, 9, 20), stays=[bl.Stay(D(2026, 9, 15))], price_at=flat(500_000),
        credits=[credit("p", D(2026, 9, 15), money)] if money else []))
    first = ledger.segments[0]
    assert first.covered_days == days_paid
    if days_paid == 30:
        assert first.partial_credit == 0
    else:  # the partial day is what is left over the paid days (the design formula; a unit of rounding at most)
        assert first.partial_credit == money - (days_paid * 500_000) // 30
        assert 0 <= first.partial_credit <= 16_667


# ---- prices --------------------------------------------------------------------------------------------------


def test_price_zero_is_free_and_never_a_debt():
    l = led(today=D(2026, 10, 21), stays=[bl.Stay(D(2026, 9, 15))], price_at=flat(0))
    assert l.status.status == ps.FREE and l.debt == 0 and l.charged == 0
    assert all(s.basis == "zero" and s.covered_days == len(s.days) for s in l.segments)
    assert l.lesson_paid(D(2026, 10, 1))


def test_missing_price_charges_nothing_and_the_money_waits_as_advance():
    l = led(today=D(2026, 10, 21), stays=[bl.Stay(D(2026, 9, 15))], price_at=flat(None),
            credits=[credit("p", D(2026, 10, 1), 300_000)])
    assert l.flags == (bl.F_PRICE_MISSING,) and l.unpriced_from == D(2026, 9, 15)
    assert (l.debt, l.charged, l.advance) == (0, 0, 300_000)
    assert [s.state for s in l.segments] == ["unpriced", "unpriced"] and all(s.amount == 0 for s in l.segments)
    assert l.reconciles and not l.lesson_paid(D(2026, 10, 2)) and l.covered_through is None


def test_a_price_that_starts_later_skips_the_unpriced_periods_and_charges_from_the_first_priced_one():
    # no price before 01.10: the period of 15.09 is unpriced (skipped), the one of 15.10 is charged
    l = led(today=D(2026, 10, 21), stays=[bl.Stay(D(2026, 9, 15))], price_at=flat(500_000, since=D(2026, 10, 1)))
    assert l.unpriced_from == D(2026, 9, 15) and l.debt == 500_000 and bl.F_PRICE_MISSING in l.flags
    assert [s.state for s in l.segments if s.begun] == ["unpriced", "open"]


def test_an_unpriced_closed_run_does_not_break_the_cover_of_the_priced_run_after_it():
    """Stay 05.09-20.09 before the price (valid from 01.10), back on 10.10, 500 000 paid on 10.10: paid, not NONE."""
    l = led(today=D(2026, 10, 21), stays=[bl.Stay(D(2026, 9, 5), D(2026, 9, 20)), bl.Stay(D(2026, 10, 10))],
            price_at=flat(500_000, since=D(2026, 10, 1)), credits=[credit("p", D(2026, 10, 10), 500_000)])
    assert l.covered_through == D(2026, 11, 9) and l.status.status == ps.ACTIVE and l.debt == 0
    assert l.lesson_paid(D(2026, 10, 12)) and bl.F_PRICE_MISSING in l.flags
    (rec,) = bl.coverage_records(l)
    assert rec.valid_to == D(2026, 11, 9)


def test_the_price_of_a_partial_first_period_is_the_price_of_its_first_billable_day():
    """Fixed day 1, price valid from 15.10, joined 15.10: October is charged pro rata by the lessons of the group."""
    l = led(today=D(2026, 10, 21), stays=[bl.Stay(D(2026, 10, 15))], price_at=flat(600_000, since=D(2026, 10, 15)),
            regimes=[FIXED1], lesson_days=weekly({0, 2, 4}))
    first = l.segments[0]
    assert (first.period_start, first.days[0]) == (D(2026, 10, 1), D(2026, 10, 15)) and first.state != "unpriced"
    # Oct 2026 Mon/Wed/Fri: 13 lessons; from the 15th: 16, 19, 21, 23, 26, 28, 30 = 7
    assert (first.basis, first.lessons_in_part, first.lessons_in_period) == ("lessons", 7, 13)
    assert first.amount == round_1000(600_000 * 7, 13) == 323_000 and first.price.amount == 600_000 and l.debt == 323_000
    # the same in payment-date mode: the period starts on the first billable day anyway
    l = led(today=D(2026, 10, 21), stays=[bl.Stay(D(2026, 10, 15))], price_at=flat(600_000, since=D(2026, 10, 15)))
    assert l.debt == 600_000


def test_a_segment_of_a_membership_made_by_a_transfer_is_begun_only_from_its_first_billable_day():
    """Fixed day: the cell starts 01.10 but the student is charged from 12.10; a later 'today' is not a debt before it."""
    l = led(today=D(2026, 10, 5), stays=[bl.Stay(D(2026, 10, 11))], price_at=flat(400_000), regimes=[FIXED1],
            floor=D(2026, 10, 12))
    assert l.charged == 0 and l.debt == 0 and not l.segments[0].begun
    l = led(today=D(2026, 10, 12), stays=[bl.Stay(D(2026, 10, 11))], price_at=flat(400_000), regimes=[FIXED1],
            floor=D(2026, 10, 12))
    assert l.charged == 258_000


def test_price_is_taken_at_the_start_of_each_period():
    l = bl.run_ledger(e1(price_at=flat(500_000, changes={D(2026, 10, 10): 700_000})))
    assert [(s.period_start.isoformat(), s.amount) for s in l.segments[:3]] == [
        ("2026-09-15", 500_000), ("2026-10-15", 700_000), ("2026-11-15", 700_000)][:len(l.segments[:3])]


# ---- periods -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("anchor_day,expected", [
    (29, ["2026-01-29", "2026-02-28", "2026-03-29"]),
    (30, ["2026-01-30", "2026-02-28", "2026-03-30"]),
    (31, ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"]),
])
def test_payment_date_anchor_29_to_31(anchor_day, expected):
    l = led(today=D(2026, 3, 10), stays=[bl.Stay(D(2026, 1, anchor_day))], price_at=flat(100_000), start=D(2026, 1, 1))
    assert [s.period_start.isoformat() for s in l.segments if s.begun] == expected[:len([s for s in l.segments if s.begun])]
    # each period ends the day before the next one starts
    for a, b in zip(l.segments, l.segments[1:]):
        assert a.period_end + timedelta(days=1) == b.period_start


def test_payment_date_leap_february():
    l = led(today=D(2028, 3, 20), stays=[bl.Stay(D(2028, 1, 31))], price_at=flat(100_000), start=D(2028, 1, 1))
    assert [s.period_start.isoformat() for s in l.segments if s.begun] == ["2028-01-31", "2028-02-29", "2028-03-31"][:2]
    assert l.segments[1].period_end == D(2028, 3, 30)


@pytest.mark.parametrize("day", [1, 10, 28])
def test_fixed_day_grid(day):
    reg = bl.Regime(anchor=ps.FIXED_DAY, billing_day=day)
    l = led(today=D(2026, 10, 25), stays=[bl.Stay(D(2026, 9, 3))], price_at=flat(300_000), regimes=[reg])
    starts = [s.period_start for s in l.segments if s.begun]
    assert all(s.day == day for s in starts[1:]) and starts[0] == ps.billing_period_start(D(2026, 9, 3), day)
    first = l.segments[0]
    assert first.days[0] == D(2026, 9, 3)
    assert first.amount == (300_000 if first.period_start == D(2026, 9, 3) else first.amount)


def test_fixed_day_incomplete_period_without_lessons_falls_back_to_days_and_zero_lessons_charge_nothing():
    # lessons exist but none in the student's part -> charge 0
    reg = FIXED1
    l = led(today=D(2026, 10, 21), stays=[bl.Stay(D(2026, 10, 20))], price_at=flat(600_000), regimes=[reg],
            lesson_days=lambda a, b: [D(2026, 10, 5), D(2026, 10, 12)])
    s = l.segments[0]
    assert (s.amount, s.basis, s.lessons_in_part, s.lessons_in_period) == (0, "lessons", 0, 2)
    assert s.covered_days == len(s.days)  # nothing to pay -> paid


def test_accounting_start_cuts_the_first_period_and_grace_covers_the_lessons_before_it():
    l = led(today=D(2026, 10, 21), stays=[bl.Stay(D(2026, 9, 1))], price_at=flat(500_000), start=D(2026, 10, 1),
            regimes=[FIXED1])
    assert l.first_billable == D(2026, 10, 1) and l.segments[0].days[0] == D(2026, 10, 1)
    assert l.lesson_paid(D(2026, 9, 20)) is True      # before the accounting start: grace
    assert l.lesson_paid(D(2026, 10, 3)) is False     # a charged day with no money
    l = led(today=D(2026, 10, 21), stays=[bl.Stay(D(2026, 9, 1))], price_at=flat(500_000), start=D(2026, 10, 1),
            regimes=[FIXED1], credits=[credit("p", D(2026, 10, 2), 500_000)])
    assert l.lesson_paid(D(2026, 10, 31)) and not l.lesson_paid(D(2026, 11, 1))


def test_gap_in_payment_date_mode_starts_a_new_chain():
    l = led(today=D(2026, 10, 25), stays=[bl.Stay(D(2026, 9, 1), D(2026, 9, 10)), bl.Stay(D(2026, 9, 20))],
            price_at=flat(300_000))
    firsts = [(s.period_start.isoformat(), len(s.days)) for s in l.segments if s.begun]
    assert firsts[0] == ("2026-09-01", 10) and firsts[1] == ("2026-09-20", 30)
    assert l.segments[0].basis == "days" and l.segments[0].amount == 100_000   # 10 of 30 days of 300 000


def test_touching_and_overlapping_stays_are_one_run():
    assert bl.merge_stays([bl.Stay(D(2026, 1, 1), D(2026, 1, 10)), bl.Stay(D(2026, 1, 11), D(2026, 1, 20)),
                           bl.Stay(D(2026, 1, 15), D(2026, 1, 18)), bl.Stay(D(2026, 2, 1))]) == [
        (D(2026, 1, 1), D(2026, 1, 20)), (D(2026, 2, 1), None)]


def test_one_day_stay_is_one_billable_day():
    l = led(today=D(2026, 10, 21), stays=[bl.Stay(D(2026, 10, 5), D(2026, 10, 5))], price_at=flat(310_000), regimes=[FIXED1])
    assert len(l.segments[0].days) == 1 and l.segments[0].amount == 10_000


# ---- horizon and big numbers ---------------------------------------------------------------------------------


def test_horizon_is_capped_at_24_future_periods():
    l = led(today=D(2026, 10, 21), stays=[bl.Stay(D(2026, 9, 15))], price_at=flat(1),
            credits=[credit("p", D(2026, 10, 1), 10_000_000_000)])
    future = [s for s in l.segments if not s.begun]
    assert len(future) == bl.MAX_FUTURE_PERIODS and bl.F_HORIZON in l.flags
    assert l.advance == 10_000_000_000 - l.charged and l.reconciles
    assert l.allocations[-1].segment_id == bl.ADVANCE_SEGMENT
    assert l.covered_through == future[-1].days[-1]


def test_generation_stops_when_the_money_is_exhausted():
    l = led(today=D(2026, 10, 21), stays=[bl.Stay(D(2026, 9, 15))], price_at=flat(500_000),
            credits=[credit("p", D(2026, 10, 1), 2_200_000)])
    assert [s.period_start.isoformat() for s in l.segments] == [
        "2026-09-15", "2026-10-15", "2026-11-15", "2026-12-15", "2027-01-15"]
    # begun charges 1 000 000; the rest of the 2 200 000 is prepaid money on the 3 future periods that were generated
    assert (l.debt, l.advance, l.flags) == (0, 1_200_000, ())


# ---- outflows ------------------------------------------------------------------------------------------------


def test_refund_takes_the_newest_payments_first():
    l = bl.run_ledger(make(today=D(2026, 10, 25), stays=[bl.Stay(D(2026, 9, 1), D(2026, 9, 30))], price_at=flat(100_000),
                           credits=[credit("a", D(2026, 9, 1), 100_000), credit("b", D(2026, 9, 5), 100_000, minutes=1),
                                    credit("c", D(2026, 9, 6), 100_000, minutes=2)],
                           outflows=[outflow("r", D(2026, 10, 1), 150_000)]))
    assert l.refunded_from == {"a": 0, "b": 50_000, "c": 100_000} and l.net_money == {"a": 100_000, "b": 50_000, "c": 0}
    assert (l.charged, l.advance, l.debt) == (100_000, 50_000, 0) and l.reconciles


def test_refund_only_takes_money_dated_before_it():
    l = bl.run_ledger(make(today=D(2026, 10, 25), stays=[bl.Stay(D(2026, 9, 1), D(2026, 9, 30))], price_at=flat(100_000),
                           credits=[credit("a", D(2026, 9, 1), 100_000), credit("late", D(2026, 10, 20), 80_000, minutes=1)],
                           outflows=[outflow("r", D(2026, 10, 1), 50_000)]))
    assert l.refunded_from == {"a": 50_000, "late": 0}


def test_an_outflow_above_the_money_is_flagged_not_fatal():
    l = bl.run_ledger(make(today=D(2026, 10, 25), stays=[bl.Stay(D(2026, 9, 1))], price_at=flat(100_000),
                           credits=[credit("a", D(2026, 9, 1), 100_000)], outflows=[outflow("r", D(2026, 9, 5), 500_000)]))
    assert bl.F_OUTFLOW_EXCEEDS in l.flags and l.net_money == {"a": 0}


def test_writeoff_is_not_refundable_and_not_taken_by_refunds():
    l = bl.run_ledger(make(today=D(2026, 10, 25), stays=[bl.Stay(D(2026, 10, 1), D(2026, 10, 10))], price_at=flat(310_000),
                           regimes=[FIXED1], credits=[credit("w", D(2026, 10, 5), 200_000, bl.WRITEOFF),
                                                      credit("p", D(2026, 10, 6), 100_000)]))
    # charge: 10 days of 31 -> 100 000; credits 300 000; 200 000 is advance but only the money part is refundable
    assert (l.advance, l.refundable) == (200_000, 100_000)


# ---- modes and regimes ---------------------------------------------------------------------------------------


def test_mode_change_in_the_middle_of_a_month_cuts_the_period_and_never_double_charges():
    r1 = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
    r2 = bl.Regime(effective_from=D(2026, 10, 16), anchor=ps.FIXED_DAY, billing_day=10)
    l = led(today=D(2026, 10, 20), stays=[bl.Stay(D(2026, 9, 1))], price_at=flat(310_000), regimes=[r1, r2], start=D(2026, 9, 1))
    days = [d for s in l.segments for d in s.days]
    assert len(days) == len(set(days)) and days[0] == D(2026, 9, 1)
    assert l.reconciles
    october = [s for s in l.segments if s.days and s.days[0].month == 10]
    assert (october[0].days[0], october[0].days[-1]) == (D(2026, 10, 1), D(2026, 10, 15))    # the old grid's cell is cut on the day before the change
    assert october[1].days[0] == D(2026, 10, 16)                                                 # the new grid starts the day of the change


def test_calendar_then_lessons_regime():
    r1 = bl.Regime()
    r2 = bl.Regime(effective_from=D(2026, 10, 1), mode=bl.LESSONS, package_size=4)
    counted = [bl.CountedLesson(D(2026, 10, d), f"l{d}") for d in (2, 5, 7, 9, 12)]
    l = led(today=D(2026, 10, 15), stays=[bl.Stay(D(2026, 9, 1))], price_at=flat(400_000), regimes=[r1, r2],
            counted=counted, credits=[credit("p", D(2026, 9, 5), 500_000)])
    assert l.mode == bl.LESSONS and l.lessons is not None and l.reconciles
    assert [s.kind for s in l.segments][-2:] == ["package", "package"]
    assert l.lessons.used == 5 and l.lessons.covered >= 0


def test_lesson_packages_cost_per_lesson_adds_up_to_the_price():
    # 100 000 / 12 lessons does not divide: the remainder lies in the first lessons, the package costs exactly the price
    counted = [bl.CountedLesson(D(2026, 10, 1) + timedelta(days=i), f"l{i}") for i in range(12)]
    l = led(today=D(2026, 10, 20), stays=[bl.Stay(D(2026, 9, 1))], price_at=flat(100_000),
            regimes=[bl.Regime(mode=bl.LESSONS, package_size=12)], counted=counted)
    assert l.segments[0].amount == 100_000 and l.debt == 100_000


def test_lessons_price_change_between_packages_and_partial_lesson():
    counted = [bl.CountedLesson(D(2026, 10, 1) + timedelta(days=i), f"l{i}") for i in range(14)]
    l = led(today=D(2026, 10, 20), stays=[bl.Stay(D(2026, 9, 1))],
            price_at=flat(120_000, changes={D(2026, 10, 13): 240_000}),
            regimes=[bl.Regime(mode=bl.LESSONS, package_size=12)], counted=counted,
            credits=[credit("p", D(2026, 10, 20), 125_000)])
    # package 1 at 120 000 (10 000 a lesson), package 2 starts 13.10 at 240 000 (20 000 a lesson)
    assert [s.amount for s in l.segments] == [120_000, 40_000] and l.charged == 160_000
    assert (l.lessons.covered, l.lessons.unpaid, l.debt, l.partial_credit) == (12, 2, 35_000, 5_000)


def test_two_lessons_on_one_day_are_two_lessons():
    counted = [bl.CountedLesson(D(2026, 10, 1), "a"), bl.CountedLesson(D(2026, 10, 1), "b"), bl.CountedLesson(D(2026, 10, 2), "c")]
    l = led(today=D(2026, 10, 20), stays=[bl.Stay(D(2026, 9, 1))], price_at=flat(30_000),
            regimes=[bl.Regime(mode=bl.LESSONS, package_size=3)], counted=counted, credits=[credit("p", D(2026, 10, 5), 20_000)])
    assert l.lessons.covered == 2
    assert [l.lesson_paid(c.day, c.lesson_id) for c in counted] == [True, True, False]


def test_lessons_regime_without_price_has_no_packages_and_no_debt():
    counted = [bl.CountedLesson(D(2026, 10, 1), "a")]
    l = led(today=D(2026, 10, 20), stays=[bl.Stay(D(2026, 9, 1))], price_at=flat(None),
            regimes=[bl.Regime(mode=bl.LESSONS, package_size=12)], counted=counted)
    assert l.debt == 0 and bl.F_PRICE_MISSING in l.flags


# ---- status --------------------------------------------------------------------------------------------------


def test_a_student_without_any_money_event_stays_none_with_the_debt_visible():
    l = bl.run_ledger(e1())
    l = bl.run_ledger(replace(e1(), credits=()))
    assert l.status.status == ps.NONE and l.debt == 1_000_000 and l.status.paid_until is None


def test_status_thresholds():
    assert bl.run_ledger(e1([credit("p", TODAY_, 269_000)])).status.status == ps.EXPIRED          # paid to 14.10
    assert bl.run_ledger(e1([credit("p", TODAY_, 769_000)])).status.status == ps.ACTIVE           # paid to 14.11


TODAY_ = D(2026, 10, 21)


@pytest.mark.parametrize("through_offset,status", [(-1, ps.EXPIRED), (0, ps.ENDS_TODAY), (1, ps.EXPIRING), (3, ps.EXPIRING), (4, ps.ACTIVE)])
def test_paid_until_vs_today(through_offset, status):
    # one period of 31 days from 01.10 at 310 000: one day = 10 000
    today = D(2026, 10, 10)
    days = 10 + through_offset  # paid through today + offset
    l = led(today=today, stays=[bl.Stay(D(2026, 10, 1))], price_at=flat(310_000), regimes=[FIXED1],
            credits=[credit("p", D(2026, 10, 1), days * 10_000)])
    assert l.status.paid_until == D(2026, 10, 1) + timedelta(days=days - 1)
    assert l.status.status == status


def test_left_membership_is_not_tracked():
    l = led(today=D(2026, 10, 21), stays=[bl.Stay(D(2026, 9, 1), D(2026, 9, 20))], price_at=flat(300_000),
            credits=[credit("p", D(2026, 9, 1), 50_000)])
    assert l.status.status == ps.NONE and l.debt > 0


# ---- pieces: transfer chains -----------------------------------------------------------------------------------


def _chain_world():
    """A (paid 500 000 on 01.10, leaves 11.10) -> B (leaves 21.10) -> C; all fixed day 1."""
    reg = FIXED1
    a = make(today=D(2026, 10, 31), stays=[bl.Stay(D(2026, 10, 1), D(2026, 10, 11))], price_at=flat(500_000), regimes=[reg],
             credits=[credit("pa", D(2026, 10, 1), 500_000)])
    x1 = bl.refundable_at(a, D(2026, 10, 11))
    b = make(today=D(2026, 10, 31), stays=[bl.Stay(D(2026, 10, 11), D(2026, 10, 21))], price_at=flat(400_000), regimes=[reg],
             floor=D(2026, 10, 12), credits=[credit("tb", D(2026, 10, 11), x1, bl.TRANSFER_IN)])
    x2 = bl.refundable_at(b, D(2026, 10, 21))
    c = make(today=D(2026, 10, 31), stays=[bl.Stay(D(2026, 10, 21))], price_at=flat(600_000), regimes=[reg],
             floor=D(2026, 10, 22), credits=[credit("tc", D(2026, 10, 21), x2, bl.TRANSFER_IN)])
    return a, b, c, x1, x2


def test_transfer_chain_a_b_c_conserves_money():
    a, b, c, x1, x2 = _chain_world()
    assert x1 == 323_000
    la = bl.run_ledger(replace(a, outflows=(outflow("oa", D(2026, 10, 11), x1, bl.TRANSFER_OUT),)))
    lb = bl.run_ledger(replace(b, outflows=(outflow("ob", D(2026, 10, 21), x2, bl.TRANSFER_OUT),)))
    lc = bl.run_ledger(c)
    # B: 11.10 is A's day, B is charged 12-21.10 (10 days of 31 -> 129 000); 323 000 - 129 000 = 194 000 advance moves on
    assert lb.charged == 129_000 and x2 == 194_000
    assert (la.debt, la.advance, lb.debt, lb.advance) == (0, 0, 0, 0)
    # I7: out == in
    assert la.transferred_out + lb.transferred_out == lb.transferred_in + lc.transferred_in
    assert all(l.reconciles for l in (la, lb, lc))
    # all of the 500 000 is accounted for: charged in A and B plus what C holds
    assert la.charged + lb.charged + (lc.charged + lc.advance - lc.debt) == 500_000


def test_transfer_back_to_the_first_group_uses_a_new_stay():
    reg = FIXED1
    a_first = make(today=D(2026, 10, 31), stays=[bl.Stay(D(2026, 10, 1), D(2026, 10, 11)), bl.Stay(D(2026, 10, 21))],
                   price_at=flat(500_000), regimes=[reg], credits=[credit("pa", D(2026, 10, 1), 500_000)])
    l = bl.run_ledger(a_first)
    # the gap 12-20.10 is not charged: 11 + 11 = 22 days of 31
    assert sum(len(s.days) for s in l.segments if s.begun) == 22


def test_refundable_at_is_a_slice_of_the_account():
    inp = e1([credit("p18", D(2026, 10, 25), 400_000, minutes=1)], today=D(2026, 10, 30),
             stays=[bl.Stay(D(2026, 9, 15), D(2026, 10, 25))])
    assert bl.refundable_at(inp, D(2026, 10, 24)) == bl.run_ledger(bl.sliced_input(inp, D(2026, 10, 24))).refundable
    assert bl.refundable_at(inp, D(2026, 9, 1)) == 0


def test_credits_are_laid_in_date_order_whatever_the_input_order():
    credits = [credit("c", D(2026, 10, 5), 100_000, minutes=3), credit("a", D(2026, 9, 20), 231_000),
               credit("b", D(2026, 10, 1), 50_000, minutes=2)]
    one = bl.run_ledger(make(today=TODAY_, stays=[bl.Stay(D(2026, 9, 15))], price_at=flat(500_000), credits=credits))
    two = bl.run_ledger(make(today=TODAY_, stays=[bl.Stay(D(2026, 9, 15))], price_at=flat(500_000), credits=credits[::-1]))
    assert one == two


# ---- adapters ------------------------------------------------------------------------------------------------


def test_coverage_records_for_the_excuse_deadline():
    l = bl.run_ledger(e1([credit("p18", TODAY_, 300_000, minutes=1)]))
    (rec,) = bl.coverage_records(l)
    assert (rec.valid_from, rec.valid_to) == (D(2026, 9, 15), D(2026, 10, 15)) and rec.lessons_count is None
    l = bl.run_ledger(e1())
    (rec,) = bl.coverage_records(l)
    assert rec.valid_to == D(2026, 9, 27)
    from tests.pay2_ledger_helpers import e5
    l = bl.run_ledger(e5(12, D(2026, 10, 21), [credit("p", D(2026, 10, 21), 630_000)]))
    (rec,) = bl.coverage_records(l)
    assert rec.lessons_count == 12 and rec.valid_to is None
    assert bl.coverage_records(bl.run_ledger(replace(e1(), credits=()))) == []


def test_a_free_group_in_lessons_mode_is_free_and_builds_no_virtual_lessons():
    counted = [bl.CountedLesson(D(2026, 10, 1) + timedelta(days=i), f"l{i}") for i in range(3)]
    inp = make(today=D(2026, 10, 20), stays=[bl.Stay(D(2026, 9, 1))], price_at=flat(0), counted=counted,
               regimes=[bl.Regime(mode=bl.LESSONS, package_size=12)])
    l = bl.run_ledger(inp)
    assert l.status.status == ps.FREE and l.debt == 0 and l.charged == 0
    assert l.lessons is not None and l.lessons.covered == 3 and all(l.lessons.paid_flags)   # nothing but the held lessons
    l = bl.run_ledger(replace(inp, credits=(credit("p", D(2026, 10, 2), 100_000),)))
    assert l.status.status == ps.FREE and l.lessons.covered == 3


def test_refunds_and_transfers_out_are_cut_separately():
    l = bl.run_ledger(make(today=D(2026, 10, 25), stays=[bl.Stay(D(2026, 9, 1), D(2026, 9, 30))], price_at=flat(100_000),
                           credits=[credit("a", D(2026, 9, 1), 300_000)],
                           outflows=[outflow("r", D(2026, 10, 1), 50_000), outflow("t", D(2026, 10, 2), 30_000, bl.TRANSFER_OUT, minutes=1)]))
    assert (l.refunded_from["a"], l.transferred_from["a"], l.net_money["a"]) == (50_000, 30_000, 220_000)


def test_lessons_held_before_the_price_are_skipped_and_the_packages_go_on_from_the_first_priced_lesson():
    """Price valid from 10.10: the lessons of 02.10 and 05.10 are neither charged nor paid; package 1 starts on 12.10."""
    days = [D(2026, 10, 2), D(2026, 10, 5), D(2026, 10, 12), D(2026, 10, 14), D(2026, 10, 16)]
    counted = [bl.CountedLesson(d, f"l{i}") for i, d in enumerate(days)]
    inp = make(today=D(2026, 10, 20), stays=[bl.Stay(D(2026, 9, 1))], price_at=flat(300_000, since=D(2026, 10, 10)),
               regimes=[bl.Regime(mode=bl.LESSONS, package_size=3)], counted=counted,
               credits=[credit("p", D(2026, 10, 12), 200_000)])
    l = bl.run_ledger(inp)
    assert bl.F_PRICE_MISSING in l.flags and l.unpriced_from == D(2026, 10, 2)
    assert [(s.package_no, s.days) for s in l.segments] == [(1, tuple(days[2:]))]
    assert l.charged == 300_000 and l.debt == 100_000 and l.reconciles
    assert l.lessons.used == 3 and l.lessons.covered == 2 and l.lessons.unpaid == 1
    # per counted lesson (all five stay in the list): the skipped ones are not paid, then 2 of 3 are
    assert [l.lesson_paid(c.day, c.lesson_id) for c in counted] == [False, False, True, True, False]
