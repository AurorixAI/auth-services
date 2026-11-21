"""PAY-TAILS Б7.8 step 5 (R4): a lesson cancelled by the centre is an EVENT of the account with its own moment, like a pause (pure ledger, no database; the numbers are worked out by hand in the comments).

World: a common month 600 000 (billing day 1), Tue / Thu / Fri; November 2026 has 12 lessons: a lesson is 50 000; the student joined 01.11, today 20.11.  The lesson of Thursday 12.11 is cancelled by the centre.
The rule: the cheapening of a period by a deduction is a truncation like a pause - the room of the period on the line of the tying is 600 000 - 50 000 = 550 000; a forgiveness entered BEFORE the cancel loses the 50 000
(money first - А6.1), one entered AFTER it knew the cancel (the debt it was capped by was 550 000); the money the deduction pushes out of the period goes on.  Inside the period: money, the deduction, the forgiveness."""
from dataclasses import replace
from datetime import date

from src.auth import billing_ledger as bl
from src.auth import payment_status as ps
from tests.pay2_ledger_helpers import D, at, flat, make, weekly

FIXED1 = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
TTF = weekly((1, 3, 4))
ALWAYS = (bl.RecalcVersion(date.min, True),)


def pay(cid, on, entered, amount, minute=10):
    return bl.Credit(cid, bl.PAYMENT, on, at(entered, minute), amount)


def forgive(cid, on, entered, amount, minute=10):
    return bl.Credit(cid, bl.WRITEOFF, on, at(entered, minute), amount)


def nov(credits, *, cancel_entered, versions=ALWAYS, today=D(2026, 11, 20), cancel_day=D(2026, 11, 12)):
    inp = make(today=today, stays=[bl.Stay(D(2026, 11, 1))], price_at=flat(600_000), regimes=[FIXED1], start=D(2026, 10, 1), credits=credits, lesson_days=TTF)
    cancels = (bl.CentreCancel(cancel_day, "l", entered=cancel_entered),)
    return bl.run_ledger(replace(inp, centre_cancels=cancels, recalc_versions=versions))


def november(L):
    return next(s for s in L.segments if s.period_start == D(2026, 11, 1))


def test_c6_paid_then_forgiven_then_the_lesson_is_cancelled():
    # money 300 000 [0; 300 000], forgiveness 300 000 [300 000; 600 000]; the cancel (entered 15.11) makes the month 550 000: the forgiveness loses 50 000 (money first), alive 250 000.
    # The period: money 300 000 + deduction 50 000 + forgiveness 250 000 = 600 000.  Used 250 000 of 300 000, advance 0, debt 0.
    L = nov([pay("p", D(2026, 11, 2), D(2026, 11, 2), 300_000), forgive("w", D(2026, 11, 3), D(2026, 11, 3), 300_000)], cancel_entered=at(D(2026, 11, 15)))
    assert (L.charged, L.debt, L.advance, L.written_off, L.written_off_granted, L.recalculated) == (600_000, 0, 0, 250_000, 300_000, 50_000) and L.reconciles and not L.flags
    assert (november(L).money, november(L).recalculated, november(L).forgiven) == (300_000, 50_000, 250_000)


def test_c6_prime_forgiven_then_paid_then_the_lesson_is_cancelled():
    # the same in the other order of entering (forgiveness 02.11, payment 03.11): the line is [forgiveness 0..300 000][money 300 000..600 000]; the cancel cuts the forgiveness (the one that is not money): the same numbers
    L = nov([forgive("w", D(2026, 11, 2), D(2026, 11, 2), 300_000), pay("p", D(2026, 11, 3), D(2026, 11, 3), 300_000)], cancel_entered=at(D(2026, 11, 15)))
    assert (L.charged, L.debt, L.advance, L.written_off, L.written_off_granted, L.recalculated) == (600_000, 0, 0, 250_000, 300_000, 50_000) and L.reconciles and not L.flags
    assert (november(L).money, november(L).recalculated, november(L).forgiven) == (300_000, 50_000, 250_000)


