"""PAY-TAILS Б1.6 (step 6): the small things of the server part.  PostgreSQL, through the routes of the product."""
from datetime import date

from src.auth import payments_texts as tx
from src.models.auth import GroupAudit, MembershipPriceOverride

from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixtures)
from tests.test_pay_tails_golden_pg import get
from tests.test_pay_tails_money_pg import lp, pay

pytestmark = needs_pg
D = date


def test_back_to_the_group_price_for_a_student_without_a_personal_price_writes_nothing(world, fixed_today):
    g, s = lp(world, fixed_today)
    mid = membership(world, s, g).id
    before = (world.db.query(GroupAudit).count(), world.db.query(MembershipPriceOverride).count())
    r = owner(world).put(f"{API}/org/memberships/{mid}/price-override", json={"amount": None})
    assert r.status_code == 200, r.text
    world.db.expire_all()
    assert (world.db.query(GroupAudit).count(), world.db.query(MembershipPriceOverride).count()) == before      # no row of the book, no line of the journal
    # a real individual price and the way back DO write (the control: the guard is not a switch-off of the route)
    assert owner(world).put(f"{API}/org/memberships/{mid}/price-override", json={"amount": 300_000, "reason": "Скидка семье"}).status_code == 200
    world.db.expire_all()
    lines = world.db.query(GroupAudit).count()
    assert lines == before[0] + 1                                              # the individual price wrote its line
    assert owner(world).put(f"{API}/org/memberships/{mid}/price-override", json={"amount": None}).status_code == 200
    world.db.expire_all()
    assert world.db.query(GroupAudit).count() == lines + 1                      # and the way back to the group price too (a real change)


def test_the_running_balance_of_a_statement_row_speaks_the_dictionary(world, fixed_today):
    g, s = lp(world, fixed_today)
    pay(world, s, g, amount=500_000, paid_on=D(2026, 11, 1))             # 420 000 charged, 500 000 paid: advance 80 000
    st = get(world, f"/org/memberships/{membership(world, s, g).id}/statement")
    texts = [r["balance_text"] for r in st["rows"]]
    assert texts[-1] == "Аванс " + tx.money(80_000) and all(t == "0" or t.endswith("сум") for t in texts)      # a sum always has its «сум»
