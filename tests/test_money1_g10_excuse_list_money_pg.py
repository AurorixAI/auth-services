"""Money-1 G-10 (MONEY1_G1_REVIEW П5/T30): the list of certificates shows ``deadline`` and ``billing_mode`` - the account of the student - only to the money right;
without it the list is the reason, the lesson and the status."""
from tests.ops_helpers import grant_money_right
from tests.pg_world import make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_excuse_billing_mode_pg import excuse_all
from tests.test_excuse_deadline import API, listing
from tests.test_payments_service_pg import day, pay, set_settings

pytestmark = needs_pg


def _seed(w):
    set_settings(w, period_mode="lessons", lessons_per_payment=12)
    pay(w, w.students[0], paid_on=day(-30), lessons_count=12)
    excuse_all(w)


def test_with_the_money_right_the_list_carries_the_account(world):
    grant_money_right(world)
    _seed(world)
    paid = next(i for i in listing(world) if i["student"]["id"] == str(world.students[0].id))
    assert paid["billing_mode"] == "package" and paid["deadline"] is not None


def test_without_the_money_right_the_list_has_no_deadline_and_no_mode_but_everything_else(world):
    grant_money_right(world)
    _seed(world)
    with_right = {i["id"]: i for i in listing(world)}
    grant_money_right(world, False)
    queue = make_client(world).get(f"{API}/org/attendance/excuses").json()["items"]               # no dates: the queue of all periods
    for items in (queue, listing(world), listing(world, status="pending")):
        assert items and all(i["deadline"] is None and i["billing_mode"] is None and i["deadline_at_decision"] is None for i in items)
        for i in items:
            full = with_right[i["id"]]
            assert {k: v for k, v in i.items() if k not in ("deadline", "billing_mode", "deadline_at_decision")} == \
                   {k: v for k, v in full.items() if k not in ("deadline", "billing_mode", "deadline_at_decision")}