def test_c7_paid_in_full_the_period_holds_550_000_and_50_000_go_on():
    # money 600 000, the cancel entered after: the room of November is 550 000: 550 000 in its month + the deduction 50 000, and 50 000 of the money go on (an advance: December has not begun).
    # (before R4 the whole 600 000 lay on November and the deduction 'travelled' to the next period: the same totals, another place)
    L = nov([pay("p", D(2026, 11, 2), D(2026, 11, 2), 600_000)], cancel_entered=at(D(2026, 11, 15)))
    assert (L.charged, L.debt, L.advance, L.written_off, L.recalculated) == (600_000, 0, 50_000, 0, 50_000) and L.reconciles and not L.flags
    assert (november(L).money, november(L).recalculated, november(L).forgiven) == (550_000, 50_000, 0)


def test_overpayment_after_a_lesson_cancelled_in_advance_the_forgiveness_stays_and_the_money_goes_on():
    # the lesson is cancelled 05.11 (in advance); 06.11 the debt is 600 000 - 50 000 = 550 000: forgiven 250 000; 07.11 paid 600 000.  The room of November is 550 000: [forgiveness 0..250 000][money 250 000..550 000] = money 300 000,
    # the other 300 000 of the payment go on (advance).  Period: money 300 000 + deduction 50 000 + forgiveness 250 000 = 600 000: the forgiveness is kept in full, used 250 000, advance 300 000.
    L = nov([forgive("w", D(2026, 11, 6), D(2026, 11, 6), 250_000), pay("p", D(2026, 11, 7), D(2026, 11, 7), 600_000)], cancel_entered=at(D(2026, 11, 5)))
    assert (L.charged, L.debt, L.advance, L.written_off, L.written_off_granted, L.recalculated) == (600_000, 0, 300_000, 250_000, 250_000, 50_000) and L.reconciles and not L.flags
    assert (november(L).money, november(L).recalculated, november(L).forgiven) == (300_000, 50_000, 250_000)


def test_the_cancel_is_known_only_from_the_day_the_setting_was_switched_on():
    # the lesson of 17.11 is cancelled in advance (05.11), but the setting «deduct» is made 14.11 (the version from 14.11, made that day): the deduction exists from 14.11, not from 05.11.
    # 10.11 forgiven 300 000 (the debt was 600 000), 11.11 paid 300 000: both are EARLIER than the deduction - the forgiveness loses 50 000: money 300 000 + deduction 50 000 + forgiveness 250 000, used 250 000, advance 0.
    versions = (bl.RecalcVersion(D(2026, 11, 14), True, entered=at(D(2026, 11, 14))),)
    L = nov([forgive("w", D(2026, 11, 10), D(2026, 11, 10), 300_000), pay("p", D(2026, 11, 11), D(2026, 11, 11), 300_000)], cancel_entered=at(D(2026, 11, 5)), versions=versions, cancel_day=D(2026, 11, 17))
    assert (L.charged, L.debt, L.advance, L.written_off, L.written_off_granted, L.recalculated) == (600_000, 0, 0, 250_000, 300_000, 50_000) and L.reconciles and not L.flags


def test_a_cancel_known_before_the_forgiveness_does_not_cut_it_and_the_money_that_comes_after_goes_on():
    # the setting was made 03.11 (from 03.11), the lesson of 17.11 was cancelled 05.11: from 05.11 the deduction is known.  10.11 forgiven 300 000 (the debt 550 000 allowed it), 11.11 paid 300 000.
    # The room 550 000: [forgiveness 0..300 000][money 300 000..550 000] = money 250 000 in the month, 50 000 go on.  Period: money 250 000 + deduction 50 000 + forgiveness 300 000: used 300 000, advance 50 000.
    versions = (bl.RecalcVersion(D(2026, 11, 3), True, entered=at(D(2026, 11, 3))),)
    L = nov([forgive("w", D(2026, 11, 10), D(2026, 11, 10), 300_000), pay("p", D(2026, 11, 11), D(2026, 11, 11), 300_000)], cancel_entered=at(D(2026, 11, 5)), versions=versions, cancel_day=D(2026, 11, 17))
    assert (L.charged, L.debt, L.advance, L.written_off, L.written_off_granted, L.recalculated) == (600_000, 0, 50_000, 300_000, 300_000, 50_000) and L.reconciles and not L.flags
    assert (november(L).money, november(L).recalculated, november(L).forgiven) == (250_000, 50_000, 300_000)


