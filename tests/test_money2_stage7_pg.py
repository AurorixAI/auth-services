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


def test_m3_the_single_adding_with_an_individual_price_on_a_cancelled_leaving_is_refused_without_a_price_it_is_cancelled(world, fixed_today):
    a, b, c = groups3(world, fixed_today, SUNDAY)
    s2, before = returning(world, b)
    r = owner(world).post(f"{API}/org/groups/{b.id}/students/{s2.id}", params={"mode": "add"}, json={"price_override": {"amount": 250_000, "reason": "новая цена"}})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "LEAVE_UNDO_NO_START" and r.json()["detail"]["message"] == NEW_TEXT, r.text
    world.db.expire_all()
    assert membership(world, s2, b).is_active is False and [r[1] for r in prices(world, s2, b) if r[1] is not None] == [300_000]      # the marker of the leaving (no amount) is there: the leaving is NOT cancelled
    assert add(world, s2, group=b, mode="add")["leave_undo"]["undone"] is True and snap(world, s2, b) == before


