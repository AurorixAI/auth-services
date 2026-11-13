"""Money-1 G-8 (MONEY1_G1_REVIEW N14): «removed from A, added to B» by hand on ONE day - that day belongs to the group he left, B charges from the next day
(as it does after a transfer, rule B6).  A student who is added to B while he STAYS in A has no such day."""
from datetime import date, datetime, timedelta

from src.models.auth import StudentGroup

from tests.pg_world import give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_loader_pg import D, join_on, loader, membership_of, set_payments_settings

pytestmark = needs_pg


def _two_groups(world, a_has_lessons=False):
    db = world.db
    group_a, group_b = world.group, make_group(db, world.org, world.teacher, "Группа B")
    group_b.schedule = []                         # no lessons: an incomplete period is charged by days
    if not a_has_lessons:
        group_a.schedule = []
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


# ---- Г-8b: the day belongs to the old group only when it really charges it ---------------------------------------------------------------------------------

def test_a_one_day_stay_in_a_without_a_lesson_charges_nothing_so_b_charges_from_that_day(world):
    a_group, b_group = _two_groups(world)
    a = membership_of(world.db, world.students[0], a_group)
    join_on(world.db, a, D(2026, 10, 11), left=D(2026, 10, 11))          # added and removed on 11.10, A has no lesson that day
    b = _join_b(world, b_group, D(2026, 10, 11))
    la, lb = _ledgers(world, a, b)
    assert la.charged == 0 and lb.charged == 271_000 and lb.first_billable == D(2026, 10, 11)      # 21 of 31 days of 400 000: the 11th is nobody's otherwise


def test_a_one_day_stay_in_a_with_a_lesson_charges_that_day_so_b_starts_the_next(world):
    a_group, b_group = _two_groups(world, a_has_lessons=True)           # a lesson every day
    a = membership_of(world.db, world.students[0], a_group)
    join_on(world.db, a, D(2026, 10, 11), left=D(2026, 10, 11))
    b = _join_b(world, b_group, D(2026, 10, 11))
    la, lb = _ledgers(world, a, b)
    assert la.charged > 0 and lb.charged == 258_000 and lb.first_billable == D(2026, 10, 12)


# ---- Р-6 (R3 В5): the old group keeps the day T only when it CHARGES it - a per-day share, or a lesson of its own that day -----------------------------------------
# One rule for the transfer and for the manual «removed from A, added to B»: a day that A does not charge is never lost - B charges it from T.

from tests.money1b_helpers import account, set_schedule, slot  # noqa: E402
from tests.test_pay2_api_pg import fixed_today, settings_block, stay  # noqa: E402,F401  (fixtures)

T = D(2026, 10, 27)                                                      # a Tuesday
SHARED_MONTH = dict(accounting_start_date="2026-09-01", period_mode="calendar", billing_anchor="fixed_day", billing_day=1)


def _by_lessons(world, a_weekdays, b_weekdays):
    group_a, group_b = world.group, make_group(world.db, world.org, world.teacher, "Группа B")
    set_schedule(world, [slot(f"a{d}", d, "2026-01-01") for d in a_weekdays], group_a)
    set_schedule(world, [slot(f"b{d}", d, "2026-01-01") for d in b_weekdays], group_b)
    set_payments_settings(world.db, world.org, **SHARED_MONTH)
    give_group_price(world.db, group_a, 500_000)
    give_group_price(world.db, group_b, 400_000)
    return group_a, group_b


def _account_b(world, b, today=D(2026, 10, 31)):
    return {r.membership.id: r.ledger for r in loader(world, today).load(membership_ids=[b.id], scope="all")}[b.id]


def test_r6_manual_a_charges_no_lesson_on_the_day_so_b_charges_it_from_that_day(world):
    a_group, b_group = _by_lessons(world, a_weekdays=(1, 3), b_weekdays=(2,))                # A: Mon / Wed, B: Tue; T = Tuesday 27.10
    a = membership_of(world.db, world.students[0], a_group)
    join_on(world.db, a, D(2026, 10, 1), left=T)
    b = _join_b(world, b_group, T)
    lb = _account_b(world, b)
    assert lb.first_billable == T and lb.charged == 100_000       # B has 4 Tuesdays in October, the lesson of T is the first of his: 1/4 of 400 000 (before: 0, the lesson of the 27th was nobody's)


def test_r6_manual_a_charges_a_lesson_on_the_day_so_b_starts_the_next_day(world):
    a_group, b_group = _by_lessons(world, a_weekdays=(2,), b_weekdays=(2, 3))                # A has a lesson on Tuesday: the day is A's
    a = membership_of(world.db, world.students[0], a_group)
    join_on(world.db, a, D(2026, 10, 1), left=T)
    b = _join_b(world, b_group, T)
    lb = _account_b(world, b)
    assert lb.first_billable == T + timedelta(days=1) and lb.charged == 50_000               # Wed 28.10 is the one lesson of B in the part: 1/8 of 400 000


def test_r6_transfer_a_charges_no_lesson_on_the_day_so_b_charges_it_from_that_day(world, fixed_today):
    w = world
    fixed_today(T)
    settings_block(w, **SHARED_MONTH)
    a_group, b_group = _by_lessons(w, a_weekdays=(1, 3), b_weekdays=(2,))
    s = w.students[0]
    stay(w, membership_of(w.db, s, a_group), D(2026, 9, 25))
    from tests.money1b_helpers import transfer

    transfer(w, s, b_group)
    w.db.expire_all()
    mb = membership_of(w.db, s, b_group)
    lb = account(w, mb.id, D(2026, 10, 31))
    assert lb.first_billable == T and lb.charged == 100_000


def test_r6_transfer_a_charges_a_lesson_on_the_day_so_b_starts_the_next_day(world, fixed_today):
    w = world
    fixed_today(T)
    settings_block(w, **SHARED_MONTH)
    a_group, b_group = _by_lessons(w, a_weekdays=(2,), b_weekdays=(2, 3))
    s = w.students[0]
    stay(w, membership_of(w.db, s, a_group), D(2026, 9, 25))
    from tests.money1b_helpers import transfer

    transfer(w, s, b_group)
    w.db.expire_all()
    lb = account(w, membership_of(w.db, s, b_group).id, D(2026, 10, 31))
    assert lb.first_billable == T + timedelta(days=1) and lb.charged == 50_000
