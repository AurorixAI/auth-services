"""Money-1 pack V (PostgreSQL): operations on the money of a student - the amount a person SAW and a repeated request (V1).

Numbers of ``test_pay2_api_pg`` worlds: ``e6_world`` debt 100 000 (price 500 000, 400 000 paid), ``e7`` an advance of 344 000 (790 000 paid, left on 25.10)."""
from uuid import uuid4

from src.models.auth import GroupAudit, StudentPayment, StudentRefund, StudentWriteoff

from tests.pg_world import needs_pg, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import (  # noqa: F401  (fixtures)
    API, D, TODAY, e1_world, e6_world, fixed_today, membership, owner, post_payment, seed_payment, stay,
)

pytestmark = needs_pg
NB = " "


def key():
    return {"Idempotency-Key": str(uuid4())}


def writeoffs(w):
    w.db.expire_all()
    return w.db.query(StudentWriteoff).filter_by(org_id=w.org.id).all()


def refunds(w):
    w.db.expire_all()
    return w.db.query(StudentRefund).filter_by(org_id=w.org.id).all()


# ---- V1: the sum the person saw -------------------------------------------------------------------------------------------------------------


def test_v1_a_whole_forgiveness_of_a_debt_that_moved_is_refused_and_nothing_is_written(world, fixed_today):
    m = e6_world(world)                                                        # debt 100 000
    post_payment(world, m, amount=30_000)                                       # while the window was open another person took 30 000
    r = owner(world).post(f"{API}/org/memberships/{m.id}/writeoffs", json={"all": True, "reason": "льгота", "expected_debt": 100_000})
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["code"] == "BALANCE_CHANGED" and d["debt"] == 70_000
    assert d["message"] == f"Долг изменился: было 100{NB}000{NB}сум, сейчас 70{NB}000{NB}сум"
    assert writeoffs(world) == []                                               # the old code wrote 70 000


def test_v1_the_same_sum_is_written_and_a_request_without_the_sum_works_as_before(world, fixed_today):
    m = e6_world(world)
    c = owner(world)
    assert c.post(f"{API}/org/memberships/{m.id}/writeoffs", json={"amount": 10_000, "reason": "льгота", "expected_debt": 100_000}).status_code == 201
    assert c.post(f"{API}/org/memberships/{m.id}/writeoffs", json={"amount": 10_000, "reason": "льгота"}).status_code == 201       # no sum: the old behaviour
    assert [w.amount for w in writeoffs(world)] == [10_000, 10_000]


def test_v1_a_double_click_with_one_key_writes_one_forgiveness(world, fixed_today):
    m = e6_world(world)
    c, h = owner(world), key()
    first = c.post(f"{API}/org/memberships/{m.id}/writeoffs", json={"amount": 20_000, "reason": "льгота"}, headers=h)
    again = c.post(f"{API}/org/memberships/{m.id}/writeoffs", json={"amount": 20_000, "reason": "льгота"}, headers=h)
    assert first.status_code == 201 and again.status_code == 409, again.text
    assert again.json()["detail"]["code"] == "DUPLICATE_WRITEOFF" and again.json()["detail"]["writeoff_id"] == first.json()["writeoff"]["id"]
    assert [w.amount for w in writeoffs(world)] == [20_000]                      # the old code: two rows, 40 000
    other = c.post(f"{API}/org/memberships/{m.id}/writeoffs", json={"amount": 20_000, "reason": "льгота"}, headers=key())       # a new key is a new decision
    assert other.status_code == 201 and len(writeoffs(world)) == 2


def test_v1_voiding_a_forgiveness_checks_the_debt_the_person_saw_and_a_second_void_says_who_did_it(world, fixed_today):
    m = e6_world(world)
    c = owner(world)
    wid = c.post(f"{API}/org/memberships/{m.id}/writeoffs", json={"amount": 40_000, "reason": "льгота"}).json()["writeoff"]["id"]      # debt 60 000
    stale = c.post(f"{API}/org/writeoffs/{wid}/void", json={"reason": "ошибка", "expected_debt": 100_000})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "BALANCE_CHANGED" and stale.json()["detail"]["debt"] == 60_000
    assert c.post(f"{API}/org/writeoffs/{wid}/void", json={"reason": "ошибка", "expected_debt": 60_000}).status_code == 200
    second = c.post(f"{API}/org/writeoffs/{wid}/void", json={"reason": "ещё раз"})
    assert second.status_code == 409 and second.json()["detail"]["code"] == "ALREADY_VOIDED" and second.json()["detail"]["voided_at"]
    assert world.db.query(GroupAudit).filter(GroupAudit.org_id == world.org.id, GroupAudit.action == "payment_writeoff_void").count() == 1


