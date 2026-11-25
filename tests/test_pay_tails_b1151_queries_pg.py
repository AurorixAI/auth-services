"""PAY-TAILS 8.14.16: the number of SQL statements of the return (the probe and the record) - printed (``-s``) and written to ``$PAYTAILS_QUERIES_FILE`` when it is set (the same test runs on the head before the step: the numbers are compared)."""
import os
from datetime import date as D

from sqlalchemy import event

from tests.money1b_helpers import add
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import fixed_today, settings_block  # noqa: F401  (fixture)
from tests.test_pay_tails_b11_pg import om_at
from tests.test_pay_tails_money_pg import leave

pytestmark = needs_pg


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
    return n[0], r


def test_the_statements_of_a_return(world, fixed_today):
    g, s = om_at(world, fixed_today, D(2026, 11, 2))
    fixed_today(D(2026, 11, 25))
    leave(world, g, s)
    fixed_today(D(2026, 11, 26))
    out = {}
    out["probe"], _ = counted(world, lambda: add(world, s, group=g, mode="add", dry_run=True))
    out["record"], _ = counted(world, lambda: add(world, s, group=g, mode="add"))
    line = "QUERIES " + " ".join(f"{k}={v}" for k, v in out.items())
    print(line)
    if os.environ.get("PAYTAILS_QUERIES_FILE"):
        open(os.environ["PAYTAILS_QUERIES_FILE"], "a", encoding="utf-8").write(line + "\n")
