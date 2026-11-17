"""Money-2 stage fix 2 (У1-У5): the one-day stay rule at every reader (PostgreSQL).

Calendar: Tuesday 17.11.2026.  A = Mon/Wed/Fri (no lesson today), B = Tue/Thu/Sat (lesson today), C = Tue/Fri (lesson today).
November 2026: B = Tue 3,10,17,24 + Thu 5,12,19,26 + Sat 7,14,21,28 = 12 lessons; C = Tue 3,10,17,24 + Fri 6,13,20,27 = 8 lessons.
C from today: Tue 17, 24 + Fri 20, 27 = 4 of 8 = 250 000 of 500 000; from tomorrow: 24, 20, 27 = 3 of 8 = 187 500 -> 188 000 (whole thousands)."""
from datetime import date

import pytest

from tests.money1b_helpers import add, fresh_student, hold_lessons
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money2_sameday_pg import numbers, settle, three
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)

pytestmark = needs_pg


@pytest.mark.parametrize("mode", ["shared", "personal"])
def test_u1_wrong_group_and_moved_the_same_day_the_new_group_charges_todays_lesson(world, fixed_today, mode):
    # written down into B (a lesson today) by mistake and moved the same day into C (a lesson today): B had a stay of no day -> it holds no day -> C charges from TODAY
    a, b, c = three(world, fixed_today, mode)
    s = fresh_student(world, "Ошибся")
    add(world, s, group=b, mode="add")
    add(world, s, group=c, mode="transfer")
    nb, nc = numbers(world, s, b), numbers(world, s, c)
    assert nb[0] == 0 and nb[3] == 0, nb
    assert nc[0] == (250_000 if mode == "shared" else 500_000), nc      # shared: 4 of 8 lessons; a personal period: the whole period of 500 000 from today


def test_u1_a_to_b_to_c_the_same_day_c_charges_todays_lesson_b_nothing_and_the_money_is_whole_in_c(world, fixed_today):
    a, b, c = three(world, fixed_today)
    s0 = world.students[0]
    settle(world, s0, a)
    add(world, s0, group=b, mode="transfer")
    carried_to_b = numbers(world, s0, b)[5]
    add(world, s0, group=c, mode="transfer")
    nb, nc = numbers(world, s0, b), numbers(world, s0, c)
    assert nb[0] == 0 and nb[3] == 0 and nb[5] == 0, nb
    assert nc[0] == 250_000 and nc[5] == carried_to_b > 0, nc


def test_u1_an_ordinary_transfer_from_an_old_group_with_a_lesson_today_keeps_the_day_in_the_old_group(world, fixed_today):
    a, b, c = three(world, fixed_today)
    s0 = fresh_student(world, "Давний")
    add(world, s0, group=b, mode="add")
    settle(world, s0, b, joined=date(2026, 11, 2), paid=450_000)
    add(world, s0, group=c, mode="transfer")
    nb, nc = numbers(world, s0, b), numbers(world, s0, c)
    # B: 7 of 12 lessons up to and including today = 262 500 -> 263 000, the rest of the 450 000 (187 000) went to C; C starts TOMORROW (nothing charged yet): the day is neither paid twice nor lost
    assert nb[0] == 263_000 and nc[0] == 0 and nc[2] == 187_000 and nc[3] == 0, (nb, nc)
