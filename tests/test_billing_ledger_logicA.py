"""LOGIC-A core: the opening balance, pauses, the deferred change of the billing regime and ``lesson_cover`` - the golden chains of the
spec (``logicA_spec.md`` §2-§3, hand-calculated in ``scripts/logicA_gen_fixtures.py``) as independent examples."""
from datetime import date, timedelta

import pytest

from src.auth import billing_ledger as bl
from src.auth import payment_status as ps

from tests.pay2_ledger_helpers import credit, flat, make, outflow

D = date
FIXED1 = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
TODAY = D(2026, 10, 21)
AS = D(2026, 10, 1)


def run(**kw):
    return bl.run_ledger(make(**kw))


# ---- G-OPEN (centre fixed_day=1, price 600 000, accounting start 01.10, today 21.10, enrolled 21.10) ----------------------------------


def g_open(opening=None, credits=(), today=TODAY):
    return run(today=today, stays=[bl.Stay(D(2026, 10, 21))], price_at=flat(600_000), regimes=[FIXED1], start=AS,
               credits=credits, ) if opening is None else bl.run_ledger(
        bl.LedgerInput(today=today, accounting_start=AS, stays=(bl.Stay(D(2026, 10, 21)),), regimes=(FIXED1,),
                       price_at=flat(600_000), credits=tuple(credits), opening=opening))


def test_open_a_paid_until_does_not_charge_nor_create_an_advance():
    lg = g_open(bl.Opening(paid_until=D(2026, 10, 31)))
    assert (lg.charged, lg.debt, lg.advance, lg.covered_through) == (0, 0, 0, D(2026, 10, 31))
    assert lg.status.status == ps.ACTIVE and lg.status.days_left == 10 and lg.reconciles
    assert lg.refundable == 0
    # 02.11: November (a whole month) has begun and nothing paid it
    lg = g_open(bl.Opening(paid_until=D(2026, 10, 31)), today=D(2026, 11, 2))
    assert (lg.charged, lg.debt, lg.covered_through, lg.status.status, lg.status.days_overdue) == (600_000, 600_000, D(2026, 10, 31),
                                                                                                 ps.EXPIRED, 2)


def test_open_b_debt_is_the_oldest_and_paid_first():
    op = bl.Opening(debt_amount=250_000, debt_on=D(2026, 9, 30))
    lg = g_open(op)
    assert (lg.charged, lg.debt, lg.past_debt, lg.current_due) == (463_000, 463_000, 250_000, 213_000)
    assert [s.kind for s in lg.segments] == ["opening", "period"] and lg.reconciles
    assert lg.covered_through is None and lg.status.status == ps.EXPIRED and lg.status.days_overdue == 21
    lg = g_open(op, credits=[credit("p1", TODAY, 300_000)])
    assert (lg.debt, lg.past_debt, lg.covered_through) == (163_000, 0, D(2026, 10, 22))   # floor(50 000 * 11 / 213 000) = 2
    assert lg.segments[0].unpaid == 0 and lg.segments[1].money == 50_000
    a = [(x.segment_id, x.amount, x.bucket, x.closes) for x in lg.allocations]
    assert a == [("seg:opening", 250_000, bl.PAST_DEBT, True), (lg.segments[1].id, 50_000, bl.CURRENT, False)]
    assert lg.allocations[0].label == "Закрыла долг на начало учёта на 30.09.2026"
    # 100 000 only: the opening debt is the only thing paid; "paid until" does not move
    lg = g_open(op, credits=[credit("p1", TODAY, 100_000)])
    assert (lg.debt, lg.covered_through, lg.segments[0].unpaid) == (363_000, None, 150_000)


def test_open_c_both():
    lg = g_open(bl.Opening(paid_until=D(2026, 10, 25), debt_amount=100_000, debt_on=D(2026, 9, 30)))
    assert [s.amount for s in lg.segments] == [100_000, 116_000] and lg.segments[1].days[0] == D(2026, 10, 26)
    assert (lg.debt, lg.covered_through) == (100_000, D(2026, 10, 25)) and not lg.segments[1].begun
    assert lg.status.status == ps.ACTIVE            # 4 days left


def test_open_overpaying_goes_to_the_current_period_then_the_advance_and_refundable_has_no_free_days():
    op = bl.Opening(paid_until=D(2026, 10, 31))
    lg = g_open(op, credits=[credit("p1", TODAY, 100_000)])
    assert (lg.debt, lg.advance, lg.refundable) == (0, 100_000, 100_000)       # money is money; the free days are not refundable
    assert lg.covered_through == D(2026, 11, 5)                              # floor(100 000 * 30 / 600 000) = 5 days of November


