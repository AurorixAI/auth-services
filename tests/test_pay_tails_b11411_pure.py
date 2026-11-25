"""PAY-TAILS Б11.44 (step 8.14.11) п. 1 and п. 7: ``charge_later`` and the insurance of the lines of a pause, on the clean ledger (the two accounts of ``bl.run_ledger``, no database).

World: 600 000 a month, Tue / Thu / Fri, the student in the group from 01.10.2026, the working date 24.11 unless a world says another.  November 2026 has 12 lessons (3 5 6 10 12 13 17 19 20 24 26 27),
December 14 (1 3 4 8 10 11 15 17 18 22 24 25 29 31); a lesson in a month of the common month costs 600 000 / lessons of the month.

The hand counts BEFORE the run (written in the test of each world).  The rule (п. 1): ``charge_later`` - only when the LATEST begun unit (a cell of the common month, a personal period, a package) is a cell of
the common month, or nothing has begun and the first changed day lies in a cell; a personal period or a package that has begun moves its own end: the old one line, no ``charge_later``."""
import logging
from types import SimpleNamespace
from dataclasses import replace
from datetime import date as D, timedelta

from src.auth import billing_ledger as bl
from src.auth import payment_status as ps
from src.auth import payments_views as pv
from tests.pay2_ledger_helpers import at, flat, make, pause, weekly

TTF = weekly((1, 3, 4))
FIX = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
PERSONAL = bl.Regime()


def span(a, b):
    return {a + timedelta(days=i) for i in range((b - a).days + 1)}


def base(today, regimes, *, start=D(2026, 10, 1), pattern=None, counted=()):
    inp = make(today=today, stays=[bl.Stay(start, None, at(start))], price_at=flat(600000), regimes=list(regimes), start=D(2026, 9, 1), lesson_days=TTF, counted=counted)
    return inp if pattern is None else replace(inp, pattern_days=pattern)


def run(inp, first, last, today=None):
    today = today or inp.today
    after_inp = replace(inp, pauses=(pause(first, last, today),))
    before, after = bl.run_ledger(inp), bl.run_ledger(after_inp)
    return pv.pause_charge_rows(before, after, today, span(first, last), money=True, membership_id="w", regimes=inp.regimes), after.charged - before.charged


def shown(rows):
    return [(r.label, r.lessons, r.amount) for r in rows]


def test_w2b_common_month_until_october_then_a_personal_period_a_pause_moves_its_end_no_rows_no_later():
    # By hand: October is a cell (31 days, begun, unchanged).  From 01.11 a personal period 01.11 - 30.11 (begun on 24.11) - NOT a cell: no line.  The pause 24.11 - 30.11 lies in a begun personal period,
    # the latest begun unit is it, not a cell: ``charge_later`` is null (before the fix: «24.11–30.11», a lie: the days do enter the cost, the personal period only ends later).
    (rows, later), _ = run(base(D(2026, 11, 24), (FIX, bl.Regime(effective_from=D(2026, 11, 1)))), D(2026, 11, 24), D(2026, 11, 30))
    assert rows == [] and later is None


def test_w2c_common_month_until_october_then_packages_no_rows_no_later():
    # By hand: from 01.11 packages (a package that has held lessons has begun, its slot is not a cell).  The latest begun unit is a package: no lines, ``charge_later`` null.
    held = [bl.CountedLesson(d, f"l{i}") for i, d in enumerate(sorted(d for d in TTF(D(2026, 11, 1), D(2026, 11, 23))))]        # 9 lessons held by 23.11
    (rows, later), _ = run(base(D(2026, 11, 24), (FIX, bl.Regime(effective_from=D(2026, 11, 1), mode=bl.LESSONS)), counted=held), D(2026, 11, 24), D(2026, 11, 30))
    assert rows == [] and later is None


def test_w2c_opus_probe_packages_from_01_11_no_lesson_of_the_package_yet_no_later():
    # Б11.45 С п.5, the probe of Opus W2c as it was (paytails-r4/b1410/probe2.py, no held lessons).  By hand: the common month until 31.10, from 01.11 packages; no lesson of the package has been
    # held, so no unit of the package is in the account: the latest begun unit is the cell of OCTOBER (ends 31.10) and by the segments the clause «24.11–30.11» was given.  By the regimes: on the first
    # day of the clause (24.11) the version in force is the package one -> ``charge_later`` null; no lines (October has no changed day), the change of the charge 0.
    (rows, later), delta = run(base(D(2026, 11, 24), (FIX, bl.Regime(effective_from=D(2026, 11, 1), mode=bl.LESSONS))), D(2026, 11, 24), D(2026, 11, 30))
    assert rows == [] and later is None and delta == 0


