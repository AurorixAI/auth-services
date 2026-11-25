"""PAY-TAILS Б11.46 (step 8.14.13) п. 1-4: the worlds of the lines of a pause that the check of Opus found (probes P1, P2, P4 and the world of a package that is not finished), on the clean ledger (``bl.run_ledger``).

World: 600 000 a month, Tue / Thu / Fri, the student in the group from 01.10.2026.  November 2026 has 12 lessons (3 5 6 10 12 13 17 19 20 24 26 27), December 14 (1 3 4 8 10 11 15 17 18 22 24 25 29 31).
The hand counts are written in the test of each world BEFORE the run; if a run does not agree, the count goes to the report, the number is not bent."""
import logging
from dataclasses import replace
from datetime import date as D

from src.auth import billing_ledger as bl
from src.auth import payments_views as pv
from tests.pay2_ledger_helpers import flat
from tests.test_pay_tails_b11411_pure import FIX, PERSONAL, TTF, base, pause, run, shown


def errors(caplog):
    return [r for r in caplog.records if "pause_charge_rows" in r.getMessage()]


def test_p1_personal_period_until_october_then_the_common_month_a_pause_across_the_change_no_lines_no_error(caplog):
    # By hand: the personal period 01.10 - 31.10 (600 000, begun), from 01.11 the common month (the cell of November 600 000, begun on 02.11).  The pause 27.10 - 03.11 on 02.11:
    # October (personal, a payment-date chain) loses 27 - 31.10 = 5 of 31 days: 600 000 x 26 / 31 = 503 226 -> 503 000 (-97 000).  The pause moves the first paid day of the cell of November to 04.11 (01.11 - 03.11
    # are paused, 03.11 is a lesson): the cell has not begun on 02.11 -> -600 000 carried out of the account.  The change of the charge -697 000 (= 1 200 000 -> 503 000).  The lines of the cell alone say -600 000: the
    # -97 000 of the personal period is not a line of the common month: the world is lawful (as M5): NO lines, NO error in the log (before the fix: an error), ``charge_later`` null (the days 01.11 - 03.11 lie inside the begun cell).
    T = D(2026, 11, 2)
    with caplog.at_level(logging.ERROR, logger="src.auth.payments_views"):
        (rows, later), delta = run(base(T, (PERSONAL, replace(FIX, effective_from=D(2026, 11, 1)))), D(2026, 10, 27), D(2026, 11, 3))
    assert rows == [] and later is None and delta == -697_000
    assert errors(caplog) == []


def test_p2_the_price_changes_inside_the_carried_period_no_lines_no_error_and_the_december_charge_by_the_new_price(caplog):
    # By hand: working date 02.12, 600 000 a month, from 03.12 an individual price 900 000.  Before: October + November + December (begun on 02.12, the first paid day 01.12, the price 600 000) = 1 800 000.
    # The pause 24.11 - 03.12: November loses 24 26 27 -> 9 of 12 = 450 000 (-150 000); December: 01.12 and 03.12 are lessons of the pause, the first paid day is 04.12 > 02.12: December has not begun -> -600 000.
    # The change -750 000.  The line of December would say «2 lessons -> +171 000» (the price after is 771 000 = 900 000 x 12 / 14, the price before 600 000): two prices, not lessons: NO lines, NO error, ``charge_later`` null.
    # The core's number on 04.12 (the rule of the price day goes to LEDGER-FREEZE): December begun, 12 lessons (4 8 10 11 15 17 18 22 24 25 29 31) x 900 000 / 14 = 771 428 -> 771 000.
    T = D(2026, 12, 2)
    inp = replace(base(T, (FIX,)), price_at=flat(600_000, changes={D(2026, 12, 3): 900_000}))
    with caplog.at_level(logging.ERROR, logger="src.auth.payments_views"):
        (rows, later), delta = run(inp, D(2026, 11, 24), D(2026, 12, 3))
    assert rows == [] and later is None and delta == -750_000
    assert errors(caplog) == []
    on_04 = bl.run_ledger(replace(inp, today=D(2026, 12, 4), pauses=(pause(D(2026, 11, 24), D(2026, 12, 3), T),)))
    dec = [s for s in on_04.segments if s.slot == ("c", D(2026, 12, 1))]
    assert [(s.amount, s.begun) for s in dec] == [(771_000, True)]


def test_p2_control_the_same_carry_at_one_price_keeps_its_lines():
    # By hand (the same world with no change of the price): December carried at 600 000 on both sides: 14 lessons of which 01.12 and 03.12 leave -> 12 of 14 = 514 285 -> 514 000 after, 600 000 before: amount
    # -86 000 (2 lessons), deferred -514 000; November -150 000: the sum -150 000 - 86 000 - 514 000 = -750 000 = the change.  (The line stays: one price on both sides.)
    T = D(2026, 12, 2)
    (rows, later), delta = run(base(T, (FIX,)), D(2026, 11, 24), D(2026, 12, 3))
    assert shown(rows) == [("24.11–30.11", 3, -150_000), ("01.12–03.12", 2, -86_000)] and delta == -750_000
    assert [r.deferred for r in rows] == [None, -514_000]


