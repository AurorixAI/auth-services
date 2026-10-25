"""LOGIC-A: the global invariants G1-G10 of the spec (§1.9) as property tests over seeded random accounts.

G1 / G4 / G5 also run inside the PAY-2 sweep (``test_billing_ledger_properties.py``, ``logic_a=True``: the same random accounts plus pauses,
an opening balance and deferred regime versions).  ``LOGICA_PROP_RUNS`` (default 1500) is the number of accounts per property.
"""
import os
import random
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from src.auth import billing_ledger as bl
from src.auth import payment_status as ps

from tests.pay2_ledger_helpers import T0, flat
from tests.test_billing_ledger_properties import random_input

RUNS = int(os.environ.get("LOGICA_PROP_RUNS", "1500"))
SOURCES = {"money", "writeoff", "before_start", "unpaid", "unpriced"}


def simple(seed: int, *, anchor=ps.FIXED_DAY, flat_price=True):
    """A calendar account that has no price changes and no regime change (where the monotone statements of G3 hold)."""
    rng = random.Random(seed)
    today = date(2026, 6, 1) + timedelta(days=rng.randint(0, 220))
    first = today - timedelta(days=rng.randint(0, 150))
    last = None if rng.random() < 0.6 else min(today + timedelta(days=rng.randint(-5, 30)), first + timedelta(days=rng.randint(0, 150)))
    if last is not None and last < first:
        last = first
    price = rng.choice([300_000, 600_000, 620_000, 1_000_000])
    reg = bl.Regime(anchor=anchor, billing_day=rng.choice([1, 1, 10]))
    credits = tuple(bl.Credit(f"c{i}", bl.PAYMENT, first + timedelta(days=rng.randint(0, max(0, (today - first).days))),
                              T0 + timedelta(minutes=i), rng.choice([price // 3, price, price * 2 + 7_000])) for i in range(rng.randint(0, 3)))
    inp = bl.LedgerInput(today=today, accounting_start=first - timedelta(days=rng.randint(0, 40)), stays=(bl.Stay(first, last),),
                         regimes=(reg,), price_at=flat(price), credits=credits)
    pauses = []
    for _ in range(rng.randint(1, 2)):
        a = first + timedelta(days=rng.randint(0, 120))
        pauses.append(bl.Pause(a, a + timedelta(days=rng.randint(0, 40))))
    return inp, tuple(pauses)


def pause_days(inp):
    out = set()
    for p in inp.pauses:
        d = p.first
        while d <= p.last:
            out.add(d)
            d += timedelta(days=1)
    return out


@pytest.mark.parametrize("chunk", range(3))
def test_g1_g2_g9_every_lesson_has_exactly_one_cover_and_one_price(chunk):
    for seed in list(range(RUNS))[chunk::3]:
        inp = random_input(seed, True)
        lg = bl.run_ledger(inp)
        ctx = f"seed={seed}"
        assert lg.reconciles, ctx                                                                 # G1
        days = [d for s in lg.segments if s.kind == "period" for d in s.days]
        assert len(days) == len(set(days)), ctx                                                   # G2: a day in one segment
        assert all(s.covered_days <= len(s.days) for s in lg.segments), ctx
        assert not (set(days) & pause_days(inp)), ctx                                             # G3: billable days are outside pauses
        for s in lg.segments:
            if s.kind != "period" or not s.days:
                continue
            by_money = bl._cover(s, s.money)
            for i, d in enumerate(s.days):
                info = lg.lesson_cover(d)
                assert info.source in SOURCES, (ctx, d)
                if not s.unpriced:
                    assert info.price == s.price and info.segment_id == s.id, (ctx, d)           # G9: one base per segment
                    if info.paid:
                        assert info.source == ("money" if i < by_money else "writeoff"), (ctx, d)
                    else:
                        assert info.source == "unpaid", (ctx, d)
        for lesson in lg.counted:
            info = lg.lesson_cover(lesson.day, lesson.lesson_id)
            assert info.source in SOURCES, ctx
            if info.segment_id:
                seg = next(x for x in lg.segments if x.id == info.segment_id)
                assert lesson.day in seg.days and info.price == seg.price, ctx
            assert info.paid == lg.lesson_paid(lesson.day, lesson.lesson_id), ctx


@pytest.mark.parametrize("anchor", [ps.FIXED_DAY, ps.PAYMENT_DATE])
@pytest.mark.parametrize("chunk", range(2))
def test_g3_a_pause_never_adds_a_charge_and_void_restores_the_account(chunk, anchor):
    """Owner decision 21.10: a pause never adds a charge - in BOTH modes (payment date: the pause extends the period it falls into and the
    later periods follow, the chain is not restarted)."""
    for seed in list(range(RUNS))[chunk::2]:
        inp, pauses = simple(seed, anchor=anchor)
        base = bl.run_ledger(inp)
        paused = bl.run_ledger(replace(inp, pauses=pauses))
        ctx = f"seed={seed}"
        assert paused.charged <= base.charged, ctx
        assert not ({d for s in paused.segments for d in s.days} & pause_days(replace(inp, pauses=pauses))), ctx
        assert paused.reconciles, ctx
        if anchor == ps.PAYMENT_DATE:
            # the same number of study days is paid for the same price: a period that is not cut by the end of the stay has exactly the
            # nominal number of study days, and consecutive periods touch (no gap, no overlap)
            segs = [x for x in paused.segments if x.kind == "period"]
            for left, right in zip(segs, segs[1:]):
                assert right.period_start == left.days[-1] + timedelta(days=1), ctx
            for x in segs[:-1]:
                assert len(x.days) == x.nominal_days, ctx
        assert bl.run_ledger(replace(paused.__class__ and inp, pauses=())) == base, ctx           # void o create = id
        # money buys days, not dates: the paid-until date does not move earlier when days are taken out (no refund in the account)
        if base.covered_through is not None and paused.covered_through is not None and inp.stays[0].last is None:
            # only the rounding of the charge to 1 000 (the amount of the shortened period is rounded again) can move it, by a day or two
            assert paused.covered_through >= base.covered_through - timedelta(days=2), ctx


@pytest.mark.parametrize("chunk", range(2))
def test_g4_order_of_pauses_and_credits_does_not_matter(chunk):
    for seed in list(range(RUNS))[chunk::2]:
        inp = random_input(seed, True)
        rng = random.Random(seed)
        shuffled = replace(inp, pauses=tuple(rng.sample(list(inp.pauses), len(inp.pauses))),
                           credits=tuple(rng.sample(list(inp.credits), len(inp.credits))))
        assert bl.run_ledger(inp) == bl.run_ledger(shuffled), seed


def test_g6_paid_until_alone_creates_neither_debt_nor_advance():
    for seed in range(RUNS):
        inp, _ = simple(seed)
        rng = random.Random(seed + 5)
        paid = inp.today + timedelta(days=rng.randint(0, 40))
        lg = bl.run_ledger(replace(inp, credits=(), opening=bl.Opening(paid_until=paid), stays=(bl.Stay(inp.stays[0].first),)))
        assert (lg.debt, lg.advance, lg.refundable) == (0, 0, 0), seed
        assert lg.covered_through is not None and lg.covered_through >= paid or lg.charged > 0 or lg.covered_through is None, seed
        assert lg.covered_through == paid or lg.covered_through > paid, seed
        assert lg.reconciles, seed


def test_g7_opening_debt_is_paid_first():
    for seed in range(RUNS):
        inp, _ = simple(seed)
        rng = random.Random(seed + 9)
        x = rng.choice([1, 99_000, 250_000, 5_000_000])
        m = rng.choice([0, 1, x - 1, x, x + 1, 3 * x])
        first = inp.stays[0].first
        t = first - timedelta(days=rng.randint(1, 30))
        credits = (bl.Credit("m", bl.PAYMENT, max(first, t), T0, m),) if m else ()
        lg = bl.run_ledger(replace(inp, credits=credits, opening=bl.Opening(debt_amount=x, debt_on=t)))
        seg = next(s for s in lg.segments if s.kind == "opening")
        assert seg.money == min(m, x) and seg.unpaid == x - min(m, x), seed
        if seg.unpaid and t < inp.today:
            assert lg.past_debt >= seg.unpaid, seed
        assert lg.reconciles, seed
        assert lg.charged >= x, seed


@pytest.mark.parametrize("chunk", range(3))
def test_g10_a_deferred_regime_leaves_the_begun_periods_as_they_were(chunk):
    rng0 = random.Random(77)
    checked = 0
    for seed in list(range(RUNS))[chunk::3]:
        rng = random.Random(seed)
        today = date(2026, 9, 1) + timedelta(days=rng.randint(30, 200))
        first = today - timedelta(days=rng.randint(10, 150))
        c = today - timedelta(days=rng.randint(0, (today - first).days))
        old = rng.choice([bl.Regime(), bl.Regime(anchor=ps.FIXED_DAY, billing_day=rng.choice([1, 10])), bl.Regime(mode=bl.LESSONS, package_size=rng.choice([4, 8]))])
        new = bl.Regime(effective_from=c, mode=rng.choice([bl.CALENDAR, bl.LESSONS]), anchor=rng.choice([ps.PAYMENT_DATE, ps.FIXED_DAY]),
                        billing_day=rng.choice([1, 15]), package_size=rng.choice([4, 12]), defer=True)
        lessons = []
        d = first
        while d <= today:
            if rng.random() < 0.4:
                lessons.append(bl.CountedLesson(d, f"l{len(lessons)}"))
            d += timedelta(days=1)
        base = bl.LedgerInput(today=today, accounting_start=first - timedelta(days=3), stays=(bl.Stay(first),), regimes=(old,),
                              price_at=flat(rng.choice([300_000, 600_000])), counted_lessons=tuple(lessons),
                              lesson_days=lambda a, b: ())
        lg_old = bl.run_ledger(base)
        lg_new = bl.run_ledger(replace(base, regimes=(old, new)))
        assert lg_new.reconciles, seed
        key = lambda s: (s.kind, s.period_start, s.days, s.amount, s.price.amount)
        have = {key(s) for s in lg_new.segments}
        for s in lg_old.segments:
            began = (s.days[0] if s.days else s.period_start)
            # a student whose first day IS the day of the change has no running period: his first period goes by the new mode (batch 25.10); every other begun period is kept
            if (began < c or (began <= c and first < c)) and s.begun and not (old.mode == bl.LESSONS and not s.complete_package and any(x.day > c for x in lessons[:0])):
                if old.mode == bl.LESSONS:
                    # a package in progress on the change day is finished under the old regime: compare the ones that are full
                    if not s.complete_package:
                        continue
                assert key(s) in have, (seed, s.id)
                checked += 1
    assert checked > 100


def test_lessons_inside_a_pause_are_not_in_any_package():
    for seed in range(RUNS):
        inp = random_input(seed, True)
        lg = bl.run_ledger(inp)
        paused = pause_days(inp)
        for s in lg.segments:
            if s.kind == "package":
                assert not (set(s.days) & paused), seed


def test_extra_credit_on_a_pause_account_is_monotone():
    for seed in range(0, RUNS, 2):
        inp, pauses = simple(seed)
        inp = replace(inp, pauses=pauses)
        a = bl.run_ledger(inp)
        b = bl.run_ledger(replace(inp, credits=inp.credits + (bl.Credit("x", bl.PAYMENT, inp.today, datetime(2030, 1, 1), 50_000),)))
        assert b.covered_days_total >= a.covered_days_total and b.debt <= a.debt, seed
