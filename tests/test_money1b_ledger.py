"""Money-1 pack B, the pure ledger (no database): one test for each hole of the analyses with the NUMBERS of the analyses (``area-payments-calc*``).

* Б5 / B2 (d8): a student taken out of the group and back INSIDE the charged period goes on with it - 300 000, not 97 000 + 300 000;
* Б8 / ДЫРА-08: every lesson of the period cancelled by the centre - the whole price comes back (294 000 -> 300 000; the 6 000 of rounding were a debt for a month without lessons);
* Б7 / ДЫРА-05: three days of «ждём оплату» from the first day of a period, then a debtor; the debt is the same number all the time (I-1).
Every account also checks the invariant ``reconciles`` (I-1)."""
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from src.auth import billing_ledger as bl
from src.auth import payments_views as pv
from tests.pay2_ledger_helpers import D, credit, flat, make, weekly

MWF = weekly((0, 2, 4))
ON = (bl.RecalcVersion(date.min, True),)


def own_month(stays, today, *, paid=300_000, price=300_000, **extra):
    """«Свой месяц» (payment_date), 300 000 a month, paid on 05.10 (a Monday)."""
    inp = make(today=today, stays=stays, price_at=flat(price, D(2026, 1, 1)), start=D(2026, 9, 1),
               credits=[credit("p1", D(2026, 10, 5), paid)] if paid else [], lesson_days=MWF)
    return replace(inp, **extra)


# ---- Б5: «убрали и вернули» (d8) ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("back, charged", [
    (D(2026, 10, 14), 300_000),        # the same day: the stays touch (it worked before)
    (D(2026, 10, 15), 300_000),
    (D(2026, 10, 16), 300_000),        # d8: before 397 000 (97 000 + a second FULL period from 16.10), debt 97 000
    (D(2026, 10, 25), 300_000),        # ten days away: the gap is a pause, the period is extended by it
    (D(2026, 11, 4), 300_000),         # the last day of the period he left in
])
def test_b5_a_return_inside_the_charged_period_goes_on_with_it(back, charged):
    led = bl.run_ledger(own_month([bl.Stay(D(2026, 10, 5), D(2026, 10, 14)), bl.Stay(back)], D(2026, 11, 4)))
    assert led.charged == charged and led.debt == 0 and led.reconciles
    assert len([s for s in led.segments if s.kind == "period"]) == 1


def test_b5_the_numbers_of_d8_return_on_16_10():
    led = bl.run_ledger(own_month([bl.Stay(D(2026, 10, 5), D(2026, 10, 14)), bl.Stay(D(2026, 10, 16))], D(2026, 10, 20)))
    (seg,) = led.segments
    assert (seg.period_start, seg.period_end, seg.amount) == (D(2026, 10, 5), D(2026, 11, 5), 300_000)      # 31 study days: the gap of 15.10 is not one
    assert led.charged == 300_000 and led.debt == 0 and led.covered_through == D(2026, 11, 5)
    assert [(e.day, e.ref_id) for e in led.statement if e.kind == "pause"] == [(D(2026, 10, 15), "gap:2026-10-15:2026-10-15:1")]     # no pause was asked for; R3 money R-6: the statement says the period is one day longer
    assert led.pauses == ((D(2026, 10, 15), D(2026, 10, 15)),)                                                  # ... but the overdue days skip the gap, as the chain does


def test_b5_a_return_after_the_period_has_ended_starts_a_new_one_k1():
    led = bl.run_ledger(own_month([bl.Stay(D(2026, 10, 5), D(2026, 10, 14)), bl.Stay(D(2026, 11, 5))], D(2026, 11, 10)))
    assert [(s.period_start, s.amount) for s in led.segments] == [(D(2026, 10, 5), 97_000), (D(2026, 11, 5), 300_000)]
    assert led.charged == 397_000 and led.advance == 0 and led.debt == 97_000 and led.reconciles                   # 203 000 of the first payment would be an advance, but the period of 05.11 is not covered in full yet


def test_b5_a_real_pause_and_a_bridged_return_are_both_kept():
    pauses = (bl.Pause(D(2026, 10, 8), D(2026, 10, 9)),)
    led = bl.run_ledger(own_month([bl.Stay(D(2026, 10, 5), D(2026, 10, 14)), bl.Stay(D(2026, 10, 16))], D(2026, 10, 20), pauses=pauses))
    (seg,) = led.segments
    assert (seg.period_start, seg.period_end, seg.amount) == (D(2026, 10, 5), D(2026, 11, 7), 300_000)         # 31 study days: two days of the pause and one of the gap are not study days
    assert [(e.day, e.ref_id.split(":")[0]) for e in led.statement if e.kind == "pause"] == [(D(2026, 10, 8), "pause"), (D(2026, 10, 15), "gap")]


def test_b5_only_the_own_month_chain_is_bridged_a_fixed_day_cell_counts_the_days_of_each_stay():
    fixed = bl.Regime(anchor="fixed_day", billing_day=1)
    base = dict(today=D(2026, 10, 25), price_at=flat(300_000, D(2026, 1, 1)), start=D(2026, 9, 1), regimes=[fixed], lesson_days=MWF)
    apart = bl.run_ledger(make(stays=[bl.Stay(D(2026, 10, 1), D(2026, 10, 13)), bl.Stay(D(2026, 10, 15))], **base))      # out for 14.10 - a Wednesday, one of the 13 lessons of the month
    whole = bl.run_ledger(make(stays=[bl.Stay(D(2026, 10, 1))], **base))
    assert whole.charged == 300_000 and apart.charged == 277_000                                                # 12 of 13 lessons: nothing is charged twice, the day of the gap is not charged at all


