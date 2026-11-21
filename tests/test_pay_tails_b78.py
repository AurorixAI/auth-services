"""PAY-TAILS Б7.8 step 4.2: a PACKAGE has a world known at the moment of entering too (pure ledger, no database; the numbers are worked out by hand in the comment).

World: packages of 8 lessons, 400 000 a package (50 000 a lesson), Tue / Thu / Fri, the student joined 01.10, today 02.12; every held lesson is in the input (the lessons of a pause are in it, the
ledger takes the days of the pauses it KNOWS out).  Lessons: October 1 2 6 8 9 13 15 16 | 20 22 23 27 29 30 + November 3 5 | 6 10 12 13 17 19 20 24 ... : package 1 = 01.10 .. 16.10 (8 lessons),
package 2 = 20.10 .. 05.11, package 3 = 10.11 .. (first day 06.11 before the pause: the lesson of 20.10 leaves package 2, so the whole line shifts by one lesson)."""
from dataclasses import replace
from datetime import timedelta

from src.auth import billing_ledger as bl

from tests.pay2_ledger_helpers import D, at, flat, make, pause, weekly

TUE_THU_FRI = weekly((1, 3, 4))


def packages_world(pauses=()):
    today = D(2026, 12, 2)
    held = [d for d in (D(2026, 10, 1) + timedelta(days=k) for k in range((today - D(2026, 10, 1)).days + 1)) if d.weekday() in (1, 3, 4)]
    inp = make(today=today, stays=[bl.Stay(D(2026, 10, 1))], price_at=flat(400_000), regimes=[bl.Regime(mode=bl.LESSONS, package_size=8)], start=D(2026, 9, 1), lesson_days=TUE_THU_FRI)
    forgiveness = bl.Credit("c0", bl.WRITEOFF, D(2026, 9, 23), at(D(2026, 10, 21), 190), 600_000)
    return replace(inp, counted_lessons=tuple(bl.CountedLesson(d, f"l{d}") for d in held), credits=(forgiveness,), pauses=tuple(pauses))


def test_b78_a_forgiveness_stays_on_the_package_that_was_going_when_it_was_entered_after_a_pause_takes_its_first_lesson_away():
    # 21.10 the debt is 1 350 000 - ...: the forgiveness 600 000 lies on package 1 (400 000, 8 lessons 01.10 - 16.10) and on package 2 (200 000 of 400 000): package 2 had BEGUN on 20.10 (its first lesson).
    # 02.12 a pause 20.10 - 21.10 is entered: the lesson of 20.10 is nobody's (50 000 less), package 2 «begins» on 22.10 > 21.10 - by the day of today's world the forgiveness would lose the 200 000.
    # In the world known on 21.10 package 2 was going (it had its lesson of 20.10): the forgiveness stays on it.  Charged 1 350 000 - 50 000 = 1 300 000, used 600 000, debt 700 000 (before: 750 000).
    before = bl.run_ledger(packages_world())
    assert (before.charged, before.written_off, before.debt) == (1_350_000, 600_000, 750_000)
    after = bl.run_ledger(packages_world([pause(D(2026, 10, 20), D(2026, 10, 21), D(2026, 12, 2), 1080)]))
    assert after.reconciles and not after.flags
    assert (after.charged, after.written_off, after.debt, after.advance) == (1_300_000, 600_000, 700_000, 0)
    forgiven = {s.package_no: s.forgiven for s in after.segments if s.kind == "package"}
    assert (forgiven[1], forgiven[2]) == (400_000, 200_000)
