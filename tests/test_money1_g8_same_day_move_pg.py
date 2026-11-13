"""Money-1 G-8 (MONEY1_G1_REVIEW N14): «removed from A, added to B» by hand on ONE day - that day belongs to the group he left, B charges from the next day
(as it does after a transfer, rule B6).  A student who is added to B while he STAYS in A has no such day."""
from datetime import date, datetime

from src.models.auth import StudentGroup

from tests.pg_world import give_group_price, make_group, needs_pg, world  # noqa: F401  (fixtures)
from tests.test_pay2_loader_pg import D, join_on, loader, membership_of, set_payments_settings

pytestmark = needs_pg


def _two_groups(world):
    db = world.db
    group_a, group_b = world.group, make_group(db, world.org, world.teacher, "Группа B")
    group_a.schedule = group_b.schedule = []      # no lessons: an incomplete period is charged by days
    db.commit()
    set_payments_settings(db, world.org, accounting_start_date="2026-09-01", period_mode="calendar", billing_anchor="fixed_day", billing_day=1)
    give_group_price(db, group_a, 500_000)
    give_group_price(db, group_b, 400_000)
    return group_a, group_b


def _join_b(world, group_b, day, left=None):
    db = world.db
    b = StudentGroup(student_id=world.students[0].id, group_id=group_b.id, is_active=left is None, joined_at=datetime(day.year, day.month, day.day, 6),
                     left_at=None if left is None else datetime(left.year, left.month, left.day, 6))
    db.add(b)
    db.commit()
    return b


def _ledgers(world, a, b):
    rows = {r.membership.id: r.ledger for r in loader(world, D(2026, 10, 25)).load(membership_ids=[a.id, b.id], scope="all")}
    return rows[a.id], rows[b.id]


def test_removed_from_a_and_added_to_b_the_same_day_is_charged_once(world):
    a_group, b_group = _two_groups(world)
    a = membership_of(world.db, world.students[0], a_group)
    join_on(world.db, a, D(2026, 10, 1), left=D(2026, 10, 11))
    b = _join_b(world, b_group, D(2026, 10, 11))
    la, lb = _ledgers(world, a, b)
    assert la.charged == 177_000                       # 11 of 31 days of 500 000: 1..11.10 - the 11th is A's
    assert lb.charged == 258_000                       # 20 of 31 days of 400 000: 12..31.10 (271 000 = 21 days, the 11th twice, used to be charged)
    assert lb.first_billable == D(2026, 10, 12)


def test_added_to_b_while_he_stays_in_a_is_charged_from_the_day_he_came(world):
    a_group, b_group = _two_groups(world)
    a = membership_of(world.db, world.students[0], a_group)
    join_on(world.db, a, D(2026, 10, 1))
    b = _join_b(world, b_group, D(2026, 10, 11))
    _, lb = _ledgers(world, a, b)
    assert lb.charged == 271_000 and lb.first_billable == D(2026, 10, 11)


def test_removed_from_a_added_to_b_and_removed_from_b_the_same_day_owes_nothing_in_b(world):
    a_group, b_group = _two_groups(world)
    a = membership_of(world.db, world.students[0], a_group)
    join_on(world.db, a, D(2026, 10, 1), left=D(2026, 10, 11))
    b = _join_b(world, b_group, D(2026, 10, 11), left=D(2026, 10, 11))
    la, lb = _ledgers(world, a, b)
    assert la.charged == 177_000 and lb.charged == 0 and lb.debt == 0
