"""«Деньги-2», проверка 5 (правило 142 п.5), through the endpoint: the statement of A after the transfer A -> B -> A in one day shows the lines of the day in the order they were
entered and the balances the student really had (never the advance 923 000 that he never had); ``reconciles`` stays true."""
from datetime import date

from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money2_transfer_a_pg import add, groups3, joined
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixtures)
from tests.test_payments_service_pg import pay

pytestmark = needs_pg
DAY = date(2026, 11, 22)


def test_the_statement_after_a_transfer_there_and_back_in_one_day_goes_pay_out_in(world, fixed_today):
    a, b, _c = groups3(world, fixed_today, DAY)
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    pay(world, s0, a, amount=600_000, paid_on=DAY.isoformat())
    add(world, s0, group=b, mode="transfer")
    add(world, s0, group=a, mode="transfer")
    m = membership(world, s0, a)
    r = owner(world).get(f"{API}/org/memberships/{m.id}/statement")
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["summary"]["reconciles"] is True
    day = [(x["type"], x["balance_after"]) for x in st["rows"] if x["date"] == DAY.isoformat()]
    kinds = [k for k, _ in day]
    assert kinds.index("payment") < kinds.index("transfer_out") < kinds.index("transfer_in"), day
    assert 923_000 not in [x["balance_after"] for x in st["rows"]]
    # every balance is the running sum of the lines in the order shown (the order changes the balances, not the end)
    run = 0
    for x in st["rows"]:
        run += x["credit"] - x["debit"] - x["outflow"]
        assert x["balance_after"] == run, x
