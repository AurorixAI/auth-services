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


def test_m1_m2_a_batch_of_three_the_common_fields_go_to_the_new_ones_and_the_returning_student_gets_his_leaving_cancelled(world, fixed_today):
    a, b, c = groups3(world, fixed_today, SUNDAY)
    s0, s1 = world.students[0], world.students[1]                        # s0, s1: in A, come to B as a second group (add)
    s2, before = returning(world, b)
    dry = bulk(world, b, [s0, s1, s2], dry_run=True, **COMMON)
    out = bulk(world, b, [s0, s1, s2], **COMMON)
    for answer in (dry, out):
        assert answer["added"] == 3 and answer["skipped"] == 0 and [r["status"] for r in answer["results"]] == ["added"] * 3, answer
        first, second, third = answer["students"]
        assert first["leave_undo"] is None and second["leave_undo"] is None
        assert third["leave_undo"]["undone"] is True and third["billing"] is None
    assert dry["students"][2]["leave_undo"] == out["students"][2]["leave_undo"]                  # the window says what the write does
    assert {i["code"] for i in out["students"][2]["leave_undo"]["restored"]} == {"individual_price", "pause"}
    assert snap(world, s2, b) == before                                                          # the statement, the price 300 000, the pause, «оплачено до»: as before the leaving
    assert [r[1] for r in prices(world, s2, b)] == [300_000]                                     # the common 400 000 is NOT written for him
    assert all(billing_from is None for _, billing_from in periods_billing_from(world, s2, b))
    for s in (s0, s1):
        assert [r[1] for r in prices(world, s, b)] == [400_000]                                  # the common price and «Оплата с» 24.11 are applied to the others
        assert periods_billing_from(world, s, b)[-1][1] == date(2026, 11, 24)


def test_m2_a_bulk_transfer_with_a_common_price_does_not_touch_the_returning_one(world, fixed_today):
    a, b, c = groups3(world, fixed_today, SUNDAY)
    s0 = world.students[0]                                                                       # moved A -> B with his own source
    s2, before = returning(world, b)
    out = bulk(world, b, [s0, s2], mode="transfer", price_override=COMMON["price_override"], sources={str(s0.id): str(a.id)})
    assert out["added"] == 2 and out["students"][1]["leave_undo"]["undone"] is True and out["students"][0]["leave_undo"] is None
    assert snap(world, s2, b) == before and [r[1] for r in prices(world, s2, b)] == [300_000]
    assert [r[1] for r in prices(world, s0, b)] == [400_000] and membership(world, s0, a).is_active is False


def test_m1_the_forgiveness_cancelled_at_the_leaving_is_in_the_row_with_the_sum_only_for_the_money_right(world, fixed_today):
    from tests.test_money2_stage3_pg import recorded_forgiven_taken_out
    from tests.test_money2_wave_b_pg import _admin

    s, b, m = recorded_forgiven_taken_out(world, fixed_today)
    new = fresh_student(world, "Новый в партии")
    row = bulk(world, b, [s, new], dry_run=True)["students"][0]
    assert row["leave_undo"]["undone"] is True and row["leave_undo"]["forgiveness_cancelled"] == {"amount": 38_000}
    plain = bulk(world, b, [s, new], dry_run=True, client=_admin(world))["students"][0]["leave_undo"]
    assert plain["forgiveness_cancelled"] == {} and "38" not in plain["text"]


def test_m3_the_single_adding_with_an_individual_price_on_a_cancelled_leaving_is_refused_without_a_price_it_is_cancelled(world, fixed_today):
    a, b, c = groups3(world, fixed_today, SUNDAY)
    s2, before = returning(world, b)
    r = owner(world).post(f"{API}/org/groups/{b.id}/students/{s2.id}", params={"mode": "add"}, json={"price_override": {"amount": 250_000, "reason": "новая цена"}})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "LEAVE_UNDO_NO_START" and r.json()["detail"]["message"] == NEW_TEXT, r.text
    world.db.expire_all()
    assert membership(world, s2, b).is_active is False and [r[1] for r in prices(world, s2, b) if r[1] is not None] == [300_000]      # the marker of the leaving (no amount) is there: the leaving is NOT cancelled
    assert add(world, s2, group=b, mode="add")["leave_undo"]["undone"] is True and snap(world, s2, b) == before


def test_m4_the_balance_row_of_this_group_says_same_group_and_the_rows_of_other_groups_do_not(world, fixed_today):
    from tests.test_money2_return_window_pg import _left_with_a_lesson, _window

    b, s = _left_with_a_lesson(world, fixed_today)                                               # B: left on Tue with a debt of 38 000
    (row,) = _window(world, b, s)["past_balances"]
    assert row["same_group"] is True and row["amount"] == 38_000
    from src.models.auth import Group

    gamma = world.db.query(Group).filter(Group.org_id == world.org.id, Group.name == "Гамма").one()
    other = _window(world, gamma, s)["past_balances"]
    assert [r["group_id"] for r in other] == [str(b.id)] and "same_group" not in other[0]
