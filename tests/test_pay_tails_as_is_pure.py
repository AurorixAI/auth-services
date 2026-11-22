"""PAY-TAILS Б11.9 п. 3 / Б11.8 п. 11, step 8.9: the pure-ledger tests «as it is» (LEDGER-FREEZE will change them; each names the number «as it should be»)."""
from dataclasses import replace
from datetime import date as D

from src.auth import billing_ledger as bl
from tests.pay2_ledger_helpers import at, credit, flat, make, pause, weekly


def test_world_5231_the_pause_that_makes_the_first_personal_period_dearer_leaves_a_debt_of_the_rise_as_it_is_now():
    # Personal period 420 000 for 30 days from 01.10, the stay 01.10 - 04.11 (the leaving 04.11), lessons Tue / Thu / Fri; the lesson of 13.10 is cancelled by the centre (deduction 30 000); forgiven 300 000 (20.10) and 150 000
    # (04.11).  Period 1 costs 420 000 - 30 000 = 390 000, period 2 (01 - 04.11, 4 days) 420 000 x 4 / 30 = 56 000: used 390 000 + 56 000 = 446 000 of 450 000, debt 0.
    # Then a pause 13 - 15.10 is entered (20.11): the cancelled lesson falls into it (no deduction any more) and the first period moves its end to 03.11 and costs 420 000 again; the second is one day, 14 000: its forgiveness
    # is cut by 56 000 - 14 000 = 42 000 and goes nowhere, the first period is dearer by 30 000 after its forgiveness: debt 30 000, used 390 000 + 14 000 = 404 000.
    # LEDGER-FREEZE (not a blocker of the launch): AS IT SHOULD BE - the surplus of the cheaper period (42 000) covers the rise of its neighbour in the same event: debt 0.
    inp = make(today=D(2026, 11, 20), stays=[bl.Stay(D(2026, 10, 1), D(2026, 11, 4), left_at=at(D(2026, 11, 5)))], price_at=flat(420_000, D(2026, 1, 1)), start=D(2026, 9, 1), lesson_days=weekly((1, 3, 4)),
               regimes=[bl.Regime()], credits=[bl.Credit("w1", bl.WRITEOFF, D(2026, 10, 20), at(D(2026, 10, 20), 1), 300_000), bl.Credit("w2", bl.WRITEOFF, D(2026, 11, 4), at(D(2026, 11, 4), 2), 150_000)])
    inp = replace(inp, centre_cancels=(bl.CentreCancel(D(2026, 10, 13), "c", at(D(2026, 10, 14))),), recalc_versions=(bl.RecalcVersion(D(2026, 9, 1), True, at(D(2026, 9, 1))),))
    before = bl.run_ledger(inp)
    assert (before.debt, before.written_off, before.recalculated) == (0, 446_000, 30_000)
    after = bl.run_ledger(replace(inp, pauses=(pause(D(2026, 10, 13), D(2026, 10, 15), D(2026, 11, 20), 30),)))
    assert (after.debt, after.written_off) == (30_000, 404_000)                      # as it is; should be debt 0


def test_packages_a_certificate_that_takes_a_held_lesson_off_does_not_move_the_forgiveness_as_it_is_now():
    # Package 150 000 / 3 lessons (50 000 each); three lessons held; 04.11 forgiven 50 000, 05.11 paid 100 000: charged 150 000, used 50 000, debt 0, advance 0.  Then a certificate is confirmed: one lesson is not counted
    # any more (two counted = 100 000).  The forgiveness keeps its place (it was entered first) and the money 100 000 - 50 000 = 50 000 of it is the advance: advance 50 000, used 50 000.
    # LEDGER-FREEZE: AS IT SHOULD BE - the money first (nothing needs the forgiveness): advance 0, used 0.
    reg = bl.Regime(mode=bl.LESSONS, package_size=3)
    held = [bl.CountedLesson(D(2026, 11, 3), "a"), bl.CountedLesson(D(2026, 11, 5), "b"), bl.CountedLesson(D(2026, 11, 6), "c")]
    kw = dict(today=D(2026, 11, 10), stays=[bl.Stay(D(2026, 11, 1))], price_at=flat(150_000), regimes=[reg], start=D(2026, 10, 1),
              credits=[credit("w", D(2026, 11, 4), 50_000, bl.WRITEOFF), credit("p", D(2026, 11, 5), 100_000)])
    three = bl.run_ledger(make(counted=held, **kw))
    assert (three.charged, three.debt, three.advance, three.written_off) == (150_000, 0, 0, 50_000)
    two = bl.run_ledger(make(counted=held[:2], **kw))
    assert (two.charged, two.debt, two.advance, two.written_off) == (100_000, 0, 50_000, 50_000)           # as it is; should be advance 0, used 0