# ---- Б8 / ДЫРА-08: the recalculation when EVERY lesson of the period is cancelled ---------------------------------------------------------------


def recalc_account(cancelled: int):
    days = MWF(D(2026, 10, 5), D(2026, 11, 4))
    assert len(days) == 14
    inp = own_month([bl.Stay(D(2026, 10, 5))], D(2026, 10, 25), paid=0, centre_cancels=tuple(bl.CentreCancel(d, "l") for d in days[:cancelled]), recalc_versions=ON)
    return bl.run_ledger(inp)


def test_b8_two_cancelled_lessons_cost_what_they_cost_f1_f3_unchanged():
    led = recalc_account(2)
    assert led.recalculated == 42_000 and led.debt == 258_000 and led.reconciles                            # 2 x round_1000(300 000 / 14) = 2 x 21 000


def test_b8_all_14_of_14_cancelled_the_whole_price_comes_back_the_rest_of_the_rounding_goes_to_the_last_lesson():
    led = recalc_account(14)
    assert led.recalculated == 300_000 and led.debt == 0 and led.reconciles                                  # before: 14 x 21 000 = 294 000 and a debt of 6 000 for a month without lessons
    assert [l.amount for l in led.recalcs] == [21_000] * 13 + [27_000]


def test_b8_13_of_14_is_still_by_the_price_of_a_lesson():
    led = recalc_account(13)
    assert led.recalculated == 273_000 and led.debt == 27_000


# ---- Б7 / ДЫРА-05 -> PAY-DUE: the term of payment (these cases were «ждём оплату» for three days; now the term of the centre, here 3 days, and ``pay_due.classify``) -------------------------------

from src.auth import pay_due as pd


def named(inp, days=3, lessons=2, in_centre=True):
    ledger = bl.run_ledger(inp)
    return ledger, pd.classify(ledger, inp.today, days, lessons, in_centre)


@pytest.mark.parametrize("today, name, until, overdue", [
    (D(2026, 11, 5), pd.DUE, D(2026, 11, 7), 0),       # A3: the first day of the new month: the debt of a period that has just begun is «К оплате»
    (D(2026, 11, 6), pd.DUE, D(2026, 11, 7), 0),
    (D(2026, 11, 7), pd.DUE, D(2026, 11, 7), 0),       # the third day, inclusive
    (D(2026, 11, 8), pd.DEBT, None, 1),                # the fourth day: a debt, overdue one day
])
def test_b7_the_unpaid_sum_of_a_new_period_is_within_its_term_then_a_debt(today, name, until, overdue):
    ledger, r = named(own_month([bl.Stay(D(2026, 10, 5))], today))                                           # 300 000 paid for October, November unpaid
    assert ledger.debt == 300_000 and ledger.reconciles                                                      # I-1: the sum is the same whatever the name
    assert (r.name, r.due_until, r.overdue_days) == (name, until, overdue) and r.unpaid == 300_000


def test_b7_a_new_student_has_the_term_too_and_a_part_payment_does_not_end_it():
    ledger, new = named(own_month([bl.Stay(D(2026, 11, 5))], D(2026, 11, 6), paid=0))
    assert ledger.debt == 300_000 and new.name == pd.DUE and new.due_until == D(2026, 11, 7)
    ledger, part = named(own_month([bl.Stay(D(2026, 11, 5))], D(2026, 11, 6), paid=0, credits=(credit("p", D(2026, 11, 6), 100_000),)))
    assert ledger.debt == 200_000 and (part.name, part.due_amount) == (pd.DUE, 200_000)
    _, late = named(own_month([bl.Stay(D(2026, 11, 5))], D(2026, 11, 9), paid=0))
    assert (late.name, late.overdue_days) == (pd.DEBT, 2)


def test_b7_an_old_tail_or_a_student_who_left_is_a_debt_at_once():
    ledger, old = named(own_month([bl.Stay(D(2026, 10, 5))], D(2026, 11, 5), paid=0))                       # October is past its term, November just began
    assert ledger.past_debt == 300_000 and (old.debt_amount, old.due_amount) == (300_000, 300_000)
    ledger, gone = named(own_month([bl.Stay(D(2026, 11, 5), D(2026, 11, 6))], D(2026, 11, 6), paid=0), in_centre=False)
    assert ledger.debt > 0 and (gone.name, gone.due_amount) == (pd.DEBT, 0)


def test_b7_a_package_is_by_lesson_not_by_period():
    inp = make(today=D(2026, 11, 5), stays=[bl.Stay(D(2026, 10, 5))], price_at=flat(600_000, D(2026, 1, 1)), start=D(2026, 9, 1),
               regimes=[bl.Regime(mode=bl.LESSONS, package_size=12)], counted=[bl.CountedLesson(D(2026, 11, 2), "l1")])
    ledger, r = named(inp)
    assert ledger.debt > 0 and r.due_lessons == {"used": 1, "limit": 2} and r.name == pd.DUE            # one unpaid lesson, within its 14 days
    _, closed = named(inp, lessons=0)
    assert closed.name == pd.DEBT