def test_open_without_debt_amount_is_ignored_and_empty_opening_is_none():
    assert g_open(bl.Opening()).charged == g_open(None).charged == 213_000


# ---- pauses -----------------------------------------------------------------------------------------------------------------------------


def chain1_a(today, *, pauses=(), payments=((D(2026, 10, 16), 300_000),), last=None, opening=bl.Opening(paid_until=D(2026, 10, 15))):
    return bl.run_ledger(bl.LedgerInput(
        today=today, accounting_start=AS, stays=(bl.Stay(D(2026, 10, 11), last),), regimes=(FIXED1,), price_at=flat(620_000),
        credits=tuple(credit(f"p{i}", on, amount) for i, (on, amount) in enumerate(payments)), pauses=tuple(pauses), opening=opening))


PAUSE = (bl.Pause(D(2026, 10, 20), D(2026, 10, 24)),)


def test_chain1_a_opening_payment_pause():
    assert (lambda lg: (lg.debt, lg.advance, lg.covered_through))(chain1_a(D(2026, 10, 11), payments=())) == (0, 0, D(2026, 10, 15))
    lg = chain1_a(D(2026, 10, 16))
    assert (lg.segments[0].amount, lg.debt, lg.covered_through) == (320_000, 20_000, D(2026, 10, 30))
    lg = chain1_a(TODAY, pauses=PAUSE)
    assert (lg.charged, lg.advance, lg.refundable, lg.covered_through) == (220_000, 80_000, 80_000, D(2026, 11, 3))
    assert lg.status.status == ps.PAUSED and lg.status.paused_until == D(2026, 10, 24) and lg.status.days_overdue is None
    assert all(d not in (D(2026, 10, 20), D(2026, 10, 22), D(2026, 10, 24)) for s in lg.segments for d in s.days)
    # void of the pause returns the old numbers (G3: void o create = id)
    lg0, lg1 = chain1_a(TODAY), chain1_a(TODAY, pauses=())
    assert lg0 == lg1 and (lg0.charged, lg0.debt) == (320_000, 20_000)
    assert any(e.kind == "pause" for e in chain1_a(TODAY, pauses=PAUSE).statement)


def test_chain1_transfer_to_b_and_leave():
    a26 = chain1_a(D(2026, 10, 26), pauses=PAUSE, last=D(2026, 10, 26))
    assert (a26.charged, a26.refundable) == (120_000, 180_000)
    inp = bl.LedgerInput(today=D(2026, 10, 28), accounting_start=AS, stays=(bl.Stay(D(2026, 10, 11)),), regimes=(FIXED1,),
                         price_at=flat(620_000), credits=(credit("p0", D(2026, 10, 16), 300_000),), pauses=PAUSE,
                         opening=bl.Opening(paid_until=D(2026, 10, 15)))
    x = bl.refundable_at(inp, D(2026, 10, 26))
    assert x == 180_000                                    # the slice cuts the stay on the transfer day and keeps the pause
    price_b = flat(465_000, changes={D(2026, 11, 1): 540_000})
    tin = bl.Credit("tr:1", bl.TRANSFER_IN, D(2026, 10, 26), D(2026, 10, 26), 180_000)
    base = dict(accounting_start=AS, regimes=(FIXED1,), price_at=price_b, credits=(tin,), first_billable_floor=D(2026, 10, 27))
    b = bl.run_ledger(bl.LedgerInput(today=D(2026, 10, 28), stays=(bl.Stay(D(2026, 10, 26)),), **base))
    assert (b.charged, b.advance, b.covered_through) == (75_000, 105_000, D(2026, 11, 5))
    left = bl.run_ledger(bl.LedgerInput(today=D(2026, 10, 29), stays=(bl.Stay(D(2026, 10, 26), D(2026, 10, 29)),), **base))
    assert (left.charged, left.advance, left.refundable) == (45_000, 135_000, 135_000)
    assert 120_000 + 45_000 + 135_000 == 300_000


