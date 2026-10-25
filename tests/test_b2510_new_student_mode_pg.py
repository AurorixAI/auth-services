"""Batch 25.10 (stage walk): a student who joins on the day the centre switches the mode - or later - has no running period, so his first period goes by the NEW
mode; a student with a running period keeps the old one to the end of it (LOGIC-A F12)."""
import pytest

from tests.pg_world import give_group_price, pg_session, world  # noqa: F401
from tests.test_pay2_api_pg import API, D, fixed_today, membership, owner, seed_payment, settings_block, stay  # noqa: F401

pytestmark = pytest.mark.skipif(not __import__("os").environ.get("A2_AUTH_TEST_DATABASE_URL"), reason="Requires disposable A2 auth PostgreSQL")


def join_at(w, m, hour_utc):
    """The moment he joined, on the day of the stay (the helper `stay` puts 06:00 UTC)."""
    m.joined_at = m.joined_at.replace(hour=hour_utc)
    (period,) = m.periods
    period.joined_at = m.joined_at
    w.db.commit()


def row_of(w, student):
    r = owner(w).get(f"{API}/org/students/{student.id}/memberships")
    assert r.status_code == 200, r.text
    return r.json()[0]


def test_switch_then_enrol_the_same_day_gives_a_package_and_a_running_month_stays_a_month(world, fixed_today):
    settings_block(world, accounting_start_date="2026-09-01")                              # payment_date, a calendar month from the first day
    give_group_price(world.db, world.group, 600_000, valid_from=D(2026, 1, 1))
    running, yesterday, new = (membership(world, s) for s in world.students)
    stay(world, running, D(2026, 10, 15))
    seed_payment(world, running, 600_000, D(2026, 10, 15))                                 # a paid running month 15.10-14.11
    stay(world, yesterday, D(2026, 10, 20))                                                # came yesterday, nothing paid: his month is running (a debt)
    stay(world, new, D(2026, 10, 21))                                                      # comes on the day of the switch...
    c = owner(world)
    assert c.put(f"{API}/org/settings/payments", json={"period_mode": "lessons", "lessons_per_payment": 12}).status_code == 200     # the clock stands at 12:00 Tashkent = 07:00 UTC
    join_at(world, new, 8)                                                                  # ...AFTER the switch
    world.db.expire_all()
    a, b, n = (row_of(world, s) for s in world.students)
    assert (a["period_mode"], a["paid_until"], a["debt"]) == ("calendar", "2026-11-14", 0)           # the running period is not cut
    assert b["period_mode"] == "calendar" and b["debt"] == 600_000 and b["paid_until"] is None   # his running month (20.10-19.11) is due whole, the mode is the old one
    assert n["period_mode"] == "lessons"                                                              # his first period is a package
    assert n["debt"] == 0                                              # a package: the debt appears with the lessons held, not a month charged up front
    # a student who comes later still follows the new mode
    stay(world, new, D(2026, 10, 22))
    world.db.expire_all()
    assert row_of(world, world.students[2])["period_mode"] == "lessons"
    # a student who came the same morning BEFORE the switch keeps his running period
    stay(world, new, D(2026, 10, 21))
    join_at(world, new, 5)
    world.db.expire_all()
    assert row_of(world, world.students[2])["period_mode"] == "calendar"


def test_the_dry_run_counts_only_the_students_with_a_running_period(world, fixed_today):
    settings_block(world, accounting_start_date="2026-09-01")
    give_group_price(world.db, world.group, 600_000, valid_from=D(2026, 1, 1))
    a, b, n = (membership(world, s) for s in world.students)
    stay(world, a, D(2026, 10, 15)); stay(world, b, D(2026, 10, 20)); stay(world, n, D(2026, 10, 21))
    join_at(world, n, 5)                                                                    # came the same morning: a running period
    r = owner(world).put(f"{API}/org/settings/payments", params={"dry_run": True}, json={"period_mode": "lessons", "lessons_per_payment": 12})
    assert r.status_code == 200, r.text
    ri = r.json()["regime_impact"]
    assert ri["students_affected"] == 3 and ri["first_switch"] == "2026-11-15" and ri["latest_switch"] == "2026-11-21"
