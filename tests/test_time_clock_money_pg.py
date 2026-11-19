"""TIME-TESTS (server): the reconciliation and the till of the day do the same under the stand clock (an offset to tomorrow / to the next month) as under the frozen clock of the tests.

The point of ``business_clock.utc_now`` as the default of ``created_at``: a row made "tomorrow" is NEWER than the snapshot of the reconciliation taken "today" and is stamped
with the business day it was made on.  With the clock of the database (``now()``) the row would be older than the snapshot (the change is not noticed) and would land in the wrong day of the till.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.auth import business_clock as bc
from src.auth import money_reconcile as mr
from src.models.auth import GroupPrice, MoneyReconcileSnapshot

from tests.pg_world import ensure_billing, give_group_price, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_income2_pg import income
from tests.test_payments_service_pg import pay
from tests.time_clock_helpers import stand_clock  # noqa: F401  (fixture)

pytestmark = needs_pg


def _targets():
    """The three moments of the scenario, all computed from the real business day (the stand clock only moves forward): now, tomorrow 10:00, the 1st of the next month +1 day 10:00."""
    now = bc.real_utc_now().astimezone(bc.BUSINESS_TIMEZONE)
    tomorrow = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    first_next = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
    next_month = datetime.combine(first_next + timedelta(days=1), datetime.min.time(), tzinfo=bc.BUSINESS_TIMEZONE).replace(hour=10)
    return now, tomorrow, next_month


def _scenario(world, move_to):
    """The same human steps at three moments; returns what the screens and the reconciliation said."""
    out = {}
    now, tomorrow, next_month = _targets()
    s0 = world.students[0]
    move_to(now)
    ensure_billing(world)
    pay(world, s0, amount=100_000)
    mr.run(world.db, apply=True, only=[world.org.id])                              # night 1: the snapshots are taken NOW
    snaps = {s.membership_id: s.taken_at for s in world.db.query(MoneyReconcileSnapshot).filter_by(org_id=world.org.id).all()}
    day0 = bc.business_today()
    out["day0_cash"] = income(world, day0.isoformat(), day0.isoformat())["cash"]["expected"]

    move_to(tomorrow)
    day1 = bc.business_today()
    give_group_price(world.db, world.group, 500_000, valid_from=day1)               # a lawful move of the coverage: a new price, made "tomorrow"
    world.db.commit()
    pay(world, s0, amount=50_000)
    _, coverage = mr._explaining_events(world.db, world.org.id, snaps)
    out["price_explains_coverage"] = set(coverage) == set(snaps) or bool(coverage)
    out["day1_cash"] = income(world, day1.isoformat(), day1.isoformat())["cash"]["expected"]
    out["day0_cash_after"] = income(world, day0.isoformat(), day0.isoformat())["cash"]["expected"]
    second = mr.run(world.db, apply=True, only=[world.org.id])
    out["findings_tomorrow"] = sorted((i["invariant"], i["key"].split(":")[0]) for i in second["items"] if i["org"] == str(world.org.id))

    move_to(next_month)
    day2 = bc.business_today()
    pay(world, s0, amount=25_000)
    out["month_cash"] = income(world, day2.isoformat(), day2.isoformat())["cash"]["expected"]
    third = mr.run(world.db, apply=True, only=[world.org.id])
    out["findings_next_month"] = sorted((i["invariant"], i["key"].split(":")[0]) for i in third["items"] if i["org"] == str(world.org.id))
    out["price_rows_stamped_on_the_business_day"] = [bc.utc_to_business_date(r.created_at) for r in world.db.query(GroupPrice).filter_by(group_id=world.group.id).all() if r.valid_from == day1]
    out["day1"] = day1
    return out


def test_under_the_frozen_clock_of_the_tests_the_scenario_gives_these_answers(world):
    def freeze(moment):
        bc.set_clock(lambda: moment)

    try:
        by_fixture = _scenario(world, freeze)
    finally:
        bc.set_clock(None)
    assert by_fixture["day0_cash"] == 100_000 and by_fixture["day1_cash"] == 50_000 and by_fixture["day0_cash_after"] == 100_000 and by_fixture["month_cash"] == 25_000
    assert by_fixture["findings_tomorrow"] == [] and by_fixture["findings_next_month"] == []
    assert by_fixture["price_explains_coverage"]
    assert by_fixture["price_rows_stamped_on_the_business_day"] == [by_fixture["day1"]]


def test_under_the_stand_clock_the_scenario_gives_the_same_answers(world, stand_clock):
    def move(moment):
        target = max(bc.offset_for_business_now(moment.replace(tzinfo=None)), bc.current_offset_seconds())
        bc.set_offset(target)

    by_stand = _scenario(world, move)
    assert by_stand["day0_cash"] == 100_000 and by_stand["day1_cash"] == 50_000 and by_stand["day0_cash_after"] == 100_000 and by_stand["month_cash"] == 25_000
    assert by_stand["findings_tomorrow"] == [] and by_stand["findings_next_month"] == []
    assert by_stand["price_explains_coverage"]
    assert by_stand["price_rows_stamped_on_the_business_day"] == [by_stand["day1"]]
