"""PAY-TAILS Б7.7 step 3.0: the named worlds of the model «the world known at the moment of entry» on the pure ledger (numbers by hand in the comments; LESSONS 156, 158, 160, 161).

The common month 600 000, lessons Tue / Thu / Fri.  October 2026 has 14 lessons (Thu 1 8 15 22 29, Fri 2 9 16 23 30, Tue 6 13 20 27: 600 000 x k / 14 -> 257 000 for 6, 514 000 for 12, 300 000 for 7);
November has 12 (Tue 3 10 17 24, Thu 5 12 19 26, Fri 6 13 20 27: 50 000 a lesson).  Every fact is entered at a MOMENT (``tools.at(day, minute)``): the facts of one day are told apart by the minute."""
from dataclasses import replace
from datetime import date

from src.auth import billing_ledger as bl
from tests import pay_tails_b7_tools as tools
from tests.pay2_ledger_helpers import D, flat, make

PAY, WO = bl.PAYMENT, bl.WRITEOFF
credit, pause, edit = tools.mk_credit, tools.mk_pause, tools.edit_pause


def common(today, stay=D(2026, 10, 1), pauses=(), **kw):
    return replace(make(today=today, stays=[bl.Stay(stay)], price_at=flat(600_000), regimes=[tools.FIXED1], start=D(2026, 9, 1), lesson_days=tools.TTF, **kw), pauses=tuple(pauses))


def personal(today, stay=D(2026, 10, 1)):
    return make(today=today, stays=[bl.Stay(stay)], price_at=flat(420_000), regimes=[bl.Regime()], start=D(2026, 9, 1), lesson_days=tools.TTF)


def figures(inp):
    L = bl.run_ledger(inp)
    assert L.reconciles and not L.flags, L.flags
    return dict(charged=L.charged, debt=L.debt, advance=L.advance, used=L.written_off)


def parts(inp):
    """``{(period start, credit id): amount}`` of the final account."""
    L = bl.run_ledger(inp)
    seg = {s.id: s for s in L.segments}
    return {(seg[a.segment_id].period_start, a.credit_id): a.amount for a in L.allocations if a.segment_id in seg}


def test_d1_a_forgiveness_does_not_lie_on_a_period_that_has_not_begun_today():
    # October is paid (payment 600 000, 01.10).  03.11 November (600 000, 12 lessons) is forgiven in full.  05.11 a pause 01.11 - 10.11 is entered: November has 8 lessons of 12 = 400 000, its first
    # charged day is 11.11.  TODAY (05.11) November has not begun: nothing is charged for it, the forgiveness is not used - debt 0, advance 0, used 0 (it must never turn into an «advance»).
    # On 11.11 November has begun: charged 600 000 + 400 000; the forgiveness (the replay: 600 000 - 400 000 = 200 000 cut off) covers 400 000: debt 0, used 400 000.
    base = common(D(2026, 11, 5), credits=[credit("p", PAY, D(2026, 10, 1), D(2026, 10, 1), 10, 600_000), credit("w", WO, D(2026, 11, 3), D(2026, 11, 3), 10, 600_000)],
                  pauses=[pause(D(2026, 11, 1), D(2026, 11, 10), D(2026, 11, 5), 30)])
    assert figures(base) == dict(charged=600_000, debt=0, advance=0, used=0)
    assert figures(replace(base, today=D(2026, 11, 11))) == dict(charged=1_000_000, debt=0, advance=0, used=400_000)


def test_b7_d_a_pause_on_the_whole_of_october_entered_after_the_forgiveness_takes_it_and_the_debt_is_the_november_one():
    # October and November are unpaid and have begun.  05.11 the whole debt 600 000 is forgiven: it lies on October (the oldest).  07.11 October is put wholly in a pause (the month leaves the account:
    # it costs 0): the forgiveness of October is cut by 600 000 and goes nowhere - November 600 000 is owed, used 0.   The same pause entered 04.11 (BEFORE the forgiveness): the world of 05.11 has no October,
    # the forgiveness lies on November: debt 0, used 600 000.
    base = common(D(2026, 11, 10), credits=[credit("w", WO, D(2026, 11, 5), D(2026, 11, 5), 10, 600_000)])
    late = replace(base, pauses=[pause(D(2026, 10, 1), D(2026, 10, 31), D(2026, 11, 7), 10)])
    early = replace(base, pauses=[pause(D(2026, 10, 1), D(2026, 10, 31), D(2026, 11, 4), 10)])
    assert figures(late) == dict(charged=600_000, debt=600_000, advance=0, used=0)
    assert figures(early) == dict(charged=600_000, debt=0, advance=0, used=600_000)


