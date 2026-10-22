"""LOGIC-A matrix cells (spec §2) that are about the pure ledger: one marked test per cell, ``test_logicA_<item>_<row>``
(item: f1 f7 f9 f12 f13 f14 d1..d6; row: n p o z r u c b m x d a).  ``scripts/check_matrix_coverage_a.py`` checks the table."""
from dataclasses import replace
from datetime import date, timedelta

from src.auth import billing_ledger as bl
from src.auth import payment_status as ps

from tests.pay2_ledger_helpers import credit, flat

D = date
FIXED1 = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
AS = D(2026, 10, 1)


def inp(**kw):
    base = dict(today=D(2026, 10, 21), accounting_start=AS, stays=(bl.Stay(D(2026, 10, 1)),), regimes=(FIXED1,), price_at=flat(620_000))
    base.update(kw)
    return bl.LedgerInput(**base)


def run(**kw):
    return bl.run_ledger(inp(**kw))


# ---- F1: the price a lesson carries --------------------------------------------------------------------------------------------------------------


def test_logicA_f1_m_an_incomplete_first_period_prices_its_lessons_at_the_full_price_of_the_period():
    lg = run(stays=(bl.Stay(D(2026, 10, 16)),), price_at=flat(600_000))
    seg = lg.segments[0]
    assert seg.amount == 310_000 and len(seg.days) == 16                           # prorated charge ...
    assert lg.lesson_cover(D(2026, 10, 20)).price.amount == 600_000                # ... but the BASE of a lesson is the price of the period


# ---- F9: leaving ---------------------------------------------------------------------------------------------------------------------------


def test_logicA_f9_m_leaving_on_the_first_day_of_a_period_charges_that_one_day():
    full = run(stays=(bl.Stay(D(2026, 10, 1)),))
    cut = bl.run_ledger(bl.sliced_input(inp(stays=(bl.Stay(D(2026, 10, 1)),), today=D(2026, 10, 21)), D(2026, 10, 1)))
    assert cut.charged == 20_000 and full.charged == 620_000                      # 1 of 31 days


# ---- F12: the deferred change of the billing mode -------------------------------------------------------------------------------------------------


def chain5(today, regimes, payments, first=D(2026, 10, 15), price=600_000):
    held = tuple(bl.CountedLesson(D(2026, 11, 15) + timedelta(days=2 * i), f"l{i}") for i in range(10) if D(2026, 11, 15) + timedelta(days=2 * i) <= today)
    return bl.run_ledger(bl.LedgerInput(
        today=today, accounting_start=AS, stays=(bl.Stay(first),), regimes=tuple(regimes), price_at=flat(price),
        credits=tuple(credit(f"p{i}", on, a) for i, (on, a) in enumerate(payments)), counted_lessons=held))


OLD = bl.Regime()
NEW = bl.Regime(effective_from=D(2026, 10, 21), mode=bl.LESSONS, package_size=12, defer=True)


def test_logicA_f12_p_a_partly_paid_period_keeps_its_partial_remainder_after_the_change():
    pay = [(D(2026, 10, 15), 300_000)]
    base, changed = chain5(D(2026, 10, 21), [OLD], pay), chain5(D(2026, 10, 21), [OLD, NEW], pay)
    assert (changed.debt, changed.advance, changed.covered_through, changed.charged) == (base.debt, base.advance, base.covered_through, base.charged)
    assert changed.debt == 300_000 and changed.covered_through == D(2026, 10, 29)          # 15 of 31 days (floor): the same partial


def test_logicA_f12_o_an_advance_stays_an_advance_until_the_new_regime_begins():
    pay = [(D(2026, 10, 15), 1_200_000)]
    base, changed = chain5(D(2026, 10, 21), [OLD], pay), chain5(D(2026, 10, 21), [OLD, NEW], pay)
    assert changed.advance == base.advance == 600_000 and changed.debt == base.debt == 0
    assert changed.covered_through == D(2026, 11, 14) and base.covered_through == D(2026, 12, 14)   # after 14.11 the advance is not days any more
    later = chain5(D(2026, 11, 20), [OLD, NEW], pay)                                      # after 15.11 the advance buys packages by lessons
    assert later.mode == bl.LESSONS and later.reconciles


