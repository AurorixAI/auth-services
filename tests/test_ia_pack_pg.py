"""IA fix pack (server needs, 22.10): student edit with a journal row + clearing phone / note, receipt links, the teacher filter of the students list,
the owner's vocabulary in the statement."""
from datetime import timedelta

from tests.pg_world import give_group_price, make_client, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import (  # noqa: F401
    API, D, TODAY, fixed_today, membership, owner, post_payment, pytestmark, seed_payment, settings_block, stay,
)


def test_the_pause_row_of_the_statement_says_the_cost_is_not_counted(world, fixed_today):
    world.group.schedule = []
    world.db.commit()
    settings_block(world, accounting_start_date="2026-10-01", billing_anchor="fixed_day", billing_day=1)
    give_group_price(world.db, world.group, 600_000, valid_from=D(2026, 1, 1))
    m = membership(world)
    stay(world, m, D(2026, 10, 1))
    r = owner(world).post(f"{API}/org/memberships/{m.id}/pauses", json={"first_day": (TODAY - timedelta(days=5)).isoformat(), "last_day": (TODAY - timedelta(days=2)).isoformat()})
    assert r.status_code == 201, r.text
    rows = owner(world).get(f"{API}/org/memberships/{m.id}/statement").json()["rows"]
    title = next(x["title"] for x in rows if x["type"] == "pause")
    assert title.endswith("— стоимость не считается") and "начисл" not in title
