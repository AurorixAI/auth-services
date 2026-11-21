"""PAY-TAILS А6.2: the property.  Any permutation of the dates (``on``, ``created_at``, entered day) of the credits that are tied to ONE nominal period changes neither ``debt`` nor
``advance`` nor ``covered_through`` nor ``written_off`` (pure ledger, no database; many random accounts, a fixed seed).

The world: a personal period 01.11 - 30.11, 420 000; the stay is cut on a random day (the actual sum is 420 000 x days / 30), the credits (payments and forgivenesses, together at most 420 000) are
all tied to that one period.  Money first, then the forgiveness - whatever the order they were entered in."""
import random
from datetime import date, datetime, timedelta

from dataclasses import replace

from src.auth import billing_ledger as bl
from src.auth import payment_status as ps

from tests.pay2_ledger_helpers import D, T0, flat, make, weekly

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


# ---------------------------------------------------------------------------------------------------------------------------- PAY-TAILS А7.2: the wider world
# Two or three periods of the common month (billing day 1, 600 000, Tue / Thu / Fri) or of the personal period (420 000 from the day of joining); a pause entered before or after the credits; the entry day
# of a credit later than its date; a deduction for a cancelled lesson in some worlds.  The property: «a payment entered last does not take back a forgiveness that was already USED, if no period was cut».
# «Not cut» = the stay is open, no deduction, every pause was entered before the first credit (the filter reads the input, not the code under test; the test fails if the filter leaves too few worlds).

TTF = weekly((1, 3, 4))
FIXED1 = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)


