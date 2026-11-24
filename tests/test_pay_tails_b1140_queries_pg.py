"""PAY-TAILS Б11.40 (step 8.14.9): the number of SQL statements of the routes of a pause and of the list of the payments does not grow with the lines (the lines read the two accounts the routes already counted).
The numbers are printed (``-s``) and written to ``$PAYTAILS_QUERIES_FILE`` when it is set (the same test runs on the head before the step: the numbers are compared)."""
import os
from datetime import date

from sqlalchemy import event

from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixtures)
from tests.test_pay_tails_b11_pg import om_at

pytestmark = needs_pg
D = date


def counted(world, call):
    engine = world.db.get_bind()
    n = [0]

    def hit(*a, **k):
        n[0] += 1

    event.listen(engine, "before_cursor_execute", hit)
    try:
        r = call()
    finally:
        event.remove(engine, "before_cursor_execute", hit)
    assert r.status_code in (200, 201), r.text[:300]
    return n[0], r


def test_the_statements_of_the_routes_of_a_pause_and_of_the_list(world, fixed_today):
    g, s = om_at(world, fixed_today, D(2026, 11, 1))
    fixed_today(D(2026, 11, 24))
    url = f"{API}/org/memberships/{membership(world, s, g).id}/pauses"
    body = {"first_day": "2026-11-24", "last_day": "2026-12-03", "reason": "Болезнь"}
    out = {}
    out["probe"], _ = counted(world, lambda: owner(world).post(url, params={"dry_run": "true"}, json=body))
    out["save"], r = counted(world, lambda: owner(world).post(url, json=body))
    pid = r.json()["pause"]["id"]
    out["edit probe"], _ = counted(world, lambda: owner(world).patch(f"{API}/org/pauses/{pid}", params={"dry_run": "true"}, json={"last_day": "2026-11-30"}))
    out["void probe"], _ = counted(world, lambda: owner(world).post(f"{API}/org/pauses/{pid}/void", params={"dry_run": "true"}, json={"reason": "Передумали"}))
    out["void"], _ = counted(world, lambda: owner(world).post(f"{API}/org/pauses/{pid}/void", json={"reason": "Передумали"}))
    out["list"], _ = counted(world, lambda: owner(world).get(f"{API}/org/payments/status", params={"bucket": "active", "limit": 500}))
    line = "QUERIES " + " ".join(f"{k}={v}" for k, v in out.items())
    print(line)
    if os.environ.get("PAYTAILS_QUERIES_FILE"):
        open(os.environ["PAYTAILS_QUERIES_FILE"], "a", encoding="utf-8").write(line + "\n")