def test_class_a_a_pause_in_the_head_of_a_period_entered_later_does_not_make_the_period_not_begun_for_the_forgiveness():
    # 01.10 420 000 are forgiven (October, 600 000, had begun).  Later a pause 01.10 - 19.10 is entered: the first charged day of October is 20.10 (6 lessons of 14 = 257 000).  Today 08.12 (October, November,
    # December have begun: 257 000 + 600 000 + 600 000 = 1 457 000).  The forgiveness lay on October the day it was entered: it is cut to 257 000 (163 000 cut off), used 257 000, debt 1 457 000 - 257 000 = 1 200 000
    # (without the pause the debt was 1 800 000 - 420 000 = 1 380 000).  The debt must never be larger than 1 457 000 - 257 000.
    inp = common(D(2026, 12, 8), credits=[credit("w", WO, D(2026, 10, 1), D(2026, 10, 1), 10, 420_000)])
    assert figures(inp) == dict(charged=1_800_000, debt=1_380_000, advance=0, used=420_000)
    with_pause = replace(inp, pauses=[pause(D(2026, 10, 1), D(2026, 10, 19), D(2026, 11, 10), 10)])
    assert figures(with_pause) == dict(charged=1_457_000, debt=1_200_000, advance=0, used=257_000)


def test_class_b_a_pause_that_moves_the_start_of_the_next_personal_period_does_not_burn_what_lay_on_it():
    # The personal period 420 000, a stay from 01.10, today 09.11.  11.10 a payment 200 000; 03.11 a forgiveness 300 000: October 220 000 + November 80 000 (November began 01.11).  Debt 840 000 - 200 000 - 300 000
    # = 340 000.  Later (05.11) a pause 14.10 - 18.10 is entered: October runs to 05.11, the second period begins 06.11 (> 03.11).  The forgiveness lay on November the day it was entered, when November
    # had begun: used 300 000, debt 340 000 (the cost of the periods is the same).
    inp = personal(D(2026, 11, 9))
    inp = replace(inp, credits=(credit("p", PAY, D(2026, 10, 11), D(2026, 10, 11), 10, 200_000), credit("w", WO, D(2026, 11, 3), D(2026, 11, 3), 10, 300_000)))
    assert figures(inp) == dict(charged=840_000, debt=340_000, advance=0, used=300_000)
    assert figures(replace(inp, pauses=[pause(D(2026, 10, 14), D(2026, 10, 18), D(2026, 11, 5), 10)])) == dict(charged=840_000, debt=340_000, advance=0, used=300_000)


def test_class_c_the_pause_entered_the_same_day_after_the_forgiveness_cuts_it():
    # Today 05.11.  A payment 300 000 dated 05.10; a forgiveness 300 000 (dated 22.09) ENTERED 05.11 at the 10th minute; the same day, the 30th minute, a pause 29.10 - 05.11 (October 12 lessons of 14 = 514 000).
    # The pause was entered AFTER the forgiveness (the moment, not the day, decides): 600 000 - 514 000 = 86 000 are cut off the forgiveness, alive 214 000.  October: money 300 000 + forgiveness 214 000,
    # November has not begun (first charged day 06.11): charged 514 000, debt 0, advance 0, used 214 000.
    inp = common(D(2026, 11, 5), credits=[credit("p", PAY, D(2026, 10, 5), D(2026, 10, 5), 10, 300_000), credit("w", WO, D(2026, 9, 22), D(2026, 11, 5), 10, 300_000)],
                 pauses=[pause(D(2026, 10, 29), D(2026, 11, 5), D(2026, 11, 5), 30)])
    assert figures(inp) == dict(charged=514_000, debt=0, advance=0, used=214_000)
    assert parts(inp) == {(D(2026, 10, 1), "p"): 300_000, (D(2026, 10, 1), "w"): 214_000}


