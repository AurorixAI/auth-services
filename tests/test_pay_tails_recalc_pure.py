"""PAY-TAILS А6.3 (the deduction for a lesson the centre cancelled) and the pass of ``_place_credits`` that brings money back to a begun, unfilled period - pure ledger, numbers by hand."""
from dataclasses import replace
from datetime import date

from src.auth import billing_ledger as bl
from src.auth import payment_status as ps
from tests.pay2_ledger_helpers import D, T0, credit, flat, make, weekly

FIXED1 = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
TTF = weekly((1, 3, 4))                      # Tue Thu Fri: November 2026 has 12 lessons, 600 000 / 12 = 50 000 a lesson
ON = (bl.RecalcVersion(date.min, True),)


def nov(credits, *, cancels=(D(2026, 11, 12),), today=D(2026, 11, 20)):
    inp = make(today=today, stays=[bl.Stay(D(2026, 11, 1))], price_at=flat(600_000), regimes=[FIXED1], start=D(2026, 10, 1), credits=credits, lesson_days=TTF)
    return bl.run_ledger(replace(inp, centre_cancels=tuple(bl.CentreCancel(d, "l") for d in cancels), recalc_versions=ON))


def entered(cid, on, amount, minutes=0):
    from datetime import timedelta

    return bl.Credit(cid, bl.WRITEOFF, on, T0 + timedelta(minutes=minutes), amount, entered_on=on)


def test_c6_paid_300_forgiven_300_and_the_centre_cancels_a_lesson():
    # 600 000 for 12 lessons: the lesson is 50 000 (was 25 000 = (600 000 - 300 000 forgiven) / 12).  Money 300 000 + deduction 50 000 + forgiveness 250 000 = 600 000: used 250 000, advance 0
    L = nov([credit("p", D(2026, 11, 2), 300_000), entered("w", D(2026, 11, 3), 300_000, 1)])
    assert [r.amount for r in L.recalcs] == [50_000]
    assert (L.charged, L.debt, L.advance, L.refundable, L.written_off, L.written_off_granted) == (600_000, 0, 0, 0, 250_000, 300_000) and L.reconciles and not L.flags


def test_c7_paid_in_full_the_deduction_is_money_back():
    # paid 600 000, a lesson cancelled: 50 000 are money the student paid for a lesson that did not take place -> advance 50 000 = refundable (money; as before)
    L = nov([credit("p", D(2026, 11, 2), 600_000)])
    assert (L.debt, L.advance, L.refundable, L.written_off) == (0, 50_000, 50_000, 0) and L.reconciles and not L.flags


def test_a_fully_forgiven_month_and_a_cancelled_lesson_use_550_000_and_give_no_advance():
    L = nov([entered("w", D(2026, 11, 3), 600_000)])
    assert [r.amount for r in L.recalcs] == [50_000]
    assert (L.debt, L.advance, L.refundable, L.written_off, L.written_off_granted) == (0, 0, 0, 550_000, 600_000) and L.reconciles


def test_money_on_a_later_period_comes_back_to_a_begun_unfilled_period():
    # personal period from 01.10 (420 000 each).  Paid 420 000 (01.10: October); a forgiveness of 420 000 ENTERED 15.10 (it would lie on November by the order, but November had not begun:
    # thrown away); paid 420 000 on 05.11 (by the order: December).  Today 10.11: charged October + November = 840 000; money 840 000, debt 0.  The money of December comes back to November:
    # November paid 420 000, December 0 - the periods and the totals say the same.  (In the product a forgiveness is never larger than the debt of its own day, so this does not happen:
    # the pass is what keeps the picture whole if it ever does.)
    inp = make(today=D(2026, 11, 10), stays=[bl.Stay(D(2026, 10, 1))], price_at=flat(420_000), start=D(2026, 9, 1),
               credits=[credit("p1", D(2026, 10, 1), 420_000), entered("w", D(2026, 10, 15), 420_000, 1), credit("p2", D(2026, 11, 5), 420_000, minutes=2)])
    L = bl.run_ledger(inp)
    periods = [s for s in L.segments if s.kind == "period"]
    assert [(s.period_start.isoformat(), s.money, s.forgiven) for s in periods[:3]] == [("2026-10-01", 420_000, 0), ("2026-11-01", 420_000, 0), ("2026-12-01", 0, 0)]
    assert (L.charged, L.debt, L.advance, L.written_off, L.written_off_granted) == (840_000, 0, 0, 0, 420_000) and L.reconciles
    assert sum(s.unpaid for s in L.segments if s.begun) == 0