def test_w_common_now_a_personal_period_from_01_12_the_line_of_november_and_no_later():
    # Б11.45 С п.5.  By hand: the common month now, from 01.12 (a FUTURE change of the mode) a personal period.  The pause 24.11 - 03.12 on 24.11: November (begun): 24 26 27 leave -> 9 of 12 ->
    # 450 000, «24.11–30.11», 3 lessons, -150 000 (the change of the charge, the sum of the line).  The days after the begun cell, 01.12 - 03.12, lie where the personal period rules (the first day of
    # the clause, 01.12, is in the personal version): ``charge_later`` null.
    (rows, later), delta = run(base(D(2026, 11, 24), (FIX, bl.Regime(effective_from=D(2026, 12, 1)))), D(2026, 11, 24), D(2026, 12, 3))
    assert shown(rows) == [("24.11–30.11", 3, -150_000)] and delta == -150_000 and later is None


def test_w2_the_mode_changes_on_15_11_a_pause_24_11_to_03_12_no_later():
    # By hand: the cell 01.11 - 14.11 (cut by the change, begun, unchanged by the pause); from 15.11 a personal period 15.11 - 14.12 (begun).  The pause 24.11 - 03.12 lies in the personal period
    # (the latest begun unit): no lines, ``charge_later`` null (before the fix: «01.12–03.12»).
    (rows, later), _ = run(base(D(2026, 11, 24), (FIX, bl.Regime(effective_from=D(2026, 11, 15)))), D(2026, 11, 24), D(2026, 12, 3))
    assert rows == [] and later is None


def test_w_back_personal_until_october_then_the_common_month_the_line_of_november_and_later_from_01_12():
    # By hand: the personal period until 31.10, from 01.11 the common month.  Cell November (begun): the pause 24.11 - 03.12 takes out of it 24, 26, 27 = 3 lessons: 9 of 12 x 600 000 = 450 000,
    # the change -150 000, the line «24.11–30.11 · 3 · -150 000».  December has not begun (01.12 > 24.11): no line.  The latest begun unit is the cell of November (ends 30.11): the days after it,
    # 01.12 - 03.12, are ``charge_later``.  The change of the charge -150 000 = the line.
    (rows, later), delta = run(base(D(2026, 11, 24), (PERSONAL, bl.Regime(effective_from=D(2026, 11, 1), anchor=ps.FIXED_DAY, billing_day=1))), D(2026, 11, 24), D(2026, 12, 3))
    assert shown(rows) == [("24.11–30.11", 3, -150_000)] and delta == -150_000
    assert (later.label, later.first_day, later.last_day) == ("01.12–03.12", D(2026, 12, 1), D(2026, 12, 3))


def test_the_common_month_alone_is_as_before_line_and_later():
    # By hand: the common month only: the same line as in the world above (-150 000, 3 lessons) and ``charge_later`` 01.12 - 03.12 (nothing of the personal kind in the account).
    (rows, later), delta = run(base(D(2026, 11, 24), (FIX,)), D(2026, 11, 24), D(2026, 12, 3))
    assert shown(rows) == [("24.11–30.11", 3, -150_000)] and delta == -150_000 and later.label == "01.12–03.12"


def _ledger(*segments):
    return SimpleNamespace(segments=segments)


def _seg(start, end, slot, begun=False, kind="period"):
    return SimpleNamespace(kind=kind, period_start=start, period_end=end, slot=slot, begun=begun)


def test_later_days_the_regime_of_the_first_later_day_decides():
    # By hand (Б11.45 С п.5: the mode of the first day of the clause comes from the regimes of the input, ``regime_at`` - not from the segments).  Nothing has begun: every changed day is later; the
    # common month (a fixed billing day) on 02.12 -> all the days; a personal period (payment date) or packages from 01.12 -> none.  Begun cell of November (ends 30.11): the days after it (01.12 - 03.12)
    # are later; the regime on 01.12 decides: common -> the days, personal / packages from 01.12 -> none; a change of the mode on 02.12 (not on the first later day 01.12) does not matter.
    days = [D(2026, 12, 2), D(2026, 12, 3)]
    nothing = _ledger()
    for regimes, expected in (((FIX,), days), ((FIX, bl.Regime(effective_from=D(2026, 12, 1))), []), ((FIX, bl.Regime(effective_from=D(2026, 12, 1), mode=bl.LESSONS)), []),
                              ((PERSONAL,), []), ((PERSONAL, bl.Regime(effective_from=D(2026, 12, 1), anchor=ps.FIXED_DAY, billing_day=1)), days)):
        assert pv._later_days(nothing, nothing, days, regimes) == expected
    nov = _seg(D(2026, 11, 1), D(2026, 11, 30), ("c", D(2026, 11, 1)), True)
    nov_days = [D(2026, 12, 1), *days]
    assert pv._later_days(_ledger(nov), _ledger(nov), nov_days, (FIX,)) == nov_days
    assert pv._later_days(_ledger(nov), _ledger(nov), nov_days, (FIX, bl.Regime(effective_from=D(2026, 12, 1)))) == []
    assert pv._later_days(_ledger(nov), _ledger(nov), nov_days, (FIX, bl.Regime(effective_from=D(2026, 12, 2)))) == nov_days
    assert pv._later_days(_ledger(), _ledger(nov), nov_days, (FIX,)) == nov_days       # nothing begun after: the account before decides the end of the begun