def test_p4_the_clause_ends_where_the_common_month_ends():
    # By hand: the common month until 31.12, from 01.01.2027 a personal period; the working date 24.11; the pause 01.12 - 10.01.  November (begun) has no changed day, December and January have not begun: no
    # lines, the change 0.  The days after the begun cell (ends 30.11) are 01.12 - 10.01; the common month rules until 31.12, from 01.01 a personal period: the clause «01.12–31.12» (it was «01.12.2026–10.01.2027»).
    (rows, later), delta = run(base(D(2026, 11, 24), (FIX, bl.Regime(effective_from=D(2027, 1, 1)))), D(2026, 12, 1), D(2027, 1, 10))
    assert rows == [] and delta == 0
    assert (later.label, later.first_day, later.last_day) == ("01.12–31.12", D(2026, 12, 1), D(2026, 12, 31))


def test_p6_a_package_that_is_not_finished_keeps_its_days_no_clause():
    # By hand: the common month until 31.10 (the cell of October), from 01.11 packages (12 lessons a package), 9 lessons of November held by 23.11 (3 5 6 10 12 13 17 19 20): the package is open (9 of 12).  A
    # version «the common month» saved on 20.11 with ``defer`` acts only after the package in progress is finished (the 12th lesson, not yet held): it does not act on 01.12 - 05.12 - the days of the pause lie in
    # the package that goes on.  The last begun unit is the package (ends 20.11): the days of the pause are after it, and by ``regime_at`` alone (the common month from 20.11) the clause «01.12–05.12» was given: a lie.
    # By the windows of the core (the package window has no end) ``charge_later`` is null; no lines (no cell has a changed day).
    held = [bl.CountedLesson(d, f"l{i}") for i, d in enumerate(sorted(d for d in TTF(D(2026, 11, 1), D(2026, 11, 23))))]
    regimes = (FIX, bl.Regime(effective_from=D(2026, 11, 1), mode=bl.LESSONS), replace(FIX, effective_from=D(2026, 11, 20), defer=True))
    (rows, later), delta = run(base(D(2026, 11, 24), regimes, counted=held), D(2026, 12, 1), D(2026, 12, 5))
    assert rows == [] and later is None and delta == 0


def test_promise_holds_both_ways_and_the_generator_counts_what_it_checked():
    # Б11.46 п.6.  By hand (working date 02.12, 600 000 a month; the pause 24.11 - 03.12 carries December out of the account): the line of December has deferred -514 000 (12 of 14 lessons), begins 04.12; the account AFTER on
    # 04.12 charges December 514 000 = -deferred: the promise is true.  The void of that pause (the other way, the same two accounts swapped): deferred +514 000; the account BEFORE (with the pause) on 04.12 - the first paid
    # day it had - charges 514 000 = deferred.  Counters: 2 events, 2 promises, 1 negative, 1 positive; ``lines_bad`` False for both.
    from tests import pay_tails_b7_tools as tools

    T = D(2026, 12, 2)
    free = base(T, (FIX,))
    paused = replace(free, pauses=(pause(D(2026, 11, 24), D(2026, 12, 3), T),))
    start = dict(tools.LINES)
    assert tools.lines_bad(free, paused) is False and tools.lines_bad(paused, free) is False
    got = {k: tools.LINES[k] - start[k] for k in ("promise events", "promise promises", "promise negative", "promise positive")}
    assert got == {"promise events": 2, "promise promises": 2, "promise negative": 1, "promise positive": 1}, got


def test_promise_has_teeth_a_promise_that_is_off_by_1000_is_red_and_the_sum_still_holds(monkeypatch):
    # By hand: the same worlds, the function made WRONG on purpose: deferred +1 000 and amount -1 000 (the sum of the lines is the change of the charge still, the shape is right): only the Promise can see it:
    # the account after on 04.12 charges 514 000, the line promises 513 000 (or 515 000 the other way).  ``lines_bad`` True in both, the insurance counter 0 (the sum is right).
    from tests import pay_tails_b7_tools as tools

    T = D(2026, 12, 2)
    free = base(T, (FIX,))
    paused = replace(free, pauses=(pause(D(2026, 11, 24), D(2026, 12, 3), T),))
    real = pv.pause_charge_rows

    def off(*a, **k):
        rows, later = real(*a, **k)
        return [r.model_copy(update={"deferred": r.deferred + 1000, "amount": r.amount - 1000}) if r.deferred else r for r in rows], later

    monkeypatch.setattr(pv, "pause_charge_rows", off)
    insurance = tools.LINES["insurance"]
    assert tools.lines_bad(free, paused) is True and tools.lines_bad(paused, free) is True
    assert tools.LINES["insurance"] == insurance
