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


# ---- Б7 / ДЫРА-05: three days of «ждём оплату» ----------------------------------------------------------------------------------------------------


def entry(inp):
    """What ``payments_views`` reads of an entry: the ledger, the input and the three rows of ``is_tracked``."""
    return SimpleNamespace(ledger=bl.run_ledger(inp), inp=inp, membership=SimpleNamespace(is_active=True), group=SimpleNamespace(status="active"),
                           student=SimpleNamespace(status="active"), price=SimpleNamespace(amount=300_000))


@pytest.mark.parametrize("today, tab, waiting, old_reason", [
    (D(2026, 11, 5), "waiting", "Ждём оплату до 07.11", "Просрочено на 1 день"),       # A3: the first day of the new period 05.11 - the debt 300 000 is there, the student is not a debtor
    (D(2026, 11, 6), "waiting", "Ждём оплату до 07.11", "Просрочено на 2 дня"),
    (D(2026, 11, 7), "waiting", "Ждём оплату до 07.11", "Просрочено на 3 дня"),       # the third day
    (D(2026, 11, 8), "debtors", None, "Просрочено на 4 дня"),                          # the fourth day: a debtor
])
def test_b7_the_debt_of_a_new_period_is_waiting_for_three_days_then_a_debtor(today, tab, waiting, old_reason):
    e = entry(own_month([bl.Stay(D(2026, 10, 5))], today))                                                  # 300 000 paid for 05.10 - 04.11, nothing for 05.11
    assert e.ledger.debt == 300_000 and e.ledger.reconciles                                                  # I-1: the number does not depend on the name
    assert pv.entry_tab(e) == tab and pv.waiting_reason(e) == waiting
    assert pv.entry_bucket(e) == "debt" and pv.debt_reason(e)[1] == old_reason                               # R3 №21: the OLD fields keep their meaning in this release


def test_b7_a_new_student_waits_three_days_too_and_a_part_payment_does_not_end_it():
    new = entry(own_month([bl.Stay(D(2026, 11, 5))], D(2026, 11, 6), paid=0))
    assert new.ledger.debt == 300_000 and pv.entry_tab(new) == "waiting" and pv.waiting_until(new) == D(2026, 11, 7)
    part = entry(own_month([bl.Stay(D(2026, 11, 5))], D(2026, 11, 6), paid=0, credits=(credit("p", D(2026, 11, 6), 100_000),)))
    assert part.ledger.debt == 200_000 and pv.entry_tab(part) == "waiting"
    late = entry(own_month([bl.Stay(D(2026, 11, 5))], D(2026, 11, 9), paid=0))
    assert pv.entry_tab(late) == "debtors" and pv.debt_reason(late) == ("never_paid", "Ещё не платил")


def test_b7_an_old_debt_or_a_student_who_left_is_a_debtor_at_once():
    old = entry(own_month([bl.Stay(D(2026, 10, 5))], D(2026, 11, 5), paid=0))                                # October is not paid and November has begun: 600 000, the past debt is there
    assert old.ledger.past_debt == 300_000 and pv.entry_tab(old) == "debtors"
    gone = entry(own_month([bl.Stay(D(2026, 11, 5), D(2026, 11, 6))], D(2026, 11, 6), paid=0))
    gone.membership.is_active = False
    assert gone.ledger.debt > 0 and pv.entry_tab(gone) == "debtors"


def test_b7_a_package_has_no_periods_and_never_waits():
    inp = make(today=D(2026, 11, 5), stays=[bl.Stay(D(2026, 10, 5))], price_at=flat(600_000, D(2026, 1, 1)), start=D(2026, 9, 1),
               regimes=[bl.Regime(mode=bl.LESSONS, package_size=12)], counted=[bl.CountedLesson(D(2026, 11, 2), "l1")])
    e = entry(inp)
    assert e.ledger.debt > 0 and pv.entry_tab(e) == "debtors" and pv.entry_bucket(e) == "debt"