def _no_pattern_in_november(a, b):
    return [d for d in TTF(a, b) if d >= D(2026, 12, 1)]


def test_m5_a_period_counted_by_days_is_lawful_no_lines_and_no_error(caplog):
    # By hand: the working date 02.12; the pause 24.11 - 01.12.  November has NO schedule by the pattern (a group with no slots in that month): counted by days: 30 days 600 000, the pause takes 24 - 30.11
    # = 7 days: 23 of 30 = 460 000, the change -140 000 (no lessons to name).  December (14 lessons, begun on 02.12): the pause takes 01.12 = 1 lesson: 13 of 14 x 600 000 = 557 142.9 -> 557 000, the change
    # -43 000.  The change of the charge -183 000; the line of December alone -43 000 is not the sum, but the world is lawful: no lines, ``charge_later`` null (the latest begun unit is the cell of
    # December, the changed days lie inside it), NOTHING in the log.  Control: with the schedule in November, two lines (3 lessons -150 000 and 1 lesson -43 000 = -193 000 = the change), no error.
    T = D(2026, 12, 2)
    with caplog.at_level(logging.ERROR, logger="src.auth.payments_views"):
        (rows, later), delta = run(base(T, (FIX,), pattern=_no_pattern_in_november), D(2026, 11, 24), D(2026, 12, 1))
        assert rows == [] and later is None and delta == -183_000
        assert not [r for r in caplog.records if "pause_charge_rows" in r.getMessage()]
        (rows, later), delta = run(base(T, (FIX,)), D(2026, 11, 24), D(2026, 12, 1))
        assert shown(rows) == [("24.11–30.11", 3, -150_000), ("01.12", 1, -43_000)] and delta == -193_000 and later is None
        assert not [r for r in caplog.records if "pause_charge_rows" in r.getMessage()]


def test_generator_insurance_is_a_strict_violation_and_the_logger_is_left_as_it_was(caplog, monkeypatch):
    # Б11.44 М2 + М3.  By hand: (1) a real pause in the clean world (24.11 - 30.11, one line, no error): ``lines_bad`` False, the counter of the insurance 0; (2) the insurance forced to fire (the function logs the
    # error as the real one does): ``lines_bad`` True, the counter 1; (3) the world of the days (M5): lawful, False; (4) after all that the logger of the lines has no handler of the generator and still
    # propagates, so ``caplog`` sees an error written after the generator (the test of the journal does not depend on the order of the files).
    from tests import pay_tails_b7_tools as tools

    log = logging.getLogger("src.auth.payments_views")
    handlers, propagate = list(log.handlers), log.propagate
    inp = base(D(2026, 11, 24), (FIX,))
    new = replace(inp, pauses=(pause(D(2026, 11, 24), D(2026, 11, 30), inp.today),))
    start = tools.LINES["insurance"]
    assert tools.lines_bad(inp, new) is False and tools.LINES["insurance"] == start
    real = pv.pause_charge_rows

    def fires(*a, **k):
        logging.getLogger("src.auth.payments_views").error("pause_charge_rows: forced")
        return [], None

    monkeypatch.setattr(pv, "pause_charge_rows", fires)
    assert tools.lines_bad(inp, new) is True and tools.LINES["insurance"] == start + 1
    monkeypatch.setattr(pv, "pause_charge_rows", real)
    T = D(2026, 12, 2)
    days_world = base(T, (FIX,), pattern=_no_pattern_in_november)
    assert tools.lines_bad(days_world, replace(days_world, pauses=(pause(D(2026, 11, 24), D(2026, 12, 1), T),))) is False
    assert list(log.handlers) == handlers and log.propagate is propagate is True
    with caplog.at_level(logging.ERROR, logger="src.auth.payments_views"):
        log.error("pause_charge_rows: after the generator")
    assert [r.getMessage() for r in caplog.records if "after the generator" in r.getMessage()]