def test_logicA_f12_z_a_free_group_is_not_touched_by_the_change():
    base, changed = chain5(D(2026, 10, 21), [OLD], [], price=0), chain5(D(2026, 10, 21), [OLD, NEW], [], price=0)
    assert changed.charged == base.charged == 0 and changed.status.status == base.status.status == ps.FREE


def test_logicA_f12_u_a_change_taken_back_before_it_begins_never_switches_the_student():
    old = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
    mid = bl.Regime(effective_from=D(2026, 10, 18), anchor=ps.FIXED_DAY, billing_day=10, defer=True)
    back = bl.Regime(effective_from=D(2026, 10, 20), anchor=ps.FIXED_DAY, billing_day=1, defer=True)
    today = D(2026, 12, 1)
    mk = lambda regs: bl.run_ledger(bl.LedgerInput(today=today, accounting_start=AS, stays=(bl.Stay(D(2026, 10, 3)),), regimes=tuple(regs), price_at=flat(600_000)))
    base, taken_back = mk([old]), mk([old, mid, back])
    key = lambda lg: [(s.period_start, s.days, s.amount) for s in lg.segments]
    assert key(taken_back) == key(base)


def test_logicA_f12_m_a_change_on_the_first_day_of_a_period_waits_for_the_end_of_that_period():
    old = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
    new = bl.Regime(effective_from=D(2026, 11, 1), anchor=ps.FIXED_DAY, billing_day=10, defer=True)
    lg = bl.run_ledger(bl.LedgerInput(today=D(2026, 12, 20), accounting_start=AS, stays=(bl.Stay(D(2026, 10, 3)),), regimes=(old, new), price_at=flat(600_000)))
    nov = next(s for s in lg.segments if s.period_start == D(2026, 11, 1))
    assert (nov.days[0], nov.days[-1]) == (D(2026, 11, 1), D(2026, 11, 30))               # the period that began on the change day is the running one
    dec = next(s for s in lg.segments if s.days[0] == D(2026, 12, 1))
    assert dec.days[-1] == D(2026, 12, 9)                                                  # the grid of day 10 from 01.12


# ---- D1: the opening balance ------------------------------------------------------------------------------------------------------------------


def test_logicA_d1_m_paid_until_at_the_end_or_in_the_middle_of_a_period():
    end = run(stays=(bl.Stay(D(2026, 10, 21)),), opening=bl.Opening(paid_until=D(2026, 10, 31)), today=D(2026, 11, 3))
    assert end.segments[0].days[0] == D(2026, 11, 1) and len(end.segments[0].days) == 30 and end.segments[0].amount == 620_000   # the next period whole
    mid = run(stays=(bl.Stay(D(2026, 10, 21)),), opening=bl.Opening(paid_until=D(2026, 10, 25)))
    assert mid.segments[0].days[0] == D(2026, 10, 26) and len(mid.segments[0].days) == 6                                          # the tail by the incomplete rules


def test_logicA_d1_x_the_price_is_taken_on_the_first_charged_day_after_paid_until():
    price = flat(600_000, changes={D(2026, 11, 1): 700_000})
    lg = run(stays=(bl.Stay(D(2026, 10, 21)),), opening=bl.Opening(paid_until=D(2026, 10, 31)), price_at=price, today=D(2026, 11, 3))
    assert lg.segments[0].price.amount == 700_000 and lg.charged == 700_000


# ---- D2: the pause ------------------------------------------------------------------------------------------------------------------------


def test_logicA_d2_p_money_covers_the_study_days_in_order_and_skips_the_paused_ones():
    lg = run(stays=(bl.Stay(D(2026, 10, 1)),), pauses=(bl.Pause(D(2026, 10, 5), D(2026, 10, 9)),), credits=(credit("p", D(2026, 10, 1), 100_000),))
    assert lg.segments[0].amount == 520_000 and lg.covered_through == D(2026, 10, 10)      # 5 days: 1-4 and 10 (the paused ones take no money)


def test_logicA_d2_o_an_advance_paid_before_the_pause_moves_paid_until_forward():
    lg = run(stays=(bl.Stay(D(2026, 10, 1)),), pauses=(bl.Pause(D(2026, 10, 11), D(2026, 10, 20)),), credits=(credit("p", D(2026, 10, 1), 620_000),))
    assert (lg.charged, lg.advance, lg.refundable) == (420_000, 200_000, 200_000) and lg.covered_through == D(2026, 11, 9)


