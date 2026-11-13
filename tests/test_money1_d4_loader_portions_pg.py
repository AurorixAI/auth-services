"""Money-1 Д-4 (R3B В-4): the loader of the money reads the schedule of every group it meets, also in the later portions (the night reconciliation runs one loader over portions of 150
memberships) - a group of the second portion keeps its overrides, one-off lessons and moves, so its account equals the account the same group has alone."""
from datetime import date, datetime

from src.models.auth import GroupScheduleOverride, StudentGroup

from tests.money1b_helpers import set_schedule, slot
from tests.pg_world import give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_loader_pg import D, join_on, loader, membership_of, set_payments_settings

pytestmark = needs_pg


def _world(world):
    db = world.db
    group_a, group_b = world.group, make_group(db, world.org, world.teacher, "Группа B")
    set_schedule(world, [slot("m", 1, "2026-01-01")], group_a)
    set_schedule(world, [slot("m", 1, "2026-01-01")], group_b)
    # B: the Monday lesson stops from 20.10 (a managed end of the schedule) - an override, not a column of the group
    db.add(GroupScheduleOverride(org_id=world.org.id, group_id=group_b.id, lesson_id="m", scope="recurring_end", effective_date="2026-10-20", payload={}, reason="тест", changed_by=world.owner.id))
    db.commit()
    set_payments_settings(db, world.org, accounting_start_date="2026-09-01", period_mode="calendar", billing_anchor="fixed_day", billing_day=1)
    give_group_price(db, group_a, 400_000)
    give_group_price(db, group_b, 400_000)
    a = membership_of(db, world.students[0], group_a)
    join_on(db, a, D(2026, 10, 10))
    b = StudentGroup(student_id=world.students[1].id, group_id=group_b.id, is_active=True, joined_at=datetime(2026, 10, 10, 6))
    db.add(b)
    db.commit()
    return a, b


def _charged(entries, mid):
    return next(e.ledger.charged for e in entries if e.membership.id == mid)


def test_d4_a_group_of_the_second_portion_has_the_same_account_as_alone(world):
    a, b = _world(world)
    alone = _charged(loader(world, D(2026, 10, 31)).run([b.id]), b.id)
    one = loader(world, D(2026, 10, 31))
    one.run([a.id])                                                           # the first portion: the schedule is read for the groups known so far
    portion = _charged(one.run([b.id]), b.id)                                 # the second portion: another group, with its override
    assert alone == 267_000                                                   # Mondays of B: 05, 12, 19 (the schedule stops on 20.10); he came on 10.10: 2 of 3 of 400 000, rounded to 1000
    assert portion == alone