def _november(today):
    """A stay from 01.11 (one month, 12 lessons, 50 000 a lesson); a pause entered 02.11 on 24.11 - 30.11 (3 lessons lost: 450 000)."""
    return common(today, stay=D(2026, 11, 1), pauses=[pause(D(2026, 11, 24), D(2026, 11, 30), D(2026, 11, 2), 10)])


def test_chain_1_pause_then_half_the_rest_forgiven_then_the_parent_pays_the_rest_then_the_pause_is_extended():
    # November costs 450 000 (the pause of 02.11).  05.11 225 000 are forgiven (half), 10.11 the parent pays the other 225 000, 12.11 the pause is EXTENDED to 17.11 - 30.11 (6 lessons lost: 300 000).
    # The extension is entered after both: the forgiveness loses first - 450 000 - 300 000 = 150 000 are cut off it, alive 75 000.  November: money 225 000 + forgiveness 75 000, no advance, debt 0.
    inp = replace(_november(D(2026, 11, 15)), credits=(credit("w", WO, D(2026, 11, 5), D(2026, 11, 5), 10, 225_000), credit("p", PAY, D(2026, 11, 10), D(2026, 11, 10), 10, 225_000)))
    inp = replace(inp, pauses=(edit(inp.pauses[0], D(2026, 11, 17), D(2026, 11, 30), D(2026, 11, 12), 10),))
    assert figures(inp) == dict(charged=300_000, debt=0, advance=0, used=75_000)
    assert parts(inp) == {(D(2026, 11, 1), "p"): 225_000, (D(2026, 11, 1), "w"): 75_000}


def test_chain_2_pause_then_the_rest_forgiven_then_the_next_month_paid_then_the_pause_is_extended():
    # 05.11 the whole rest 450 000 is forgiven, 10.11 the parent pays DECEMBER (600 000), 12.11 the pause is extended (November 300 000): 150 000 are cut off the forgiveness (alive 300 000) and nowhere else
    # (the payment lies on December).  November 300 000 is covered by the forgiveness, December is paid ahead: debt 0, advance 600 000 (money), used 300 000.
    inp = replace(_november(D(2026, 11, 15)), credits=(credit("w", WO, D(2026, 11, 5), D(2026, 11, 5), 10, 450_000), credit("p", PAY, D(2026, 11, 10), D(2026, 11, 10), 10, 600_000)))
    inp = replace(inp, pauses=(edit(inp.pauses[0], D(2026, 11, 17), D(2026, 11, 30), D(2026, 11, 12), 10),))
    assert figures(inp) == dict(charged=300_000, debt=0, advance=600_000, used=300_000)


def test_p5_the_probe_of_the_window_gives_what_the_record_gives():
    # October is unpaid, November has begun; 05.11 300 000 are forgiven, DATED 01.10; 07.11 a pause is entered after which October costs 300 000 (7 lessons of 14: the pause 01.10 - 15.10).  10.11 a payment 600 000
    # dated 01.10 is tried in the window and then recorded.  The forgiveness (alive 300 000) fills October (money comes later in the order of entry); the payment goes to November: debt 0, used 300 000.
    base = common(D(2026, 11, 10), credits=[credit("w", WO, D(2026, 10, 1), D(2026, 11, 5), 10, 300_000)], pauses=[pause(D(2026, 10, 1), D(2026, 10, 15), D(2026, 11, 7), 10)])
    paid = credit("late", PAY, D(2026, 10, 1), D(2026, 11, 10), 900, 600_000)
    record = bl.run_ledger(replace(base, credits=(*base.credits, paid)))
    probe = bl.run_ledger(replace(base, credits=(*base.credits, tools.probe_credit(paid))))
    assert (record.debt, record.advance, record.written_off) == (0, 0, 300_000)
    assert (probe.debt, probe.advance, probe.written_off) == (0, 0, 300_000)
