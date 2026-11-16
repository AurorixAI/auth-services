"""Money-2, the round after the independent review (R3, 16.11): the holes of the row-by-row check I-5 (PostgreSQL).

Rule of these tests (LESSONS 105/115/116): a row that must have a «trace» is made THE WAY OF THE PRODUCT (a route), not by an insert - an insert has no line of the journal of its creation, so the
test could not see a hole that the creation line opens.  The distortion itself is a bare SQL statement under the feet of the code."""
from datetime import timedelta

import pytest

from src.models.auth import MembershipOpeningBalance, MoneyReconcileFinding

from tests.pg_world import business_today, ensure_billing, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money2_rows_pg import CANCEL, i2, i5, night, sql
from tests.test_payments_service_pg import mid
from tests.test_pay2_api_pg import API, fixed_today, owner  # noqa: F401  (fixture)

pytestmark = needs_pg


def opening_by_product(world, student=None, amount=250_000):
    """An opening balance made through the route of the product (it writes ``opening_balance_set`` with ``opening_id`` = the NEW row)."""
    student = student or world.students[2]
    on = (business_today() - timedelta(days=76)).isoformat()
    r = owner(world).put(f"{API}/org/memberships/{mid(world, student)}/opening-balance", json={"debt_amount": amount, "debt_on": on})
    assert r.status_code == 200, r.text
    return world.db.query(MembershipOpeningBalance).filter_by(org_id=world.org.id, membership_id=mid(world, student), voided_at=None).one().id


def test_r1_an_opening_made_by_the_product_and_cancelled_by_a_bare_update_has_no_line_of_cancellation(world):
    """The line ``opening_balance_set`` names the NEW row (the trace of the creation): it must not be taken for the trace of a cancellation of that row."""
    ensure_billing(world)
    night(world)
    row_id = opening_by_product(world)
    assert i5(night(world), world) == []
    sql(world, f"UPDATE membership_opening_balances SET {CANCEL} WHERE id = :id", id=row_id, owner=world.owner.id)
    assert f"opening:{row_id}:no_journal" in i5(night(world), world), "the debt went away without a line of cancellation"


def test_r1_an_opening_cancelled_by_the_product_and_one_replaced_by_the_product_leave_no_finding(world):
    ensure_billing(world)
    night(world)
    first = opening_by_product(world)
    night(world)
    second = opening_by_product(world, amount=300_000)                      # replaces the first one: the line ``opening_balance_set`` names it in ``voided_opening_id``
    assert second != first and i5(night(world), world) == []
    assert owner(world).post(f"{API}/org/opening-balances/{second}/void", json={"reason": "Ошибка ввода"}).status_code == 200
    assert i5(night(world), world) == []
