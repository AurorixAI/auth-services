"""Money-2 stage 7: the bulk adding cancels a leaving by itself (М1-М4), PostgreSQL.  The numbers are worked by hand in the comments.

Calendar: Sunday 22.11.2026; groups of ``groups3`` (A 300 000, B 400 000, C 500 000; a lesson every day)."""
from datetime import date

from src.models.auth import MembershipPriceOverride
from tests.money1b_helpers import add, fresh_student
from tests.pg_world import give_price, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money2_fix_r3_pg import leave, periods_billing_from
from tests.test_money2_transfer_a_pg import groups3, joined, pause, snap
from tests.test_payments_service_pg import pay
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)

pytestmark = needs_pg

SUNDAY = date(2026, 11, 22)
NEW_TEXT = "Ученик возвращается в день ухода — уход отменяется: дата начала оплаты, остаток и индивидуальная цена не меняются"
COMMON = {"pay_from": "2026-11-24", "price_override": {"amount": 400_000, "reason": "общая цена партии"}}


def bulk(w, group, students, *, mode="add", dry_run=False, client=None, **body):
    r = (client or owner(w)).post(f"{API}/org/groups/{group.id}/students/bulk", params={"mode": mode, **({"dry_run": "true"} if dry_run else {})},
                                  json={"student_ids": [str(s.id) for s in students], **body})
    assert r.status_code == 200, r.text
    return r.json()


def prices(w, student, group):
    w.db.expire_all()
    return sorted((r.valid_from, r.amount, r.reason) for r in w.db.query(MembershipPriceOverride).filter_by(membership_id=membership(w, student, group).id))


def returning(w, b, name="Вернувшийся"):
    """In B since 02.11 with an individual price 300 000 (B's own is 400 000) and the pause 22-24.11 (holds the day of leaving); paid 100 000; left today."""
    s = fresh_student(w, name)
    add(w, s, group=b, mode="add")
    joined(w, s, b, date(2026, 11, 2))
    give_price(w.db, membership(w, s, b), 300_000, reason="договорённость")
    pause(w, b, s, date(2026, 11, 22), date(2026, 11, 24))
    pay(w, s, b, amount=100_000)
    before = snap(w, s, b)
    leave(w, b, s)
    assert snap(w, s, b) != before
    return s, before


def test_m4_the_balance_row_of_this_group_says_same_group_and_the_rows_of_other_groups_do_not(world, fixed_today):
    from tests.test_money2_return_window_pg import _left_with_a_lesson, _window

    b, s = _left_with_a_lesson(world, fixed_today)                                               # B: left on Tue with a debt of 38 000
    (row,) = _window(world, b, s)["past_balances"]
    assert row["same_group"] is True and row["amount"] == 38_000
    from src.models.auth import Group

    gamma = world.db.query(Group).filter(Group.org_id == world.org.id, Group.name == "Гамма").one()
    other = _window(world, gamma, s)["past_balances"]
    assert [r["group_id"] for r in other] == [str(b.id)] and "same_group" not in other[0]
