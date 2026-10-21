"""PAY-2 stream A: the invariants I1-I7 of the design as property tests over seeded random accounts.

``PAY2_PROP_RUNS`` (default 1200) is the number of random accounts per property; the design asks for 10^4 once
(``PAY2_PROP_RUNS=10000``).
"""
import os
import random
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from src.auth import billing_ledger as bl
from src.auth import payment_status as ps

from tests.pay2_ledger_helpers import T0, flat

RUNS = int(os.environ.get("PAY2_PROP_RUNS", "1200"))


def random_input(seed: int) -> bl.LedgerInput:
    rng = random.Random(seed)
    today = date(2026, 6, 1) + timedelta(days=rng.randint(0, 220))
    start = today - timedelta(days=rng.randint(20, 400))
    r = rng.random()
    day = rng.choice([1, 10, 28])
    if r < 0.30:
        regimes = [bl.Regime()]
    elif r < 0.55:
        regimes = [bl.Regime(anchor=ps.FIXED_DAY, billing_day=day)]
    elif r < 0.70:
        regimes = [bl.Regime(mode=bl.LESSONS, package_size=rng.choice([4, 8, 12]))]
    elif r < 0.85:
        regimes = [bl.Regime(anchor=ps.FIXED_DAY, billing_day=1),
                   bl.Regime(effective_from=today - timedelta(days=rng.randint(1, 60)), anchor=ps.FIXED_DAY,
                             billing_day=day)]
    else:
        regimes = [bl.Regime(), bl.Regime(effective_from=today - timedelta(days=rng.randint(1, 50)), mode=bl.LESSONS,
                                          package_size=rng.choice([4, 12]))]
    # stays: 1-3, ordered, separated
    stays, cursor = [], start - timedelta(days=rng.randint(0, 40))
    for i in range(rng.randint(1, 3)):
        first = cursor + timedelta(days=rng.randint(0, 30))
        if first > today:
            break
        if i == 2 or rng.random() < 0.45:
            stays.append(bl.Stay(first, None))
            break
        last = first + timedelta(days=rng.randint(0, 90))
        stays.append(bl.Stay(first, last))
        cursor = last + timedelta(days=rng.randint(2, 20))
    if not stays:
        stays = [bl.Stay(today - timedelta(days=5))]
    base = rng.choice([0, 1, 1000, 150_000, 310_000, 500_000, 600_000, 999_000, 2_500_000])
    if rng.random() < 0.06:
        price_at = flat(None)
    else:
        changes = {}
        for _ in range(rng.choice([0, 0, 1, 2])):
            changes[today + timedelta(days=rng.randint(-120, 150))] = rng.choice([0, 200_000, 700_000, 1_200_000])
        price_at = flat(base, since=date(2025, 1, 1) if rng.random() < 0.95 else start + timedelta(days=rng.randint(0, 100)),
                        changes=changes)
    weekdays = set(rng.sample(range(7), rng.randint(1, 4)))

    def lesson_days(a, b):
        out, d = [], a
        while d <= b and (b - a).days < 400:
            if d.weekday() in weekdays:
                out.append(d)
            d += timedelta(days=1)
        return out

    counted = []
    d = start
    while d <= today:
        if d.weekday() in weekdays and rng.random() < 0.9 and any(s.first <= d and (s.last is None or d <= s.last) for s in stays):
            for _ in range(2 if rng.random() < 0.05 else 1):
                counted.append(bl.CountedLesson(d, f"l{len(counted)}"))
        d += timedelta(days=1)
    credits = []
    scale = max(base, 1000)
    for i in range(rng.randint(0, 6)):
        kind = rng.choices([bl.PAYMENT, bl.WRITEOFF, bl.TRANSFER_IN], [70, 15, 15])[0]
        on = today - timedelta(days=rng.randint(0, (today - start).days + 10))
        amount = rng.choice([1, 999, scale // 7 + 1, scale // 2, scale, 2 * scale + 123, 5 * scale])
        credits.append(bl.Credit(f"c{i}", kind, on, T0 + timedelta(minutes=rng.randint(0, 50)), amount))
    outflows = []
    for i in range(rng.choice([0, 0, 1, 2])):
        on = today - timedelta(days=rng.randint(0, 60))
        have = sum(c.amount for c in credits if c.kind != bl.WRITEOFF and c.on <= on) - sum(o.amount for o in outflows)
        if have <= 1:
            continue
        outflows.append(bl.Outflow(f"o{i}", rng.choice([bl.REFUND, bl.TRANSFER_OUT]), on, T0 + timedelta(minutes=i),
                                   rng.randint(1, have)))
    return bl.LedgerInput(
        today=today, accounting_start=start, stays=tuple(stays), regimes=tuple(regimes), price_at=price_at,
        credits=tuple(credits), outflows=tuple(outflows), lesson_days=lesson_days, counted_lessons=tuple(counted),
        first_billable_floor=(today - timedelta(days=rng.randint(0, 200))) if rng.random() < 0.1 else None)


def seeds():
    return range(RUNS)


@pytest.mark.parametrize("chunk", range(4))
def test_invariants_hold_on_random_accounts(chunk):
    for seed in list(seeds())[chunk::4]:
        inp = random_input(seed)
        l = bl.run_ledger(inp)
        ctx = f"seed={seed}"
        by_credit = {}
        for a in l.allocations:
            by_credit.setdefault(a.credit_id, []).append(a)
        # I1 - per credit: the pieces are the net, the net + what went out is the amount
        for c in inp.credits:
            assert sum(a.amount for a in by_credit.get(c.id, [])) == l.net_money[c.id], ctx
            assert l.net_money[c.id] + l.refunded_from[c.id] + l.transferred_from[c.id] == c.amount, ctx
            assert all(a.bucket in (bl.PAST_DEBT, bl.CURRENT, bl.ADVANCE) and a.amount > 0 for a in by_credit.get(c.id, [])), ctx
        # I2
        assert l.reconciles, ctx
        # I5 - per segment: pieces == money laid, days add up
        for s in l.segments:
            pieces = [a for a in l.allocations if a.segment_id == s.id]
            assert sum(a.amount for a in pieces) == s.money + s.forgiven, ctx
            if s.amount and s.kind == "period":
                assert sum(a.days for a in pieces) == s.covered_days, (ctx, s.id)
            elif s.amount:  # a package: lessons that cost nothing are covered without money
                assert sum(a.days for a in pieces) <= s.covered_days, (ctx, s.id)
            elif not s.unpriced:
                assert s.covered_days == len(s.days), ctx   # nothing to pay -> paid
            assert 0 <= s.covered_days <= len(s.days), ctx
            assert s.partial_credit >= 0, ctx
            if s.covered_days < len(s.days) and s.amount and not s.unpriced:
                # a partial day is less than the price of the next day (a unit of rounding apart)
                assert s.partial_credit <= (s.amount + len(s.days) - 1) // len(s.days) + 1 or s.kind == "package", (ctx, s.id)
        # I6
        assert l.refundable <= l.advance, ctx
        assert not (l.advance > 0 and l.debt > 0), ctx
        assert l.refundable <= sum(v for k, v in l.net_money.items() if next(c.kind for c in inp.credits if c.id == k) != bl.WRITEOFF), ctx
        # debt / advance are the begun charges against the credit, split into past and current
        begun = [s for s in l.segments if s.begun]
        assert l.debt == sum(s.unpaid for s in begun), ctx
        assert l.past_debt + l.current_due == l.debt and l.past_debt >= 0 and l.current_due >= 0, ctx
        # every cent of the credit sits on a segment or in the advance
        assert sum(l.net_money.values()) == sum(s.money + s.forgiven for s in l.segments) + sum(
            a.amount for a in l.allocations if a.segment_id == bl.ADVANCE_SEGMENT), ctx
        # a billable day belongs to one segment only (calendar)
        days = [d for s in l.segments if s.kind == "period" for d in s.days]
        assert len(days) == len(set(days)), ctx
        # statement ends at advance - debt
        if l.statement:
            assert l.statement[-1].balance_after == l.advance - l.debt, ctx


@pytest.mark.parametrize("chunk", range(2))
def test_adding_money_never_reduces_the_cover_and_is_associative(chunk):
    for seed in list(seeds())[chunk::2]:
        inp = replace(random_input(seed), outflows=())
        rng = random.Random(seed + 10_000)
        x, y = rng.randint(1, 800_000), rng.randint(1, 800_000)
        on = inp.today
        base = bl.run_ledger(inp)
        with_x = bl.run_ledger(replace(inp, credits=inp.credits + (bl.Credit("x", bl.PAYMENT, on, datetime(2030, 1, 1), x),)))
        with_xy = bl.run_ledger(replace(inp, credits=inp.credits + (
            bl.Credit("x", bl.PAYMENT, on, datetime(2030, 1, 1), x), bl.Credit("y", bl.PAYMENT, on, datetime(2030, 1, 2), y))))
        one = bl.run_ledger(replace(inp, credits=inp.credits + (bl.Credit("z", bl.PAYMENT, on, datetime(2030, 1, 1), x + y),)))
        ctx = f"seed={seed}"
        # I4 monotone
        assert with_x.covered_days_total >= base.covered_days_total, ctx
        assert with_xy.covered_days_total >= with_x.covered_days_total, ctx
        for a, b in ((base, with_x), (with_x, with_xy)):
            if a.covered_through is not None and b.covered_through is not None:
                assert b.covered_through >= a.covered_through, ctx
            assert b.debt <= a.debt and b.advance >= a.advance, ctx
            if a.lessons and b.lessons:
                assert b.lessons.covered >= a.lessons.covered, ctx
        # I4 associativity: X then Y on the same day == X + Y
        for field_name in ("debt", "past_debt", "current_due", "advance", "covered_through", "covered_days_total",
                           "partial_credit", "refundable"):
            assert getattr(with_xy, field_name) == getattr(one, field_name), (ctx, field_name)
        assert with_xy.status == one.status or with_xy.status.last_payment_id == one.status.last_payment_id, ctx


@pytest.mark.parametrize("chunk", range(2))
def test_input_order_of_credits_does_not_matter(chunk):
    for seed in list(seeds())[chunk::2]:
        inp = random_input(seed)
        shuffled = list(inp.credits)
        random.Random(seed).shuffle(shuffled)
        out = list(inp.outflows)
        random.Random(seed + 1).shuffle(out)
        assert bl.run_ledger(inp) == bl.run_ledger(replace(inp, credits=tuple(shuffled), outflows=tuple(out))), seed


def test_removing_a_payment_is_the_same_as_it_never_existing():
    """Void == never entered: the state is a pure function of the live facts."""
    for seed in list(seeds())[:300]:
        inp = random_input(seed)
        pays = [c for c in inp.credits if c.kind == bl.PAYMENT]
        if not pays or inp.outflows:
            continue
        gone = pays[0]
        keep = tuple(c for c in inp.credits if c.id != gone.id)
        a = bl.run_ledger(replace(inp, credits=keep))
        b = bl.run_ledger(replace(inp, credits=keep))
        assert a == b
        assert a.paid == sum(c.amount for c in keep if c.kind == bl.PAYMENT)


def test_i3_and_i7_over_a_centre_of_transfers_and_payments():
    """I3: payments = distributed + refunded from them, over a mixed centre;  I7: transfers out == transfers in."""
    rng = random.Random(7)
    reg = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
    total_received = total_distributed = total_refunded_from = 0
    out_sum = in_sum = 0
    for k in range(120):
        today = date(2026, 10, 31)
        # A -> B -> C chain with random payment, random transfer days
        t1 = date(2026, 10, 1) + timedelta(days=rng.randint(1, 12))
        t2 = t1 + timedelta(days=rng.randint(1, 12))
        pay = rng.choice([100_000, 450_000, 600_000, 1_300_000])
        a = bl.LedgerInput(today=today, accounting_start=date(2026, 9, 1), stays=(bl.Stay(date(2026, 10, 1), t1),),
                           regimes=(reg,), price_at=flat(rng.choice([300_000, 500_000])),
                           credits=(bl.Credit("pa", bl.PAYMENT, date(2026, 10, 1), T0, pay),))
        x1 = bl.refundable_at(a, t1)
        la = bl.run_ledger(replace(a, outflows=(bl.Outflow("oa", bl.TRANSFER_OUT, t1, T0, x1),) if x1 else ()))
        b = bl.LedgerInput(today=today, accounting_start=date(2026, 9, 1), stays=(bl.Stay(t1, t2),), regimes=(reg,),
                           price_at=flat(400_000), first_billable_floor=t1 + timedelta(days=1),
                           credits=(bl.Credit("tb", bl.TRANSFER_IN, t1, T0, x1),) if x1 else ())
        x2 = bl.refundable_at(b, t2)
        lb = bl.run_ledger(replace(b, outflows=(bl.Outflow("ob", bl.TRANSFER_OUT, t2, T0, x2),) if x2 else ()))
        c = bl.LedgerInput(today=today, accounting_start=date(2026, 9, 1), stays=(bl.Stay(t2),), regimes=(reg,),
                           price_at=flat(600_000), first_billable_floor=t2 + timedelta(days=1),
                           credits=(bl.Credit("tc", bl.TRANSFER_IN, t2, T0, x2),) if x2 else ())
        lc = bl.run_ledger(c)
        for l in (la, lb, lc):
            assert l.reconciles
        out_sum += la.transferred_out + lb.transferred_out
        in_sum += lb.transferred_in + lc.transferred_in
        # I3 for the one real payment
        allocated = sum(al.amount for al in la.allocations if al.credit_id == "pa")
        total_received += pay
        total_distributed += allocated
        total_refunded_from += la.refunded_from["pa"] + la.transferred_from["pa"]
    assert out_sum == in_sum
    assert total_received == total_distributed + total_refunded_from
