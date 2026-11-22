"""PAY-TAILS А6.2: the property.  Any permutation of the dates (``on``, ``created_at``, entered day) of the credits that are tied to ONE nominal period changes neither ``debt`` nor
``advance`` nor ``covered_through`` nor ``written_off`` (pure ledger, no database; many random accounts, a fixed seed).

The world: a personal period 01.11 - 30.11, 420 000; the stay is cut on a random day (the actual sum is 420 000 x days / 30), the credits (payments and forgivenesses, together at most 420 000) are
all tied to that one period.  Money first, then the forgiveness - whatever the order they were entered in."""
import random
from datetime import date, datetime, timedelta

from dataclasses import replace

from src.auth import billing_ledger as bl
from src.auth import payment_status as ps

from tests.pay2_ledger_helpers import D, T0, at, credit, flat, make, pause, weekly

PRICE = 420_000


def build(stay_last, specs, today):
    credits = [bl.Credit(f"c{i}", kind, on, at(entered, minute), amount) for i, (kind, amount, on, minute, entered) in enumerate(specs)]
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
        pauses = (pause(first, last, entered_pause),)
    credits = []
    for i in range(rng.randint(1, 4)):
        on = D(2026, 10, 6) + timedelta(days=rng.randint(0, (today - D(2026, 10, 6)).days))
        entered = min(today, on + timedelta(days=rng.choice([0, 0, 1, 3])))
        kind = rng.choice([bl.PAYMENT, bl.WRITEOFF])
        credits.append(bl.Credit(f"c{i}", kind, on, at(entered, i), rng.choice([100_000, 150_000, 200_000, 300_000, price])))
    if pauses and len(credits) >= 2 and rng.random() < 0.25:                      # PAY-TAILS Б6.9: the pause is entered BETWEEN the credits (after the first was entered, before the last)
        entered = sorted(bl.entered_day(c) for c in credits)
        pauses = (pause(pauses[0].first, pauses[0].last, entered[0] + (entered[-1] - entered[0]) // 2, 500),)
    inp = replace(base, credits=tuple(credits), pauses=pauses)
    if common and rng.random() < 0.3:
        cancel = D(2026, 10, 13) + timedelta(days=7 * rng.randint(0, 3))
        inp = replace(inp, centre_cancels=(bl.CentreCancel(cancel, "l"),), recalc_versions=(bl.RecalcVersion(date.min, True),))
    return inp


def uncut(inp, L, entered):
    """Nothing cut a period after the first credit was entered: the stay is open, no deduction, every pause was entered by then (read from the INPUT, not from the code under test)."""
    return not L.recalcs and all(p.forms and bl.entered_at(p).date() <= entered for p in inp.pauses)


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
        late = bl.Credit("late", bl.PAYMENT, on_late, at(inp.today, 999), rng.choice([100_000, 200_000, 420_000, 600_000]))
        after = bl.run_ledger(replace(inp, credits=(*inp.credits, late)))
        assert after.reconciles and not after.flags, (trial, after.flags)
        # Б6.9: the filter read from the INPUT: the payment must not move the CHARGE, nothing is cut by a deduction, no forgiveness is dated AFTER the late payment (the order of the dates puts the money first: the
        # documented class «a payment backdated after a forgiveness», see ``test_a_payment_entered_backdated_after_the_forgiveness...``) and every pause was entered before the first credit (the class of ``test_k1_known_deviation``)
        first_entered = min(bl.entered_day(c) for c in inp.credits)
        wo_days = [c.on for c in inp.credits if c.kind == bl.WRITEOFF]
        if after.charged != before.charged or after.recalcs or (wo_days and late.on < max(wo_days)) or not uncut(inp, after, first_entered):
            continue
        checked += 1
        with_forgiveness += before.written_off > 0
        backdated += late.on < inp.today
        assert after.written_off >= before.written_off, (trial, before.written_off, after.written_off, inp.credits, inp.pauses)
    assert checked > 300 and with_forgiveness > 100 and backdated > 40, (checked, with_forgiveness)           # the filter must not make the property empty


def test_k1_fixed_a_pause_entered_between_the_forgiveness_and_a_payment_dated_back_no_longer_leaves_a_debt():
    # Б7-1 (it was the «known deviation» K1: debt 257 000).  ОМ 600 000 (Tue / Thu / Fri).  October with a pause 15.10 - 31.10 costs 257 000 (6 lessons of 14).  05.11: 300 000 are forgiven (dated 05.11).
    # 07.11: the pause is ENTERED.  08.11: a payment of 600 000 is entered DATED 01.11.  The world of 07.11 knew only the forgiveness: it lay on October [0; 300 000], the room became 257 000, 43 000 are cut off:
    # alive 257 000.  The main line: the payment (01.11) - October 257 000 + November 343 000; the forgiveness 257 000 - November (October is full).  November 600 000 = 343 000 + 257 000: debt 0, used 257 000.
    # The neighbours are the same: the pause entered BEFORE the forgiveness (25.09), or the payment dated the day it is entered (08.11) - debt 0, used 257 000.
    inp = make(today=D(2026, 11, 10), stays=[bl.Stay(D(2026, 10, 1))], price_at=flat(600_000), regimes=[FIXED1], start=D(2026, 9, 1), lesson_days=TTF)
    forgive = bl.Credit("w", bl.WRITEOFF, D(2026, 11, 5), at(D(2026, 11, 5)), 300_000)
    pay = bl.Credit("p", bl.PAYMENT, D(2026, 11, 1), at(D(2026, 11, 8), 1), 600_000)

    def run(pause_entered, payment):
        L = bl.run_ledger(replace(inp, credits=(forgive, payment), pauses=(pause(D(2026, 10, 15), D(2026, 10, 31), pause_entered),)))
        return L.charged, L.debt, L.advance, L.written_off, L.written_off_granted

    assert run(D(2026, 11, 7), pay) == (857_000, 0, 0, 257_000, 300_000)
    assert run(D(2026, 9, 25), pay) == (857_000, 0, 0, 257_000, 300_000)
    assert run(D(2026, 11, 7), replace(pay, on=D(2026, 11, 8))) == (857_000, 0, 0, 257_000, 300_000)


# ---------------------------------------------------------------------------------------------------------------------------- PAY-TAILS Б7.2 / Б7.7: a payment never raises the debt (LESSONS 160, 161)
# The worlds are those that the product ALLOWS (``tests/pay_tails_b7_tools.py``: facts with file:line in its header; every credit entered on a day <= today, a forgiveness not larger than the debt of
# its entry day, a pause entered between the credits or the same day after a credit, a pause that runs past today or covers a whole month, a pause edited, a leaving, a payment dated back before a
# forgiveness).  Without a lesson cancelled by the centre (the deduction is the next step) in the common month, the personal period and the packages: P1 (a payment added now), P2 (a forgiveness added
# now), P3 / P3e (a pause entered or edited now), P4 (the oracle that rebuilds the world known at the moment of the forgiveness), P5 (the probe of the window = the record).  The same tool prints the
# table «credit x period» of a world and shrinks a counterexample:  B7_MODE=personal B7_CANCELS=none python tests/pay_tails_b7_tools.py smallest "P3 debt" 1 20000
from tests import pay_tails_b7_tools as tools

_COUNTS = {}
WORLDS = 1500                       # per mode in the suite; the full measure is 5 seeds x 20 000 (the tool)


def _counts(mode):
    if mode not in _COUNTS:
        _COUNTS[mode] = tools.run_counts(1, WORLDS, tools.Cfg(mode, "none"))[0]
    return _COUNTS[mode]


def _zero(keys, modes=("common", "personal", "packages")):
    for mode in modes:
        counts = _counts(mode)
        assert {k: counts[k] for k in keys} == {k: 0 for k in keys}, (mode, counts)


def test_b7_p1_a_payment_added_now_never_raises_the_debt_nor_the_used_forgiveness_and_the_advance_grows_by_no_more_than_the_payment():
    _zero(("P1 debt", "P1 used", "P1 advance", "P1 advance<=money", "P1 identity", "flags"))


def test_b7_p2_a_forgiveness_added_now_never_raises_the_debt_and_the_advance_only_by_a_backdated_one_and_by_no_more_than_its_sum():
    _zero(("P2 debt", "P2 advance<=money", "P2 advance today", "P2 advance back"))


def test_b7_p3_a_pause_entered_or_edited_now_never_raises_the_debt_nor_the_used_forgiveness_and_the_advance_stays_money():
    # The common month and the personal period: all of P3 / P3e.  «The advance grows by no more than the money the cut freed» is counted period by period BY THE KEY OF THE MODEL (the slot of a period: the cell
    # of a common month, the piece of the stay and the number in the chain of a personal period; Б7.8 п. 1): a pause on the whole first personal period moves the chain one month back and the money
    # that lay on the third period, which has not begun now, is freed (an advance in money, the right one).  The packages (Б7.8 п. 2) are a world like the others: their place is their number.
    keys = ("P3 used", "P3 debt", "P3 advance", "P3 advance<=money", "P3e used", "P3e debt", "P3e advance", "P3e advance<=money", "P3c advance", "P3ec advance")
    _zero(keys)


def test_b7_p4_p5_no_forgiveness_lies_on_a_period_that_had_not_begun_and_the_probe_of_the_window_is_the_record():
    _zero(("P4 early", "P4 today", "P5 probe"))
    assert all(_counts(mode)["P4 nomatch"] == 0 for mode in ("common", "personal", "packages"))             # the periods of the older worlds are found by their place in every world (a package: its number)


def test_b7_named_world_a_payment_dated_before_two_forgivenesses_does_not_turn_the_debt_from_0_into_420000():
    # The smallest failing world of P1 found on 1d323dd (debt 0 -> 420 000; the case «420 000 dated 24.10, forgivenesses 25.10 and 29.10, 0 -> 170 000» of the review was not reproduced by the same
    # numbers - the account of it was not written down; this one has the same shape).
    # Input: the personal period (calendar, payment-date anchor), price 420 000, a stay from 01.10, no pause, lessons Tue/Thu/Fri, today 10.11, start of the books 01.09.
    #   w1: forgiveness 420 000 dated 25.10, entered 25.10;  w2: forgiveness 420 000 dated 29.10, ENTERED 01.11;  p (later): payment 420 000 dated 24.10, entered 10.11.
    # Periods: October 01.10 (420 000) and November 01.11 (420 000) have begun; December has not.
    # Before p: w1 lies on October, w2 on November -> both used, debt 0, used 840 000.
    # With p, by the order of the dates the money is first (24.10): it fills October.  w1 (entered 25.10) moves to November, which had NOT begun on 25.10: it burns.  w2 (entered 01.11) moves to December
    # (not begun): burns.  November stays unpaid -> debt 420 000, used 0.   (This is the old result.)
    # After the Б7 step the forgiveness that was left over closes the unpaid November of the periods that had begun by its entry day: w2 (entered 01.11 >= 01.11) takes 420 000; w1 (25.10 < 01.11) cannot:
    # October money 420 000, November forgiveness 420 000, debt 0, advance 0, used 420 000 of 840 000.
    base = make(today=D(2026, 11, 10), stays=[bl.Stay(D(2026, 10, 1))], price_at=flat(420_000), start=D(2026, 9, 1), lesson_days=TTF, regimes=[bl.Regime()])
    w1 = bl.Credit("w1", bl.WRITEOFF, D(2026, 10, 25), at(D(2026, 10, 25)), 420_000)
    w2 = bl.Credit("w2", bl.WRITEOFF, D(2026, 10, 29), at(D(2026, 11, 1), 1), 420_000)
    p = bl.Credit("p", bl.PAYMENT, D(2026, 10, 24), at(D(2026, 11, 10), 9), 420_000)
    before = bl.run_ledger(replace(base, credits=(w1, w2)))
    after = bl.run_ledger(replace(base, credits=(w1, w2, p)))
    assert (before.debt, before.advance, before.written_off) == (0, 0, 840_000)
    assert (after.debt, after.advance, after.written_off, after.written_off_granted) == (0, 0, 420_000, 840_000)
    assert after.reconciles and not after.flags


# ---------------------------------------------------------------------------------------------------------------------------- PAY-TAILS Б7.8 step 5 (R4): the same properties WITH a lesson cancelled by the centre
# The cancel is an event of the chain with its own moment (before / after the lesson, before / after the credits, the setting of the deductions sometimes switched on later); P3k = a cancel entered NOW.
# The full measure: B7_CANCELS=with B7_MODE=common python tests/pay_tails_b7_tools.py count 1 20000 5   (5 seeds)

_WITH_CANCELS = {}


def _with_cancels(mode):
    if mode not in _WITH_CANCELS:
        _WITH_CANCELS[mode] = tools.run_counts(1, WORLDS, tools.Cfg(mode, "with"))[0]
    return _WITH_CANCELS[mode]


ALL_KEYS = ("P1 debt", "P1 used", "P1 advance", "P1 advance<=money", "P1 identity", "P2 debt", "P2 advance<=money", "P2 advance today", "P2 advance back", "P3 used", "P3 debt", "P3 advance",
            "P3 advance<=money", "P3k used", "P3k debt", "P3k advance", "P3k advance<=money", "P3e used", "P3e debt", "P3e advance", "P3e advance<=money", "P4 early", "P4 today", "P5 probe", "flags")


def test_b7_r4_the_common_month_with_cancelled_lessons_has_no_counterexample_of_p1_p5_and_p3k():
    counts = _with_cancels("common")
    assert {k: counts[k] for k in ALL_KEYS} == {k: 0 for k in ALL_KEYS}, counts
    assert counts["P4 nomatch"] == 0


def test_b7_r4_the_personal_period_with_cancelled_lessons_has_no_counterexample_of_any_property_b11_9():
    # The deductions DO exist in a personal period (PAY-F: the setting is for the calendar modes; only the packages have none).  Б11.9 п. 2: the growth of the debt and of the used forgiveness after a pause (a pause shifts the
    # borders of the personal periods, a deduction goes to another place) is bounded by the sum over PLACES of max(0, N after - N before); the exception of step 7.3 for «P3 » / «P3e » is gone - every key is zero.
    counts = _with_cancels("personal")
    assert {k: counts[k] for k in ALL_KEYS} == {k: 0 for k in ALL_KEYS}, counts


# ---------------------------------------------------------------------------------------------------------------------------- PAY-TAILS Б11.8 step 8: the leaving, its cancel, the correction of a payment
NEW_KEYS = ("P1c debt", "P1c advance", "P1c used", "P3u advance>=", "P3u advance<=freed", "P3u advance<=money", "P3u debt", "P3u used", "P3u transfer", "P6 same", "P6 amount", "P6b restore", "P7 undo")


def test_b11_8_the_leaving_the_cancel_of_it_the_correction_of_a_payment_and_the_payment_dated_today_keep_the_properties_at_zero():
    # P1c: a payment dated today lowers the debt by min(x, debt), the advance grows by max(0, x - debt), the used forgiveness stays - after a leaving too.  P3u: a leaving entered now never lowers the advance, grows it by no more
    # than the money that lay on the cut / vanished places, never raises the debt or the used forgiveness (a transfer: advance after + carried >= advance before).  P6 / P6b: a correction without a change, a correction of the sum
    # and a cancel + a restoration are the payment of the first record.  P7: a leaving cancelled the same day is the account before it.  The common month, the personal period and the packages, with and without a cancelled lesson.
    for mode in ("common", "personal", "packages"):
        counts = _counts(mode)
        assert {k: counts[k] for k in NEW_KEYS} == {k: 0 for k in NEW_KEYS}, (mode, counts)
    for mode in ("common", "personal"):
        counts = _with_cancels(mode)
        assert {k: counts[k] for k in NEW_KEYS} == {k: 0 for k in NEW_KEYS}, (mode, "with cancels", counts)
