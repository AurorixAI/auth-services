"""LOGIC-A (lead's addition to the contract): the statement says what is left of EACH period (``segment_remaining_after`` of every
allocation line, ``paid`` / ``written_off`` / ``remaining`` of every charge) and links a correction to the payment it replaced
(``replaces_receipt_no`` / ``replaces_amount`` / ``replaced_by_receipt_no``).  E1-like data with a write-off between two payments
and a correction."""
from datetime import date

from tests.pg_world import pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import (  # noqa: F401  (helpers)
    API, D, TODAY, e1_world, fixed_today, owner, post_payment, pytestmark,
)


def _world(world):
    m = e1_world(world)  # payment_date, 500 000, joined 15.09, 231 000 paid on 20.09 (receipt 17), today 21.10
    c = owner(world)
    r = c.post(f"{API}/org/memberships/{m.id}/writeoffs", json={"amount": 100_000, "reason": "болезнь", "effective_on": "2026-10-05"})
    assert r.status_code == 201, r.text
    p = post_payment(world, m, amount=300_000, paid_on="2026-10-10")["payment"]
    return m, p


def _statement(world, m, **params):
    r = owner(world).get(f"{API}/org/memberships/{m.id}/statement", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_remaining_after_every_allocation_and_per_charge_totals(world, fixed_today):
    m, p = _world(world)
    st = _statement(world, m)
    charges = [r["details"]["segment"] for r in st["rows"] if r["type"] == "charge"]
    seg1, seg2 = charges[0], charges[1]
    # period 1: 231 000 + 169 000 of the second payment paid, 100 000 written off between them -> closed
    assert (seg1["charge"], seg1["paid"], seg1["written_off"], seg1["remaining"]) == (500_000, 400_000, 100_000, 0)
    assert seg1["remaining"] == seg1["unpaid"]
    # period 2: the rest of the second payment
    assert (seg2["charge"], seg2["paid"], seg2["written_off"], seg2["remaining"]) == (500_000, 131_000, 0, 369_000)
    pays = [r for r in st["rows"] if r["type"] == "payment"]
    first, second = pays[0]["details"]["allocations"], pays[1]["details"]["allocations"]
    assert [(a["amount"], a["segment_remaining_after"]) for a in first] == [(231_000, 269_000)]    # before the write-off
    assert [(a["amount"], a["segment_remaining_after"]) for a in second] == [(169_000, 0), (131_000, 369_000)]
    # the same numbers on the payment itself and on the receipt snapshot
    pay = owner(world).get(f"{API}/org/memberships/{m.id}/payments").json()
    mine = next(x for x in pay if x["id"] == p["id"])
    assert [a["segment_remaining_after"] for a in mine["allocation"]["items"]] == [0, 369_000]
    rc = owner(world).get(f"{API}/org/payments/{p['id']}/receipt").json()
    assert [a["segment_remaining_after"] for a in rc["allocations"]] == [0, 369_000]


def test_correction_links_both_ways_and_remaining_follows(world, fixed_today):
    m, p = _world(world)
    r = owner(world).post(f"{API}/org/payments/{p['id']}/replace", json={
        "amount": 250_000, "paid_on": "2026-10-10", "method": "cash", "reason": "Неверная сумма"})
    assert r.status_code == 201, r.text
    new = r.json()["payment"]
    st = _statement(world, m, include_voided=True)
    live = [x for x in st["rows"] if x["type"] == "payment" and x["details"]["payment_id"] == new["id"]]
    assert len(live) == 1
    d = live[0]["details"]
    assert (d["replaces_receipt_no"], d["replaces_amount"], d["replaced_by_receipt_no"]) == (p["receipt_no"], 300_000, None)
    assert [(a["amount"], a["segment_remaining_after"]) for a in d["allocations"]] == [(169_000, 0), (81_000, 419_000)]
    old = [x for x in st["voided"] if x["details"]["payment_id"] == p["id"]]
    assert len(old) == 1 and old[0]["details"]["replaced_by_receipt_no"] == new["receipt_no"]
    assert old[0]["details"]["replaces_receipt_no"] is None
    seg2 = [x["details"]["segment"] for x in st["rows"] if x["type"] == "charge"][1]
    assert (seg2["paid"], seg2["remaining"]) == (81_000, 419_000)
    rc = owner(world).get(f"{API}/org/payments/{new['id']}/receipt").json()
    assert (rc["replaces_receipt_no"], rc["replaces_amount"]) == (p["receipt_no"], 300_000)
    rc_old = owner(world).get(f"{API}/org/payments/{p['id']}/receipt").json()
    assert rc_old["replaced_by_receipt_no"] == new["receipt_no"] and rc_old["replaces_amount"] is None


def test_remaining_after_is_none_on_the_advance_line(world, fixed_today):
    m, _ = _world(world)
    post_payment(world, m, amount=900_000)
    st = _statement(world, m)
    last = [x for x in st["rows"] if x["type"] == "payment"][-1]["details"]["allocations"]
    assert last[-1]["segment_id"] != "advance" or last[-1]["segment_remaining_after"] is None
    assert all(a["segment_remaining_after"] is not None for a in last if a["segment_id"] != "advance")