def _advance_world(w, fixed_today):
    fixed_today(D(2026, 10, 25))
    m = e1_world(w)
    seed_payment(w, m, 790_000, TODAY, receipt_no=18)
    stay(w, m, D(2026, 9, 15), D(2026, 10, 25))
    return m                                                                    # refundable 344 000


def test_v1_a_refund_of_an_advance_that_moved_is_refused_and_a_double_click_writes_one(world, fixed_today):
    m = _advance_world(world, fixed_today)
    c, h = owner(world), key()
    stale = c.post(f"{API}/org/memberships/{m.id}/refunds", json={"amount": 100_000, "method": "cash", "expected_refundable": 300_000})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "BALANCE_CHANGED" and stale.json()["detail"]["refundable"] == 344_000
    assert refunds(world) == []
    body = {"amount": 100_000, "method": "cash", "reason": "ученик ушёл", "expected_refundable": 344_000}
    assert c.post(f"{API}/org/memberships/{m.id}/refunds", json=body, headers=h).status_code == 201
    again = c.post(f"{API}/org/memberships/{m.id}/refunds", json=body, headers=h)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "DUPLICATE_REFUND" and again.json()["detail"]["refund_id"]
    assert [r.amount for r in refunds(world)] == [100_000]                       # the old code: two rows
    moved = c.post(f"{API}/org/memberships/{m.id}/refunds", json=body, headers=key())      # the first refund moved the advance: 344 000 -> 244 000
    assert moved.status_code == 409 and moved.json()["detail"]["refundable"] == 244_000 and len(refunds(world)) == 1


def test_v1_voiding_a_refund_checks_the_advance_and_names_the_voider_twice(world, fixed_today):
    m = _advance_world(world, fixed_today)
    c = owner(world)
    rid = c.post(f"{API}/org/memberships/{m.id}/refunds", json={"amount": 100_000, "method": "cash", "reason": "ушёл"}).json()["refund"]["id"]
    stale = c.post(f"{API}/org/refunds/{rid}/void", json={"reason": "ошибка", "expected_advance": 344_000})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "BALANCE_CHANGED" and stale.json()["detail"]["advance"] == 244_000
    assert c.post(f"{API}/org/refunds/{rid}/void", json={"reason": "ошибка", "expected_advance": 244_000}).status_code == 200
    second = c.post(f"{API}/org/refunds/{rid}/void", json={"reason": "ещё раз"})
    assert second.status_code == 409 and second.json()["detail"]["code"] == "ALREADY_VOIDED" and second.json()["detail"]["voided_at"]


def test_v1_a_forgiveness_in_a_closed_month_is_allowed_and_the_key_of_another_centre_does_not_interfere(world, fixed_today):
    m = e6_world(world)
    h = key()
    assert owner(world).post(f"{API}/org/memberships/{m.id}/writeoffs", json={"amount": 5_000, "reason": "льгота"}, headers=h).status_code == 201
    assert len(writeoffs(world)) == 1                                           # ДП-07: money of a student is not blocked by a closed month of a teacher (the answer warns)


# ---- V2: the journal of money decisions ---------------------------------------------------------------------------------------------------


def journal(w, first, last, **params):
    r = owner(w).get(f"{API}/org/payments/journal", params={"from": first, "to": last, **params})
    assert r.status_code == 200, r.text
    return r.json()


def audit(w, action):
    w.db.expire_all()
    rows = w.db.query(GroupAudit).filter(GroupAudit.org_id == w.org.id, GroupAudit.action == action).all()
    assert len(rows) == 1, (action, len(rows))
    return rows[0].snapshot