def october(credits, *, cancel_entered, today):
    """The worlds A and B (found by the generator of tests/pay_tails_b7_tools.py on bb25e68, shrunk): the student since 01.10, 600 000 a month, October has 14 lessons (43 000 a lesson), the lesson of Tue 27.10 is cancelled by the centre."""
    inp = make(today=today, stays=[bl.Stay(D(2026, 10, 1))], price_at=flat(600_000), regimes=[FIXED1], start=D(2026, 9, 1), credits=credits, lesson_days=TTF)
    return bl.run_ledger(replace(inp, centre_cancels=(bl.CentreCancel(D(2026, 10, 27), "l", entered=cancel_entered),), recalc_versions=ALWAYS))


def world_a(extra=()):
    return october([pay("c0", D(2026, 11, 21), D(2026, 11, 21), 150_000), forgive("c1", D(2026, 11, 23), D(2026, 11, 23), 600_000, 30), *extra], cancel_entered=at(D(2026, 11, 18), 25), today=D(2026, 12, 4))


def test_world_a_a_payment_entered_late_never_raises_the_used_forgiveness_or_the_debt():
    # the lesson was cancelled 18.11, BEFORE the money and the forgiveness (21.11, 23.11): the room of October is 557 000 for both.  Before the late payment: money 150 000 -> October; the forgiveness 600 000 -> October 407 000
    # (557 000 - 150 000) + November 193 000: used 600 000 (the deduction 43 000 + 150 000 + 407 000 = 600 000 of October); debt: November 407 000 + December 600 000 = 1 007 000.
    # The payment of 420 000 dated 22.10 (entered 04.12) is the first on the line: October 420 000 + 137 000 of the 150 000; 13 000 go to November; the forgiveness: October is full, November 600 000 - 13 000 = 587 000,
    # the 13 000 left cannot lie on December (it had not begun on 23.11) and burn.  Used 587 000 (not more than before), debt 1 800 000 - 570 000 - 587 000 - 43 000 = 600 000.
    before = world_a()
    assert (before.charged, before.debt, before.advance, before.written_off) == (1_800_000, 1_007_000, 0, 600_000) and before.reconciles and not before.flags
    after = world_a([pay("late", D(2026, 10, 22), D(2026, 12, 4), 420_000, 900)])
    assert (after.charged, after.debt, after.advance, after.written_off, after.recalculated) == (1_800_000, 600_000, 0, 587_000, 43_000) and after.reconciles and not after.flags


def world_b(extra=()):
    return october([pay("c0", D(2026, 10, 16), D(2026, 10, 16), 400_000), pay("c1", D(2026, 10, 28), D(2026, 10, 28), 100_000, 30), forgive("c2", D(2026, 11, 7), D(2026, 11, 7), 300_000, 50),
                    pay("c3", D(2026, 11, 11), D(2026, 11, 11), 400_000, 70), *extra], cancel_entered=at(D(2026, 11, 19), 90), today=D(2026, 11, 26))


