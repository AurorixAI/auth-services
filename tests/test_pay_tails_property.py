"""PAY-TAILS А6.2: the property.  Any permutation of the dates (``on``, ``created_at``, entered day) of the credits that are tied to ONE nominal period changes neither ``debt`` nor
``advance`` nor ``covered_through`` nor ``written_off`` (pure ledger, no database; many random accounts, a fixed seed).

The world: a personal period 01.11 - 30.11, 420 000; the stay is cut on a random day (the actual sum is 420 000 x days / 30), the credits (payments and forgivenesses, together at most 420 000) are
all tied to that one period.  Money first, then the forgiveness - whatever the order they were entered in."""
import random
from datetime import date, datetime, timedelta

from src.auth import billing_ledger as bl

from tests.pay2_ledger_helpers import D, T0, flat, make

PRICE = 420_000


def build(stay_last, specs, today):
    credits = [bl.Credit(f"c{i}", kind, on, T0 + timedelta(minutes=minute), amount, entered_on=entered)
               for i, (kind, amount, on, minute, entered) in enumerate(specs)]
    return make(today=today, stays=[bl.Stay(D(2026, 11, 1), stay_last)], price_at=flat(PRICE), credits=credits, start=D(2026, 10, 1))


def outcome(inp):
    L = bl.run_ledger(inp)
    assert L.reconciles and not L.flags, L.flags
    assert L.advance == L.refundable
    return L.debt, L.advance, L.covered_through, L.written_off


def test_permutations_of_dates_inside_one_period_change_nothing():
    rng = random.Random(20261121)
    for trial in range(300):
        last = D(2026, 11, rng.randint(2, 30))
        today = max(last, D(2026, 11, 3)) if rng.random() < 0.5 else D(2026, 11, 30)
        pieces, left = [], PRICE
        while left > 0 and len(pieces) < 6:
            amount = min(left, rng.choice([20_000, 50_000, 100_000, 120_000, 200_000]))
            pieces.append((rng.choice([bl.PAYMENT, bl.WRITEOFF, bl.PAYMENT]), amount))
            left -= amount
        stamps = [(D(2026, 11, rng.randint(1, 28)), rng.randint(0, 5000)) for _ in pieces]
        stamps = [(on, minute, on) for on, minute in stamps]                    # the forgiveness is entered the day it is dated; every day lies after 01.11: the period has begun
        base = [(k, a, *st) for (k, a), st in zip(pieces, stamps)]
        want = outcome(build(last, base, today))
        for _ in range(6):
            shuffled = list(stamps)
            rng.shuffle(shuffled)
            got = outcome(build(last, [(k, a, *st) for (k, a), st in zip(pieces, shuffled)], today))
            assert got == want, (trial, last, base, shuffled, got, want)


def test_a_forgiveness_never_becomes_money():
    rng = random.Random(7)
    for _ in range(300):
        last = D(2026, 11, rng.randint(2, 30))
        money = rng.choice([0, 100_000, 200_000, 300_000, 420_000])
        forgiven = rng.choice([0, 100_000, 220_000, 420_000])
        specs = [(bl.WRITEOFF, min(forgiven, PRICE), D(2026, 11, 1), 1, D(2026, 11, 1))] if forgiven else []
        specs += [(bl.PAYMENT, money, D(2026, 11, 2), 2, D(2026, 11, 2))] if money else []
        L = bl.run_ledger(build(last, specs, D(2026, 11, 30)))
        assert L.advance == L.refundable <= money, (last, money, forgiven, L.advance)
        assert L.written_off <= L.written_off_granted <= max(forgiven, 0)
        assert L.reconciles and not L.flags