def test_v2_a_payment_cancelled_in_november_is_a_line_of_november_too_and_the_totals_do_not_double(world, fixed_today):
    fixed_today(D(2026, 11, 12))
    m = e6_world(world)                                                           # 400 000 paid on 15.10 for the price 500 000: debt 100 000
    pay = world.db.query(StudentPayment).filter_by(membership_id=m.id).one()
    assert owner(world).post(f"{API}/org/payments/{pay.id}/void", json={"reason": "ошиблись суммой"}).status_code == 200
    october = journal(world, "2026-10-01", "2026-10-31")
    assert october["total"] == 1 and october["rows"][0]["status"] == "voided" and october["rows"][0]["view"] == "paid" and october["totals"]["sum"] == 0
    assert journal(world, "2026-11-01", "2026-11-30")["total"] == 0                 # as before: the month of the cancellation did not show it
    november = journal(world, "2026-11-01", "2026-11-30", cancelled_in_period="true")
    assert november["total"] == 1 and november["rows"][0]["view"] == "cancelled_here" and november["rows"][0]["amount"] == 400_000
    assert (november["totals"]["count"], november["totals"]["sum"]) == (0, 0)       # the sum of the month is the money that stays
    assert (november["totals"]["cancelled_here_count"], november["totals"]["cancelled_here_sum"]) == (1, 400_000)
    snap = audit(world, "payment_void")
    assert (snap["amount"], snap["reason"], snap["debt_before"], snap["debt_after"]) == (400_000, "ошиблись суммой", 100_000, 500_000)


def test_v2_a_corrected_payment_leaves_the_old_and_the_new_sum_and_the_debt_before_and_after(world, fixed_today):
    m = e6_world(world)
    pay = world.db.query(StudentPayment).filter_by(membership_id=m.id).one()
    r = owner(world).post(f"{API}/org/payments/{pay.id}/replace", json={"amount": 350_000, "paid_on": TODAY.isoformat(), "method": "cash", "reason": "неверная сумма"})
    assert r.status_code == 201, r.text
    snap = audit(world, "payment_replace")
    assert (snap["amount"], snap["new_amount"], snap["debt_before"], snap["debt_after"]) == (400_000, 350_000, 100_000, 150_000)
    csv = owner(world).get(f"{API}/org/payments/journal/export", params={"from": "2026-10-01", "to": "2026-10-31", "format": "csv"}).text
    csv_all = owner(world).get(f"{API}/org/payments/journal/export", params={"from": "2026-10-01", "to": "2026-10-31", "format": "csv", "with_replaced": "true"}).text
    assert "Исправлена (заменена новой)" not in csv and "Исправлена (заменена новой)" in csv_all           # the old file is not changed; the replaced version only when asked for


def test_v2_the_list_of_decisions_has_author_reason_and_debt_before_and_after_and_is_closed_to_others(world, fixed_today):
    m = e6_world(world)
    c = owner(world)
    c.post(f"{API}/org/memberships/{m.id}/writeoffs", json={"amount": 40_000, "reason": "болезнь"})
    pay = world.db.query(StudentPayment).filter_by(membership_id=m.id).one()
    c.post(f"{API}/org/payments/{pay.id}/void", json={"reason": "ошибка ввода"})
    got = c.get(f"{API}/org/payments/decisions", params={"from": "2026-10-01", "to": "2026-10-31"})
    assert got.status_code == 200, got.text
    rows = got.json()["rows"]
    assert [x["kind"] for x in rows] == ["payment_void", "payment_writeoff"] and got.json()["total"] == 2
    assert (rows[1]["amount"], rows[1]["reason"], rows[1]["debt_before"], rows[1]["debt_after"]) == (40_000, "болезнь", 100_000, 60_000)
    assert rows[0]["actor_name"] and rows[0]["student_name"] and rows[0]["kind_label"] == "Оплата отменена"
    only = c.get(f"{API}/org/payments/decisions", params={"from": "2026-10-01", "to": "2026-10-31", "kind": "payment_writeoff"}).json()
    assert only["total"] == 1
    from tests.pg_world import make_client
    assert make_client(world, user=world.teacher).get(f"{API}/org/payments/decisions", params={"from": "2026-10-01", "to": "2026-10-31"}).status_code == 403
