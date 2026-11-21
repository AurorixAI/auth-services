"""PAY-TAILS Б5.3: the list of 300 rows when EVERY row has a forecast (the charge of all of them begins later) against the same list when none has: the forecast is one more reading of the account of a row, no new loader.
The numbers are printed (``-s``); the test only keeps the cost within a loose bound (a regression of an order of magnitude)."""
import time
from datetime import date

from tests.money1b_helpers import add, fresh_student
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, owner  # noqa: F401  (fixtures)
from tests.test_pay_tails_upcoming_pg import world_om

pytestmark = needs_pg
D = date
N = 300


def timed(w, **params):
    best = None
    for _ in range(3):
        w.db.expire_all()
        start = time.perf_counter()
        r = owner(w).get(f"{API}/org/payments/status", params={"bucket": "active", "limit": 500, **params})
        took = time.perf_counter() - start
        assert r.status_code == 200, r.text
        best = took if best is None else min(best, took)
    return best, r.json()


def test_a_list_of_300_rows_with_a_forecast_on_every_row(world, fixed_today):
    g = world_om(world, fixed_today)
    g.max_students = None                                                        # a group of 300 (the setup of the world, not a step of the person)
    world.db.commit()
    ids = [fresh_student(world, f"Ученик{i:03d}") for i in range(N)]
    r = owner(world).post(f"{API}/org/groups/{g.id}/students/bulk", params={"mode": "add"}, json={"student_ids": [str(s.id) for s in ids]})
    assert r.status_code == 200 and r.json()["added"] == N, r.text[:300]
    with_forecast, page = timed(world)
    assert page["total"] == N and sum(1 for x in page["items"] if x["state"] == "starts") == N
    fixed_today(D(2026, 11, 24))                                                 # the charge has begun: no forecast on any row
    without, page2 = timed(world, bucket="to_pay")
    line = f"PERF rows={N} with forecast on every row: {with_forecast:.2f}s; the same list when nothing starts later: {without:.2f}s"
    print(line)
    import os
    if os.environ.get("PAYTAILS_PERF_FILE"):
        open(os.environ["PAYTAILS_PERF_FILE"], "a", encoding="utf-8").write(line + "\n")
    assert with_forecast < 60