def world(rng):
    common = rng.random() < 0.6
    price = 600_000 if common else 420_000
    today = D(2026, 11, 3) + timedelta(days=rng.randint(0, 45))
    base = make(today=today, stays=[bl.Stay(D(2026, 10, 1))], price_at=flat(price), regimes=[FIXED1] if common else [bl.Regime()], start=D(2026, 9, 1), lesson_days=TTF)
    pauses = ()
    if rng.random() < 0.7:
        first = D(2026, 10, 5) + timedelta(days=rng.randint(0, 40))
        last = first + timedelta(days=rng.randint(2, 18))
        entered_pause = rng.choice([D(2026, 9, 25), D(2026, 10, 4)]) if rng.random() < 0.7 else first + timedelta(days=rng.randint(10, 40))     # mostly BEFORE the credits, sometimes after
        pauses = (bl.Pause(first, last, created_on=entered_pause),)
    credits = []
    for i in range(rng.randint(1, 4)):
        on = D(2026, 10, 6) + timedelta(days=rng.randint(0, (today - D(2026, 10, 6)).days))
        entered = min(today, on + timedelta(days=rng.choice([0, 0, 1, 3])))
        kind = rng.choice([bl.PAYMENT, bl.WRITEOFF])
        credits.append(bl.Credit(f"c{i}", kind, on, T0 + timedelta(minutes=i), rng.choice([100_000, 150_000, 200_000, 300_000, price]), entered_on=entered))
    if pauses and len(credits) >= 2 and rng.random() < 0.25:                      # PAY-TAILS Б6.9: the pause is entered BETWEEN the credits (after the first was entered, before the last)
        entered = sorted(c.entered_on for c in credits)
        pauses = (replace(pauses[0], created_on=entered[0] + (entered[-1] - entered[0]) // 2),)
    inp = replace(base, credits=tuple(credits), pauses=pauses)
    if common and rng.random() < 0.3:
        cancel = D(2026, 10, 13) + timedelta(days=7 * rng.randint(0, 3))
        inp = replace(inp, centre_cancels=(bl.CentreCancel(cancel, "l"),), recalc_versions=(bl.RecalcVersion(date.min, True),))
    return inp


def uncut(inp, L, entered):
    """Nothing cut a period after the first credit was entered: the stay is open, no deduction, every pause was entered by then (read from the INPUT, not from the code under test)."""
    return not L.recalcs and all(p.created_on is not None and p.created_on <= entered for p in inp.pauses)


def test_a_payment_entered_last_never_takes_back_a_used_forgiveness_when_nothing_was_cut():
    rng = random.Random(20261122)
    checked = with_forgiveness = backdated = 0
    for trial in range(1500):
        inp = world(rng)
        before = bl.run_ledger(inp)
        assert before.reconciles and not before.flags, (trial, before.flags)
        assert before.advance == before.refundable and before.advance <= sum(c.amount for c in inp.credits if c.kind != bl.WRITEOFF)
        on_late = max(inp.today, max(c.on for c in inp.credits))
        if rng.random() < 0.4:                                                          # Б6.9: a late payment DATED BACK (entered today, dated a day inside the account): the order of the dates puts it before the others
            earliest = min(c.on for c in inp.credits)
            on_late = earliest + timedelta(days=rng.randint(0, (inp.today - earliest).days))
        late = bl.Credit("late", bl.PAYMENT, on_late, T0 + timedelta(minutes=999), rng.choice([100_000, 200_000, 420_000, 600_000]), entered_on=inp.today)
        after = bl.run_ledger(replace(inp, credits=(*inp.credits, late)))
        assert after.reconciles and not after.flags, (trial, after.flags)
        # Б6.9: the filter read from the INPUT: the payment must not move the CHARGE, nothing is cut by a deduction, no forgiveness is dated AFTER the late payment (the order of the dates puts the money first: the
        # documented class «a payment backdated after a forgiveness», see ``test_a_payment_entered_backdated_after_the_forgiveness...``) and every pause was entered before the first credit (the class of ``test_k1_known_deviation``)
        first_entered = min(c.entered_on or c.on for c in inp.credits)
        wo_days = [c.on for c in inp.credits if c.kind == bl.WRITEOFF]
        if after.charged != before.charged or after.recalcs or (wo_days and late.on < max(wo_days)) or not uncut(inp, after, first_entered):
            continue
        checked += 1
        with_forgiveness += before.written_off > 0
        backdated += late.on < inp.today
        assert after.written_off >= before.written_off, (trial, before.written_off, after.written_off, inp.credits, inp.pauses)
    assert checked > 300 and with_forgiveness > 100 and backdated > 40, (checked, with_forgiveness)           # the filter must not make the property empty


def test_k1_known_deviation_a_pause_entered_between_the_forgiveness_and_a_payment_dated_back_leaves_a_debt_as_it_is():
    # ОМ 600 000 (Tue / Thu / Fri).  October with a pause 15.10 - 31.10 costs 257 000 (the lessons that are left of 600 000).  05.11: October is forgiven for 300 000.  07.11: the pause is ENTERED (backdated).
    # 08.11: a payment of 600 000 is entered DATED 01.11.  By the order of the dates the payment comes first and its capacity counts the pause (entered by 08.11): October 257 000, November 343 000; the forgiveness
    # of 05.11 lies on October, which the money has filled: not used (0 of 300 000).  November 600 000 - 343 000 = DEBT 257 000.  Written as it IS (the owner decides in ROADMAP); the neighbours are fine:
    # the same pause entered BEFORE the forgiveness (25.09) or the payment dated the day it is entered (08.11) give debt 0 and 257 000 used.
    inp = make(today=D(2026, 11, 10), stays=[bl.Stay(D(2026, 10, 1))], price_at=flat(600_000), regimes=[FIXED1], start=D(2026, 9, 1), lesson_days=TTF)
    forgive = bl.Credit("w", bl.WRITEOFF, D(2026, 11, 5), T0, 300_000, entered_on=D(2026, 11, 5))
    pay = bl.Credit("p", bl.PAYMENT, D(2026, 11, 1), T0 + timedelta(minutes=1), 600_000, entered_on=D(2026, 11, 8))

    def run(pause_entered, payment):
        L = bl.run_ledger(replace(inp, credits=(forgive, payment), pauses=(bl.Pause(D(2026, 10, 15), D(2026, 10, 31), created_on=pause_entered),)))
        return L.charged, L.debt, L.advance, L.written_off, L.written_off_granted

    assert run(D(2026, 11, 7), pay) == (857_000, 257_000, 0, 0, 300_000)                      # the deviation: debt 257 000, forgiveness not used
    assert run(D(2026, 9, 25), pay) == (857_000, 0, 0, 257_000, 300_000)                       # the pause known before: debt 0
    assert run(D(2026, 11, 7), replace(pay, on=D(2026, 11, 8))) == (857_000, 0, 0, 257_000, 300_000)     # the payment dated the day it is entered: debt 0