def test_pause_edges():
    # a pause that covers the whole incomplete first period makes no segment; pause before the floor has no effect
    lg = run(today=TODAY, stays=[bl.Stay(D(2026, 10, 21))], price_at=flat(600_000), regimes=[FIXED1], start=AS)
    none = bl.run_ledger(bl.LedgerInput(today=TODAY, accounting_start=AS, stays=(bl.Stay(D(2026, 10, 21)),), regimes=(FIXED1,),
                                        price_at=flat(600_000), pauses=(bl.Pause(D(2026, 10, 21), D(2026, 10, 31)),)))
    assert none.charged == 0 and [s for s in none.segments if s.days] == []
    early = bl.run_ledger(bl.LedgerInput(today=TODAY, accounting_start=AS, stays=(bl.Stay(D(2026, 10, 21)),), regimes=(FIXED1,),
                                         price_at=flat(600_000), pauses=(bl.Pause(D(2026, 9, 1), D(2026, 10, 10)),)))
    assert early.charged == lg.charged
    # one day
    one = bl.run_ledger(bl.LedgerInput(today=TODAY, accounting_start=AS, stays=(bl.Stay(D(2026, 10, 21)),), regimes=(FIXED1,),
                                       price_at=flat(600_000), pauses=(bl.Pause(D(2026, 10, 25), D(2026, 10, 25)),)))
    assert len(one.segments[0].days) == 10
    # overlapping pauses are one; a pause outside the stay is cut by it
    two = bl.run_ledger(bl.LedgerInput(today=TODAY, accounting_start=AS, stays=(bl.Stay(D(2026, 10, 21), D(2026, 10, 28)),),
                                       regimes=(FIXED1,), price_at=flat(600_000),
                                       pauses=(bl.Pause(D(2026, 10, 24), D(2026, 10, 26)), bl.Pause(D(2026, 10, 25), D(2026, 11, 30)))))
    assert [d.day for d in two.segments[0].days] == [21, 22, 23]


def test_payment_date_a_pause_extends_the_period_and_the_later_anchors_shift():
    """Owner decision 21.10 (G3): a pause never adds a charge.  The period it falls into is extended by the paused days, the later periods
    follow the day after (no restart of the chain): the same price for the same number of study days."""
    base = bl.LedgerInput(today=D(2026, 12, 20), accounting_start=D(2026, 9, 1), stays=(bl.Stay(D(2026, 10, 5)),),
                          regimes=(bl.Regime(),), price_at=flat(300_000))
    paused = replace_inp(base, pauses=(bl.Pause(D(2026, 10, 20), D(2026, 10, 29)),))
    free, lg = bl.run_ledger(base), bl.run_ledger(paused)
    assert [(s.days[0], s.days[-1], len(s.days)) for s in free.segments[:2]] == [(D(2026, 10, 5), D(2026, 11, 4), 31), (D(2026, 11, 5), D(2026, 12, 4), 30)]
    # 05.10-19.10 (15 days) + 30.10-14.11 (16 days) = 31 study days; the second period begins on 15.11 and is 30 study days
    assert [(s.period_start, s.days[-1], len(s.days), s.amount) for s in lg.segments[:2]] == [
        (D(2026, 10, 5), D(2026, 11, 14), 31, 300_000), (D(2026, 11, 15), D(2026, 12, 14), 30, 300_000)]
    assert not any(D(2026, 10, 20) <= d <= D(2026, 10, 29) for seg in lg.segments for d in seg.days)
    assert lg.charged <= free.charged and lg.reconciles
    # two pauses in one period, one of them across the boundary of the nominal period
    two = replace_inp(base, pauses=(bl.Pause(D(2026, 10, 10), D(2026, 10, 12)), bl.Pause(D(2026, 11, 2), D(2026, 11, 8))))
    seg = bl.run_ledger(two).segments[0]
    assert len(seg.days) == 31 and seg.days[-1] == D(2026, 11, 14)      # 31 study days: 05-09.10, 13.10-01.11, 09-14.11


def replace_inp(inp, **kw):
    from dataclasses import replace

    return replace(inp, **kw)


# ---- deferred change of the regime (F12) --------------------------------------------------------------------------------------------------


def chain5(today, regimes, payments=((D(2026, 10, 15), 600_000),)):
    return bl.run_ledger(bl.LedgerInput(
        today=today, accounting_start=AS, stays=(bl.Stay(D(2026, 10, 15)),), regimes=tuple(regimes), price_at=flat(600_000),
        credits=tuple(credit(f"p{i}", on, a) for i, (on, a) in enumerate(payments)),
        lesson_days=lambda a, b: (), counted_lessons=tuple(c for c in (bl.CountedLesson(D(2026, 11, 15) + timedelta(days=i * 2), f"l{i}") for i in range(10))
                              if c.day <= today)))


