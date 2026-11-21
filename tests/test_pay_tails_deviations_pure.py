"""PAY-TAILS А6.7: the KNOWN deviations of the mechanism, pinned «as they are» (pure ledger, numbers by hand): О2, О3.  О1 was FIXED in А7.1 (the capacity of a period is counted per credit).  They are not fixed in package A: the lead decides, ROADMAP records them."""
from dataclasses import replace
from datetime import date, timedelta

from src.auth import billing_ledger as bl
from src.auth import payment_status as ps
from tests.pay2_ledger_helpers import D, T0, credit, flat, make, weekly


def entered(cid, on, amount, minutes=0):
    return bl.Credit(cid, bl.WRITEOFF, on, T0 + timedelta(minutes=minutes), amount, entered_on=on)


def test_o1_a_pause_planned_before_the_forgiveness_then_a_prepayment_of_the_next_month():
    # FIXED in А7.1 (it was a «known deviation» pinned as it is: the nominal sum did not know about the pause and the prepayment of the next month ended short).
    # ОМ 600 000 (12 lessons Tue / Thu / Fri), a pause 17.11 - 30.11 is entered on 09.11 (6 lessons are charged: A = 300 000; the capacity of November for a credit entered AFTER the pause = 300 000).
    # 10.11: the debt is 300 000 -> forgiven 300 000: it lies on November (capacity 300 000) and is USED.  11.11: the next month is prepaid, 600 000: November is full, so all of it is December's:
    # December money 600 000, unpaid 0.  Debt 0; the forgiveness is used 300 000; 600 000 are an advance (December has not begun: money).
    fixed = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
    inp = make(today=D(2026, 11, 20), stays=[bl.Stay(D(2026, 11, 1))], price_at=flat(600_000), regimes=[fixed], start=D(2026, 10, 1), lesson_days=weekly((1, 3, 4)),
               credits=[entered("w", D(2026, 11, 10), 300_000), credit("p", D(2026, 11, 11), 600_000, minutes=1)])
    L = bl.run_ledger(replace(inp, pauses=(bl.Pause(D(2026, 11, 17), D(2026, 11, 30), created_on=D(2026, 11, 9)),)))
    nov, dec = [s for s in L.segments if s.kind == "period"][:2]
    assert (nov.amount, nov.money, nov.forgiven) == (300_000, 0, 300_000)
    assert (dec.money, dec.forgiven, dec.unpaid) == (600_000, 0, 0)
    assert (L.written_off, L.written_off_granted, L.debt, L.advance) == (300_000, 300_000, 0, 600_000)
    # the same account when the pause is entered AFTER both records (С5: a later pause is not in the capacity): the order of the line decides as before - November takes 300 000 of the 600 000 of the nominal sum,
    # money first: money 300 000 (the forgiveness is not needed, it burns), December gets the other 300 000
    L = bl.run_ledger(replace(inp, pauses=(bl.Pause(D(2026, 11, 17), D(2026, 11, 30), created_on=D(2026, 11, 12)),)))
    nov, dec = [s for s in L.segments if s.kind == "period"][:2]
    assert (nov.money, nov.forgiven, dec.money, dec.forgiven) == (300_000, 0, 300_000, 0)
    assert (L.written_off, L.written_off_granted, L.debt, L.advance) == (0, 300_000, 0, 300_000)


