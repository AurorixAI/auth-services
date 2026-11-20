"""PAY-DUE 10.11 (decided by the lead 20.11): the column «Остаток» of the statement says what is open on the ROW itself, not a running total (PostgreSQL, through the routes of the product).

World as in ``test_pay_due_contract_pg``: Mon 12.10.2026, «Свой месяц», 600 000 each, term 5 days: A joined 01.10 (debt), B joined 06.10 (debt), C joined 09.10 («К оплате» until 13.10)."""
from tests.pay_due_helpers import API, D, client, pay, world, clock, pg_session, needs_pg  # noqa: F401  (fixtures)

pytestmark = needs_pg
NB = "\xa0"


def statement(w, m):
    r = client(w, "owner").get(f"{API}/org/memberships/{m.id}/statement")
    assert r.status_code == 200, r.text
    return r.json()


def open_sums(st):
    out = {"due": 0, "debt": 0, "advance": 0, "paid": 0}
    for row in st["rows"]:
        if row["open_name"]:
            out[row["open_name"]] += row["open_amount"]
    return out


def check_equalities(st):
    """The four equalities of the lead: the open sums of the rows are the cards of the summary; cost - paid - forgiven + refunded (- what moved in, + what moved out, - deductions) = debt + due - advance."""
    s, o = st["summary"], open_sums(st)
    assert o["due"] == s["due_amount"] and o["debt"] == s["debt_amount"] and o["advance"] == s["advance"], (o, s)
    assert s["charged"] - s["paid"] - s["transferred_in"] - s["written_off"] - s["recalculated"] + s["refunded"] + s["transferred_out"] == s["debt_amount"] + s["due_amount"] - s["advance"]
    assert s["debt"] == s["debt_amount"] + s["due_amount"]


def test_a_partial_payment_the_charge_row_says_what_is_left_and_the_payment_row_nothing(pg_session, clock):
    w, g, st, ms = world(pg_session, clock)
    pay(w, ms[2], 200_000, D(2026, 10, 12))
    s = statement(w, ms[2])
    charge = [r for r in s["rows"] if r["type"] == "charge"][0]
    payment = [r for r in s["rows"] if r["type"] == "payment"][0]
    assert (charge["open_name"], charge["open_amount"], charge["open_text"]) == ("due", 400_000, f"К оплате 400{NB}000{NB}сум")
    assert (payment["open_name"], payment["open_amount"], payment["open_text"]) == (None, None, None)       # all of it is laid on the charge
    check_equalities(s)
    assert open_sums(s) == {"due": 400_000, "debt": 0, "advance": 0, "paid": 0}


def test_an_advance_the_payment_row_says_advance_and_the_paid_charge_says_paid(pg_session, clock):
    w, g, st, ms = world(pg_session, clock)
    pay(w, ms[1], 700_000, D(2026, 10, 12))                       # 600 000 for the period, 100 000 ahead
    s = statement(w, ms[1])
    charge = [r for r in s["rows"] if r["type"] == "charge"][0]
    payment = [r for r in s["rows"] if r["type"] == "payment"][0]
    assert (charge["open_name"], charge["open_amount"], charge["open_text"]) == ("paid", 0, "Оплачено")
    assert (payment["open_name"], payment["open_amount"], payment["open_text"]) == ("advance", 100_000, f"Аванс 100{NB}000{NB}сум")
    check_equalities(s)
    assert s["summary"]["advance"] == 100_000 and s["summary"]["state"] == "paid"


def test_a_debt_and_a_sum_within_the_term_two_rows_two_names_and_two_state_texts(pg_session, clock):
    w, g, st, ms = world(pg_session, clock, today="2026-11-01")
    s = statement(w, ms[0])
    names = [(r["open_name"], r["open_amount"]) for r in s["rows"] if r["type"] == "charge"]
    assert names == [("debt", 600_000), ("due", 600_000)]
    assert (s["summary"]["debt_text"], s["summary"]["due_text"], s["summary"]["due_until"]) == (f"Долг 600{NB}000{NB}сум", f"К оплате 600{NB}000{NB}сум", "2026-11-05")
    check_equalities(s)


def test_a_forgiveness_row_is_empty_and_the_debt_row_shows_the_rest(pg_session, clock):
    w, g, st, ms = world(pg_session, clock)
    r = client(w, "owner").post(f"{API}/org/memberships/{ms[0].id}/writeoffs", json={"amount": 100_000, "reason": "Договорённость"})
    assert r.status_code == 201, r.text
    s = statement(w, ms[0])
    forgiven = [x for x in s["rows"] if x["type"] == "writeoff"][0]
    assert (forgiven["open_name"], forgiven["open_amount"]) == (None, None)
    assert [(x["open_name"], x["open_amount"]) for x in s["rows"] if x["type"] == "charge"] == [("debt", 500_000)]
    check_equalities(s)


def test_a_refund_of_an_advance_leaves_the_rest_of_the_advance_on_the_payment_row(pg_session, clock):
    w, g, st, ms = world(pg_session, clock)
    pay(w, ms[1], 700_000, D(2026, 10, 12))
    r = client(w, "owner").post(f"{API}/org/memberships/{ms[1].id}/refunds", json={"amount": 40_000, "method": "cash", "reason": "Просьба родителей"})
    assert r.status_code == 201, r.text
    s = statement(w, ms[1])
    payment = [x for x in s["rows"] if x["type"] == "payment"][0]
    refund = [x for x in s["rows"] if x["type"] == "refund"][0]
    assert (payment["open_name"], payment["open_amount"]) == ("advance", 60_000) and refund["open_name"] is None
    check_equalities(s)
