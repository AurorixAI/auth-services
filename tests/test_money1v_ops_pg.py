"""Money-1 pack V (PostgreSQL): operations on the money of a student - the amount a person SAW and a repeated request (V1).

Numbers of ``test_pay2_api_pg`` worlds: ``e6_world`` debt 100 000 (price 500 000, 400 000 paid), ``e7`` an advance of 344 000 (790 000 paid, left on 25.10)."""
from uuid import uuid4

from src.models.auth import GroupAudit, StudentRefund, StudentWriteoff

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