def test_o2_a_price_lowered_backdated_gives_an_advance_in_one_order_of_the_records_and_none_in_the_other():
    # ЛП 420 000 lowered to 300 000 from 01.11 (an act of the owner), 10.11.  Order A: forgiven 220 000 (01.11), paid 200 000 (02.11): the nominal sum is 300 000 (the NEW price), the forgiveness
    # covers 220 000, money 80 000 close November, 120 000 of money go on -> advance 120 000 (it is money).  Order B: paid 200 000, forgiven 220 000: money 200 000 + forgiveness 100 000;
    # the other 120 000 of the forgiveness lie on a period that has not begun and burn -> advance 0.
    price = flat(420_000, D(2026, 1, 1), {D(2026, 11, 1): 300_000})
    common = dict(today=D(2026, 11, 10), stays=[bl.Stay(D(2026, 11, 1))], price_at=price, start=D(2026, 10, 1))
    a = bl.run_ledger(make(credits=[entered("w", D(2026, 11, 1), 220_000), credit("p", D(2026, 11, 2), 200_000, minutes=1)], **common))
    b = bl.run_ledger(make(credits=[credit("p", D(2026, 11, 1), 200_000), entered("w", D(2026, 11, 2), 220_000, 1)], **common))
    assert (a.charged, a.debt, a.advance, a.written_off) == (300_000, 0, 120_000, 220_000)
    assert (b.charged, b.debt, b.advance, b.written_off) == (300_000, 0, 0, 100_000)
    assert a.reconciles and b.reconciles


def test_o3_a_change_of_the_billing_mode_in_the_middle_of_the_month_with_payment_and_forgiveness():
    # the mode of the centre changes on 16.11 from «свой месяц» (420 000, 01.11 - 30.11) to the common month (billing day 1): the nominal sum of the cut period is not defined by one rule.
    # The account is written as it is (FACT of the run): paid 200 000, forgiven 220 000, today 20.11.
    first = bl.Regime()
    second = bl.Regime(effective_from=D(2026, 11, 16), anchor=ps.FIXED_DAY, billing_day=1)
    L = bl.run_ledger(make(today=D(2026, 11, 20), stays=[bl.Stay(D(2026, 11, 1))], price_at=flat(420_000), regimes=[first, second], start=D(2026, 10, 1), lesson_days=weekly((1, 3, 4)),
                           credits=[credit("p", D(2026, 11, 1), 200_000), entered("w", D(2026, 11, 2), 220_000, 1)]))
    # FACT: two pieces of the cell 01.11 (the old regime 01.11 - 15.11 = 210 000, the new one from 16.11 = 210 000); the credits are all tied to the FIRST piece (its nominal sum is 420 000: money 200 000 +
    # forgiveness 220 000), which holds only 210 000: money 200 000, forgiveness 10 000 used, 210 000 of the forgiveness burn, and the second piece stays unpaid -> debt 210 000 although 220 000 were forgiven.
    # The lead decides (owner's act «forgave 220 000» vs the cut of the period by the change of the mode); the ROADMAP records it.
    assert [(s.amount, s.nominal, s.money, s.forgiven) for s in L.segments] == [(210_000, 420_000, 200_000, 10_000), (210_000, 210_000, 0, 0)]
    assert (L.charged, L.debt, L.advance, L.written_off, L.written_off_granted) == (420_000, 210_000, 0, 10_000, 220_000)
    assert L.reconciles and L.refundable == L.advance


def test_o3_the_same_change_deferred_as_the_product_saves_it_does_not_cut_the_period():
    # А7.11: the product saves a change of the mode as a DEFERRED version: the running period 01.11 - 30.11 (420 000) goes on, the common month begins for the student on 01.12.  The same credits (paid 200 000,
    # forgiven 220 000): November money 200 000 + forgiveness 220 000 = 420 000, debt 0 - the cut of О3 does not arise.
    first = bl.Regime()
    second = bl.Regime(effective_from=D(2026, 11, 16), anchor=ps.FIXED_DAY, billing_day=1, defer=True, at=T0 + timedelta(days=320))
    L = bl.run_ledger(make(today=D(2026, 11, 20), stays=[bl.Stay(D(2026, 11, 1))], price_at=flat(420_000), regimes=[first, second], start=D(2026, 10, 1), lesson_days=weekly((1, 3, 4)),
                           credits=[credit("p", D(2026, 11, 1), 200_000), entered("w", D(2026, 11, 2), 220_000, 1)]))
    assert (L.charged, L.debt, L.advance, L.written_off, L.written_off_granted) == (420_000, 0, 0, 220_000, 220_000) and L.reconciles