def test_logicA_d2_m_a_pause_across_the_month_boundary_is_cut_from_each_period_by_its_part():
    lg = run(stays=(bl.Stay(D(2026, 10, 1)),), pauses=(bl.Pause(D(2026, 10, 28), D(2026, 11, 3)),), today=D(2026, 11, 20))
    oct_, nov = lg.segments[0], lg.segments[1]
    assert (len(oct_.days), len(nov.days)) == (27, 27) and oct_.amount == round(620_000 * 27 / 31, -3)
    assert not any(D(2026, 10, 28) <= d <= D(2026, 11, 3) for s in lg.segments for d in s.days)


def test_logicA_d2_x_a_price_change_during_the_pause_prices_the_period_after_it():
    price = flat(620_000, changes={D(2026, 10, 15): 700_000})
    lg = run(stays=(bl.Stay(D(2026, 10, 1)),), pauses=(bl.Pause(D(2026, 10, 10), D(2026, 10, 20)),), price_at=price, today=D(2026, 11, 5))
    assert lg.segments[0].price.amount == 620_000 and lg.segments[1].price.amount == 700_000      # a period is priced by its first billable day


def test_logicA_d2_a_a_pause_before_the_floor_of_an_opening_balance_has_no_effect():
    opening = bl.Opening(paid_until=D(2026, 10, 15))
    base = run(stays=(bl.Stay(D(2026, 10, 11)),), opening=opening)
    paused = run(stays=(bl.Stay(D(2026, 10, 11)),), opening=opening, pauses=(bl.Pause(D(2026, 10, 10), D(2026, 10, 14)),))
    assert [(s.days, s.amount) for s in paused.segments] == [(s.days, s.amount) for s in base.segments] and paused.charged == base.charged


# ---- D5: forgiven days are paid days --------------------------------------------------------------------------------------------------------------


def writeoff(amount, on=D(2026, 10, 31)):
    return bl.Credit("w", bl.WRITEOFF, on, bl.datetime(2026, 1, 1), amount)


def test_logicA_d5_p_a_partial_writeoff_covers_days_in_proportion():
    lg = run(today=D(2026, 10, 31), credits=(credit("p", D(2026, 10, 1), 400_000), writeoff(100_000)))
    assert lg.covered_through == D(2026, 10, 25)                                         # floor(500 000 * 31 / 620 000) = 25
    assert lg.lesson_cover(D(2026, 10, 10)).source == "money" and lg.lesson_cover(D(2026, 10, 23)).source == "writeoff"
    assert lg.lesson_cover(D(2026, 10, 28)).source == "unpaid"


def test_logicA_d5_m_a_writeoff_closes_the_oldest_debt_not_a_chosen_period():
    lg = bl.run_ledger(bl.LedgerInput(today=D(2026, 10, 31), accounting_start=D(2026, 9, 1), stays=(bl.Stay(D(2026, 9, 1)),), regimes=(FIXED1,),
                                      price_at=flat(500_000), credits=(writeoff(300_000),)))
    sep, oct_ = lg.segments[0], lg.segments[1]
    assert (sep.forgiven, oct_.forgiven) == (300_000, 0) and lg.debt == 700_000


def test_logicA_d5_x_a_price_change_after_the_writeoff_recomputes_the_coverage_not_the_sum():
    cheap = run(today=D(2026, 10, 31), price_at=flat(500_000), credits=(credit("p", D(2026, 10, 1), 400_000), writeoff(100_000)))
    dear = run(today=D(2026, 10, 31), price_at=flat(600_000), credits=(credit("p", D(2026, 10, 1), 400_000), writeoff(100_000)))
    assert cheap.debt == 0 and dear.debt == 100_000 and dear.written_off == cheap.written_off == 100_000


def test_logicA_d5_a_the_writeoff_of_an_opening_debt_closes_the_opening_segment():
    lg = run(stays=(bl.Stay(D(2026, 10, 21)),), opening=bl.Opening(debt_amount=250_000, debt_on=D(2026, 9, 30)), credits=(writeoff(250_000, D(2026, 10, 21)),))
    seg = lg.segments[0]
    assert seg.kind == "opening" and seg.unpaid == 0 and seg.forgiven == 250_000 and lg.debt == lg.charged - 250_000