def test_chain5_payment_date_period_runs_to_its_end_then_packages():
    old = bl.Regime()
    new = bl.Regime(effective_from=D(2026, 10, 21), mode=bl.LESSONS, package_size=12, defer=True)
    base, changed = chain5(D(2026, 10, 21), [old]), chain5(D(2026, 10, 21), [old, new])
    assert (changed.debt, changed.advance, changed.covered_through) == (0, 0, D(2026, 11, 14)) == (base.debt, base.advance, base.covered_through)
    assert changed.mode == bl.CALENDAR and changed.charged == base.charged == 600_000
    # without the deferral (old behaviour) the running period is cut on the change day
    cut = chain5(D(2026, 10, 21), [old, bl.Regime(effective_from=D(2026, 10, 21), mode=bl.LESSONS, package_size=12)])
    assert cut.charged != 600_000
    # after the period: packages by lessons; the first lesson of the new regime is held on 15.11
    later = chain5(D(2026, 11, 16), [old, new])
    assert later.mode == bl.LESSONS and later.segments[0].days[0] == D(2026, 10, 15) and later.segments[1].kind == "package"
    assert later.reconciles


def test_defer_fixed_day_switches_on_the_next_cell_and_a_new_student_at_once():
    old = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
    new = bl.Regime(effective_from=D(2026, 10, 21), anchor=ps.FIXED_DAY, billing_day=10, defer=True)
    inp = lambda regs, first: bl.LedgerInput(today=D(2026, 11, 20), accounting_start=AS, stays=(bl.Stay(first),), regimes=tuple(regs),
                                             price_at=flat(600_000))
    lg = bl.run_ledger(inp([old, new], D(2026, 10, 3)))
    assert (lg.segments[0].period_start, lg.segments[0].period_end, lg.segments[0].amount) == (D(2026, 10, 1), D(2026, 10, 31), round(600_000 * 29 / 31, -3))
    assert lg.segments[1].days[0] == D(2026, 11, 1) and lg.segments[1].days[-1] == D(2026, 11, 9)       # the grid of day 10 from 01.11
    fresh = bl.run_ledger(inp([old, new], D(2026, 10, 25)))                                              # came after the change
    assert fresh.segments[0].period_start == D(2026, 10, 10) and fresh.segments[0].days[0] == D(2026, 10, 25)


def test_defer_two_changes_in_one_period_meet_in_one_boundary():
    old = bl.Regime()
    mid = bl.Regime(effective_from=D(2026, 10, 18), anchor=ps.FIXED_DAY, billing_day=5, defer=True)
    last = bl.Regime(effective_from=D(2026, 10, 21), anchor=ps.FIXED_DAY, billing_day=1, defer=True)
    inp = bl.LedgerInput(today=D(2026, 12, 1), accounting_start=AS, stays=(bl.Stay(D(2026, 10, 15)),), regimes=(old, mid, last),
                         price_at=flat(600_000))
    lg = bl.run_ledger(inp)
    assert lg.segments[0].days[-1] == D(2026, 11, 14) and lg.segments[1].days[0] == D(2026, 11, 15)
    assert lg.segments[1].period_start == D(2026, 11, 1) or lg.segments[1].period_start == D(2026, 11, 15)
    # the later decision wins: the grid of day 1 (not day 5) applies after the boundary
    assert lg.segments[1].days[-1] == D(2026, 11, 30)


def test_defer_lessons_to_calendar_waits_for_the_package():
    old = bl.Regime(mode=bl.LESSONS, package_size=4)
    new = bl.Regime(effective_from=D(2026, 10, 10), anchor=ps.FIXED_DAY, billing_day=1, defer=True)
    held = [bl.CountedLesson(D(2026, 10, d), f"l{d}") for d in (2, 5, 8, 9, 12, 14, 16, 19)]
    mk = lambda today, n: bl.run_ledger(bl.LedgerInput(today=today, accounting_start=AS, stays=(bl.Stay(D(2026, 10, 1)),),
                                                      regimes=(old, new), price_at=flat(400_000), counted_lessons=tuple(held[:n])))
    # c = 10.10: 4 lessons held (2, 5, 8, 9) -> the package is full: calendar from 11.10 (the day after c)
    lg = mk(D(2026, 10, 20), 8)
    assert [s.kind for s in lg.segments] == ["package", "period"] and lg.segments[1].days[0] == D(2026, 10, 11)
    assert lg.segments[0].days == tuple(D(2026, 10, d) for d in (2, 5, 8, 9))
    # c = 10.10 with only 3 lessons held by then and the 4th held on 12.10: the package closes on 12.10, calendar from 13.10
    held2 = [bl.CountedLesson(D(2026, 10, d), f"l{d}") for d in (2, 5, 8, 12, 14)]
    lg = bl.run_ledger(bl.LedgerInput(today=D(2026, 10, 20), accounting_start=AS, stays=(bl.Stay(D(2026, 10, 1)),),
                                      regimes=(old, new), price_at=flat(400_000), counted_lessons=tuple(held2)))
    assert lg.segments[0].days[-1] == D(2026, 10, 12) and lg.segments[1].days[0] == D(2026, 10, 13)
    # the package is still open (only 2 lessons held): the old regime goes on
    lg = bl.run_ledger(bl.LedgerInput(today=D(2026, 10, 20), accounting_start=AS, stays=(bl.Stay(D(2026, 10, 1)),),
                                      regimes=(old, new), price_at=flat(400_000), counted_lessons=tuple(held2[:2])))
    assert [s.kind for s in lg.segments] == ["package"] and lg.mode == bl.LESSONS