def test_world_b_a_payment_entered_late_makes_an_advance_of_exactly_its_sum():
    # the cancel is entered 19.11, AFTER the four credits: at that moment October held 400 000 + 100 000 + 100 000 of the forgiveness 300 000 (the rest 200 000 on November), the room became 557 000: the forgiveness loses 43 000, alive 257 000.
    # Before: October money 500 000 + forgiveness 57 000 + deduction 43 000; November forgiveness 200 000 + money 400 000: debt 0, used 257 000, advance 0.
    # A payment of 100 000 dated 17.09 (entered 26.11) is first on the line: October 100 000 + 400 000 + 57 000 of the 100 000; 43 000 go to November; the forgiveness (257 000) November [43 000; 300 000]; the payment of 400 000: November 300 000,
    # 100 000 go on - December has not begun: advance 100 000 (it was 143 000 and the used forgiveness 300 000: the forgiveness that the cancel had cut came back).  Debt 0, used 257 000.
    before = world_b()
    assert (before.charged, before.debt, before.advance, before.written_off, before.written_off_granted) == (1_200_000, 0, 0, 257_000, 300_000) and before.reconciles and not before.flags
    after = world_b([pay("late", D(2026, 9, 17), D(2026, 11, 26), 100_000, 900)])
    assert (after.charged, after.debt, after.advance, after.written_off) == (1_200_000, 0, 100_000, 257_000) and after.reconciles and not after.flags


# ---- Б7.10 п. 1: the worlds of the replay do not depend on the sum of the credits (the length of the tape) -------------------------------------------------------------------------------------------------------------
def personal_oct_pause_world(extra_credits=(), today=D(2026, 11, 14)):
    """A personal period 420 000 from 01.10 (Tue / Thu / Fri), the lesson of 13.10 cancelled by the centre (entered 30.10: deduction 30 000), a forgiveness of 400 000 entered 09.11, a pause of the whole October entered 12.11."""
    credits = [forgive("w", D(2026, 11, 9), D(2026, 11, 9), 400_000), *extra_credits]
    inp = make(today=today, stays=[bl.Stay(D(2026, 10, 1))], price_at=flat(420_000), regimes=[bl.Regime()], start=D(2026, 9, 1), credits=credits, lesson_days=TTF)
    pause = bl.Pause(D(2026, 10, 1), D(2026, 10, 31), forms=((at(D(2026, 11, 12), 700), D(2026, 10, 1), D(2026, 10, 31)),))
    cancels = (bl.CentreCancel(D(2026, 10, 13), "l", entered=at(D(2026, 10, 30), 90)),)
    return replace(inp, pauses=(pause,), centre_cancels=cancels, recalc_versions=(bl.RecalcVersion(date.min, True),))


def test_b710_the_forgiveness_does_not_burn_because_the_next_payment_is_not_yet_entered():
    # By hand: the pause of the whole October (entered 12.11, AFTER the forgiveness) takes the cancel deduction away (the lesson is on a pause) and shifts the periods: the first = November (room 420 000), the second
    # = December (room 420 000, not begun).  The forgiveness 400 000 lay (09.11) on the periods of the world before the pause: October 390 000 (420 000 - 30 000) + 10 000 on the next one.  In the world after the pause
    # the room of the first period is 420 000 >= 400 000, of the second 420 000 >= 10 000: nothing is cut - used 400 000, debt 420 000 - 400 000 = 20 000, advance 0.
    L = bl.run_ledger(personal_oct_pause_world())
    assert (L.written_off, L.debt, L.advance) == (400_000, 20_000, 0) and L.reconciles
    # a payment of 100 000 entered today: 20 000 pays the debt of the first period, 80 000 go on (the advance); the forgiveness is the same 400 000
    P = bl.run_ledger(personal_oct_pause_world([pay("late", D(2026, 11, 14), D(2026, 11, 14), 100_000, minute=900)]))
    assert (P.written_off, P.debt, P.advance) == (400_000, 0, 80_000) and P.reconciles
    # on 01.12 without the payment: the same 400 000 used and the 20 000 of the first period still owed
    Q = bl.run_ledger(personal_oct_pause_world(today=D(2026, 12, 1)))
    assert Q.written_off == 400_000 and Q.reconciles
    first = next(s for s in Q.segments if s.kind != "opening")
    assert first.amount - first.money - first.forgiven - first.recalculated == 20_000
