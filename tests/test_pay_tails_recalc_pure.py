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


def test_a_payment_entered_backdated_after_a_forgiveness_pays_the_period_that_the_forgiveness_could_not_reach():
    # А7.3 (а): the pass fires in the product when a payment is entered BACKDATED after a forgiveness.  Personal period from 01.10 (420 000 each).  25.10: October is forgiven, 420 000.  26.10: the
    # cashier enters a payment of 420 000 DATED 01.10 (it paid October after all).  05.11: paid 420 000.  By the order of the dates: the payment of 01.10 closes October, the forgiveness (25.10) is next on
    # the line and falls on November - which had not begun on 25.10: thrown away; the payment of 05.11 would lie on December.  The pass brings it back to November (begun, unfilled).
    # Today 10.11: charged October + November = 840 000, money 840 000, debt 0, the forgiveness is not used (granted 420 000), advance 0 - not 420 000 of "advance" in December.
    inp = make(today=D(2026, 11, 10), stays=[bl.Stay(D(2026, 10, 1))], price_at=flat(420_000), start=D(2026, 9, 1),
               credits=[credit("p1", D(2026, 10, 1), 420_000, minutes=2), entered("w", D(2026, 10, 25), 420_000, 1), credit("p2", D(2026, 11, 5), 420_000, minutes=3)])
    L = bl.run_ledger(inp)
    periods = [s for s in L.segments if s.kind == "period"]
    assert [(s.period_start.isoformat(), s.money, s.forgiven) for s in periods[:3]] == [("2026-10-01", 420_000, 0), ("2026-11-01", 420_000, 0), ("2026-12-01", 0, 0)]
    assert (L.charged, L.debt, L.advance, L.written_off, L.written_off_granted) == (840_000, 0, 0, 0, 420_000) and L.reconciles
    assert sum(s.unpaid for s in L.segments if s.begun) == 0


def test_the_pass_moves_the_latest_payment_first_by_the_key_of_the_credit_not_by_the_text_of_its_id():
    # А7.3 (б): personal period from 01.10 (420 000).  p1 01.10 420 000 -> October.  w: 25.10 forgiven 320 000 -> falls on November (not begun on 25.10: thrown away; the room of November is 320 000).
    # "zz" 05.11 300 000: November has 100 000 of capacity left -> 100 000 there, 200 000 on December.  "aa" 06.11 300 000: December 220 000, January 80 000.
    # The pass fills the room 320 000 of November from the LATEST periods and the latest payments: January "aa" 80 000; December "aa" 220 000, then "zz" 20 000.
    # November: "aa" 300 000, "zz" 100 000 + 20 000 = 120 000.  (By the text of the id "zz" would have gone first: "zz" 300 000 + 100 000, "aa" 120 000.)
    inp = make(today=D(2026, 11, 10), stays=[bl.Stay(D(2026, 10, 1))], price_at=flat(420_000), start=D(2026, 9, 1),
               credits=[credit("p1", D(2026, 10, 1), 420_000, minutes=1), entered("w", D(2026, 10, 25), 320_000, 2), credit("zz", D(2026, 11, 5), 300_000, minutes=3),
                        credit("aa", D(2026, 11, 6), 300_000, minutes=4)])
    L = bl.run_ledger(inp)
    nov = next(s for s in L.segments if s.kind == "period" and s.period_start == D(2026, 11, 1))
    assert {(a.credit_id, a.amount) for a in L.allocations if a.segment_id == nov.id} == {("aa", 300_000), ("zz", 120_000)}
    assert L.reconciles and not L.flags


def test_a_forgiveness_that_money_had_pushed_out_covers_the_donor_period_after_the_money_has_gone():
    # А7.3 (в): common month 600 000 from 01.10.  25.09: forgiven 600 000 (falls on October, which had not begun: thrown away, October stays unpaid).  05.11 paid 300 000, 06.11 forgiven 300 000: both lie on
    # November (capacity 600 000: the pause below was entered later).  20.11: a pause 17.11 - 30.11 is entered: November is 6 lessons of 12 = 300 000.  Money first: 300 000 cover it, the forgiveness
    # burns.  The pass moves the 300 000 of money to the begun, unfilled October: October money 300 000 (unpaid 300 000) - and November is free of money, so the forgiveness of 06.11 covers it: used 300 000.
    # Debt 300 000 (October's rest); without the laying-again November would also stay unpaid and the debt would be 600 000 although 300 000 were forgiven for it.  Granted 900 000, used 300 000.
    inp = make(today=D(2026, 11, 25), stays=[bl.Stay(D(2026, 10, 1))], price_at=flat(600_000), regimes=[FIXED1], start=D(2026, 9, 1), lesson_days=TTF,
               credits=[entered("w0", D(2026, 9, 25), 600_000, 0), credit("p", D(2026, 11, 5), 300_000, minutes=1), entered("w2", D(2026, 11, 6), 300_000, 2)])
    L = bl.run_ledger(replace(inp, pauses=(bl.Pause(D(2026, 11, 17), D(2026, 11, 30), created_on=D(2026, 11, 20)),)))
    octo, nov = [s for s in L.segments if s.kind == "period"][:2]
    assert (nov.amount, nov.money, nov.forgiven, nov.unpaid) == (300_000, 0, 300_000, 0)
    assert (octo.amount, octo.money, octo.forgiven, octo.unpaid) == (600_000, 300_000, 0, 300_000)
    assert (L.charged, L.debt, L.advance, L.written_off, L.written_off_granted) == (900_000, 300_000, 0, 300_000, 900_000) and L.reconciles and not L.flags
    assert L.forgiven_used_by == {"w2": 300_000}


def test_a_forgiveness_of_the_opening_balance_covers_it_even_when_the_debt_day_is_after_the_day_it_was_entered():
    # А7.7: a stay from 01.11 (420 000), the opening balance of the membership says «owed 100 000 on 10.11» (a debt «on a future start day»).  05.11: forgiven 100 000.  The rule «the period had begun by the day
    # of entering» is for periods of the stay; the debt of the opening is old by its nature, so it is covered: used 100 000 (before: 0, the forgiveness burned and the debt stayed).  Today 20.11:
    # charged 100 000 + 420 000 = 520 000, used 100 000, debt 420 000, advance 0.
    inp = make(today=D(2026, 11, 20), stays=[bl.Stay(D(2026, 11, 1))], price_at=flat(420_000), start=D(2026, 10, 1), credits=[entered("w", D(2026, 11, 5), 100_000)])
    L = bl.run_ledger(replace(inp, opening=bl.Opening(debt_amount=100_000, debt_on=D(2026, 11, 10))))
    assert (L.charged, L.debt, L.advance, L.written_off, L.written_off_granted) == (520_000, 420_000, 0, 100_000, 100_000) and L.reconciles and not L.flags
