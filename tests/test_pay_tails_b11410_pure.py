"""PAY-TAILS Б11.42 п. 2 (step 8.14.10): the KIND of a period for the folding of closed lines of a payment.  A cell of the common month whose billing day is not the 1st (``by_cell``, not a calendar month) is
a kind of its own: after a change of the mode it must not fold with a personal period (``by_cell`` false) that begins the day after it ends."""
from datetime import date
from types import SimpleNamespace

from src.auth import payments_views as pv

D = date


def seg(first, last, by_cell, kind="period"):
    return SimpleNamespace(kind=kind, by_cell=by_cell, period_start=first, period_end=last, package_no=None)


CELL = seg(D(2026, 10, 15), D(2026, 11, 14), True)          # the common month, billing day the 15th
CELL2 = seg(D(2026, 11, 15), D(2026, 12, 14), True)
PERSONAL = seg(D(2026, 11, 15), D(2026, 12, 14), False)     # a personal period that begins the day after the cell
MONTH = seg(D(2026, 11, 1), D(2026, 11, 30), True)
MONTH2 = seg(D(2026, 12, 1), D(2026, 12, 31), True)


def test_the_kinds():
    assert [pv._folding_kind(x) for x in (CELL, PERSONAL, MONTH)] == ["cell", "personal", "month"]


def test_periods_of_different_modes_in_a_row_are_not_folded():
    assert pv._next_period(CELL, PERSONAL) is False
    assert pv._next_period(PERSONAL, CELL2) is False
    assert pv._next_period(MONTH, seg(D(2026, 12, 1), D(2026, 12, 14), False)) is False


def test_the_same_kind_in_a_row_is_folded_as_before():
    assert pv._next_period(CELL, CELL2) is True
    assert pv._next_period(MONTH, MONTH2) is True
    assert pv._next_period(PERSONAL, seg(D(2026, 12, 15), D(2027, 1, 14), False)) is True
    assert pv._next_period(MONTH2, MONTH) is False           # not one after another in the calendar