# ---- lesson_cover (what payroll reads) ------------------------------------------------------------------------------------------------


def test_lesson_cover_prices_sources_and_before_start():
    price = flat(600_000, changes={D(2026, 10, 15): 700_000})
    lg = bl.run_ledger(bl.LedgerInput(
        today=D(2026, 11, 20), accounting_start=D(2026, 9, 20), stays=(bl.Stay(D(2026, 9, 1)),), regimes=(FIXED1,), price_at=price,
        credits=(credit("p1", D(2026, 10, 1), 600_000), credit("p2", D(2026, 11, 2), 700_000))))
    c = lg.lesson_cover(D(2026, 10, 17))
    assert (c.paid, c.source, c.price.amount) == (True, "money", 600_000)           # the price of the PERIOD that carries it (G-F1)
    assert lg.lesson_cover(D(2026, 11, 17)).price.amount == 700_000
    b = lg.lesson_cover(D(2026, 9, 10))
    assert (b.paid, b.source, b.price) == (True, "before_start", None)
    assert lg.lesson_paid(D(2026, 10, 17))
    assert lg.lesson_cover(D(2026, 11, 25)).source == "unpaid" and not lg.lesson_cover(D(2026, 11, 25)).paid


def test_lesson_cover_writeoff_source():
    lg = bl.run_ledger(bl.LedgerInput(
        today=D(2026, 10, 31), accounting_start=AS, stays=(bl.Stay(D(2026, 10, 1)),), regimes=(FIXED1,), price_at=flat(620_000),
        credits=(credit("p1", D(2026, 10, 1), 500_000), bl.Credit("w1", bl.WRITEOFF, D(2026, 10, 31), D(2026, 10, 31), 120_000))))
    assert lg.covered_through == D(2026, 10, 31)
    assert lg.lesson_cover(D(2026, 10, 10)).source == "money"
    assert lg.lesson_cover(D(2026, 10, 28)).source == "writeoff" and lg.lesson_cover(D(2026, 10, 28)).paid
    unpaid = bl.run_ledger(bl.LedgerInput(today=D(2026, 10, 31), accounting_start=AS, stays=(bl.Stay(D(2026, 10, 1)),),
                                          regimes=(FIXED1,), price_at=flat(620_000), credits=(credit("p1", D(2026, 10, 1), 500_000),)))
    assert unpaid.lesson_cover(D(2026, 10, 28)).source == "unpaid" and not unpaid.lesson_cover(D(2026, 10, 28)).paid


def test_lesson_cover_in_lessons_mode_uses_the_package_price():
    reg = bl.Regime(mode=bl.LESSONS, package_size=2)
    held = [bl.CountedLesson(D(2026, 10, d), f"l{d}") for d in (2, 5, 8)]
    price = flat(200_000, changes={D(2026, 10, 7): 400_000})
    lg = bl.run_ledger(bl.LedgerInput(today=D(2026, 10, 10), accounting_start=AS, stays=(bl.Stay(D(2026, 10, 1)),), regimes=(reg,),
                                      price_at=price, counted_lessons=tuple(held), credits=(credit("p", D(2026, 10, 1), 200_000),)))
    first, third = lg.lesson_cover(D(2026, 10, 2), "l2"), lg.lesson_cover(D(2026, 10, 8), "l8")
    assert (first.paid, first.source, first.price.amount) == (True, "money", 200_000)       # the price of the first lesson of its package
    assert (third.paid, third.source, third.price.amount) == (False, "unpaid", 400_000)
