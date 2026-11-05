"""Money-1 pack B4 / ДЫРА-13 (PostgreSQL): a new day of the week does not change the cells that are already closed.

The shared cell of a fixed billing day is priced by lessons: ``price x the lessons of the student / the lessons of the whole month by the schedule``.  The denominator used the schedule with
every ``validFrom`` ignored, so a Tuesday added «from 01.12» made the cell of OCTOBER cheaper after the fact (d1 of the devil's analysis: 115 000 -> 88 000).  Now a slot added later counts from its
real date; the slots of the original schedule still count for the whole month (a group that opens in the middle of a month has the month by its schedule)."""
from datetime import date

from tests.money1b_helpers import account, charge_of, set_schedule, slot
from tests.pg_world import give_group_price, needs_pg, payments_block, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import fixed_today, membership, stay  # noqa: F401  (fixtures)

pytestmark = needs_pg
D = date
OCT = D(2026, 10, 1)
BASE = [slot("mon", 1, "2026-09-01"), slot("wed", 3, "2026-09-01"), slot("fri", 5, "2026-09-01")]       # 13 lessons in October 2026 (Mon 5, 12, 19, 26; Wed 7, 14, 21, 28; Fri 2, 9, 16, 23, 30)


def october_world(w, fixed_today, schedule):
    fixed_today(D(2026, 12, 2))
    payments_block(w.db, w.org, accounting_start_date="2026-09-01", period_mode="calendar", billing_anchor="fixed_day", billing_day=1)
    give_group_price(w.db, w.group, 300_000)
    set_schedule(w, schedule)
    m = membership(w)
    stay(w, m, D(2026, 10, 20))                      # came on 20.10: 5 lessons of 13 (Wed 21, Fri 23, Mon 26, Wed 28, Fri 30)
    return m


def test_b4_adding_a_day_of_the_week_later_does_not_change_the_closed_cell_of_october(world, fixed_today):
    w = world
    m = october_world(w, fixed_today, BASE)
    assert charge_of(account(w, m.id), OCT) == 115_000                                                         # 5 / 13 x 300 000
    set_schedule(w, [*BASE, slot("tue", 2, "2026-12-01")])                                                       # the owner adds a Tuesday from 01.12
    assert charge_of(account(w, m.id), OCT) == 115_000                                                         # before: 5 / 17 = 88 000 (4 Tuesdays of October joined the denominator)


def test_b4_a_group_that_opens_mid_month_still_has_the_whole_month_by_its_schedule(world, fixed_today):
    w = world
    opened = [slot("mon", 1, "2026-10-15"), slot("wed", 3, "2026-10-15"), slot("fri", 5, "2026-10-15")]        # all the slots are from the same day: the ORIGINAL schedule
    m = october_world(w, fixed_today, opened)
    assert charge_of(account(w, m.id), OCT) == 115_000                                                         # the denominator is the month, 13, not the lessons since 15.10


def test_b4_a_slot_added_inside_the_month_counts_from_its_date_in_the_denominator(world, fixed_today):
    w = world
    m = october_world(w, fixed_today, [*BASE, slot("tue", 2, "2026-10-13")])                                   # Tuesday from 13.10: 13, 20, 27 = 3 lessons join the month
    seg = [s for s in account(w, m.id).segments if s.period_start == OCT][0]
    assert (seg.lessons_in_part, seg.lessons_in_period) == (7, 16)                                              # his lessons: 5 + Tue 20, 27; the month: 13 + 3
    assert seg.amount == 131_000                                                                               # 7 / 16 x 300 000 = 131 250
