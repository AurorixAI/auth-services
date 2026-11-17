"""Money-2, the second walk of stage (С4): the flags of the asking role in every answer that carries them (PostgreSQL)."""
import pytest

from tests.test_money2_fix_r3_pg import admin_client, void_it
from tests.test_money2_fix_r3b_pg import make_voided_by, payments_of
from tests.test_money2_fix_r3c_pg import BASE, STATES, launched, world_g1
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)

pytestmark = needs_pg


def test_c4a_restorable_of_a_payment_the_owner_cancelled_is_false_for_the_administrator_in_every_answer_and_true_for_the_owner(world, fixed_today):
    a, s0, e7 = make_voided_by(world, fixed_today, "owner")
    m = membership(world, s0, a).id
    adm, own = admin_client(world), owner(world)
    assert payments_of(adm, m)[e7["id"]]["restorable"] is False and payments_of(own, m)[e7["id"]]["restorable"] is True        # the list of payments
    for client, expected in ((adm, False), (own, True)):
        (row,) = [p for p in client.get(f"{API}/org/memberships/{m}/payments", params={"include_voided": "true"}).json() if p["id"] == e7["id"]]
        assert row["restorable"] is expected
        statement = client.get(f"{API}/org/memberships/{m}/statement", params={"include_voided": "true"}).json()
        assert all("restorable" not in r for r in statement["voided"])           # the statement carries no flag: the screen asks the list above
    # the service itself, by role: the same payment, the same answer as the route
    from src.auth.payments_service import PaymentService

    for role, expected in (("admin", False), ("owner", True)):
        (row,) = [p for p in PaymentService(world.db).list_payments(str(world.org.id), m, actor_role=role) if str(p.id) == e7["id"]]
        assert row.restorable is expected


@pytest.mark.parametrize("state", STATES)
def test_c4b_a_row_of_the_table_of_openings_is_not_editable_for_the_one_who_may_not_set_an_opening(world, fixed_today, state):
    a, s0, admin = world_g1(world, fixed_today, state)
    for client, may in ((owner(world), True), (admin, not launched(state))):
        table = client.get(BASE).json()
        rows = [r for g in table["groups"] for r in g["memberships"]]
        assert rows and table["can_set_opening"] is may
        assert all(r["editable"] is True for r in rows if may) and all(r["editable"] is False for r in rows if not may)
