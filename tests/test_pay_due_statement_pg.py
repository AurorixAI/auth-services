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
    out = {"due": 0, "debt": 0, "advance": 0, "paid": 0, "forgiven": 0}
    for row in st["rows"]:
        for part in row["open_parts"]:       # decision 6: a row with two names has two parts
            out[part["name"]] += part["amount"]
        assert (row["open_amount"] or 0) == sum(x["amount"] for x in row["open_parts"])
    return out


def check_equalities(st):
    """The four equalities of the lead: the open sums of the rows are the cards of the summary; cost - paid - forgiven + refunded (- what moved in, + what moved out, - deductions) = debt + due - advance."""
    s, o = st["summary"], open_sums(st)
    assert o["due"] == s["due_amount"] and o["debt"] == s["debt_amount"] and o["advance"] == s["advance"], (o, s)
    assert s["charged"] - s["paid"] - s["transferred_in"] - s["written_off"] - s["recalculated"] + s["refunded"] + s["transferred_out"] == s["debt_amount"] + s["due_amount"] - s["advance"]
    assert s["unpaid"] == s["debt_amount"] + s["due_amount"]


def test_a_partial_payment_the_charge_row_says_what_is_left_and_the_payment_row_nothing(pg_session, clock):
    w, g, st, ms = world(pg_session, clock)
    pay(w, ms[2], 200_000, D(2026, 10, 12))
    s = statement(w, ms[2])
    charge = [r for r in s["rows"] if r["type"] == "charge"][0]
    payment = [r for r in s["rows"] if r["type"] == "payment"][0]
    assert (charge["open_name"], charge["open_amount"], charge["open_text"]) == ("due", 400_000, f"К оплате 400{NB}000{NB}сум")
    assert (payment["open_name"], payment["open_amount"], payment["open_text"]) == (None, None, None)       # all of it is laid on the charge
    check_equalities(s)
    assert open_sums(s) == {"due": 400_000, "debt": 0, "advance": 0, "paid": 0, "forgiven": 0}


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


def test_the_money_that_came_by_a_transfer_into_the_group_is_an_advance_on_its_own_row_until_it_is_laid_on_a_charge(pg_session, clock):
    """Decision 2 of the lead (20.11): a row that brought money - a payment AND a «перенос в группу» - says «Аванс N» on the part not yet laid on a charge; the transfer OUT, a refund and a forgiveness stay empty."""
    from tests.money1b_helpers import add
    from tests.test_pay2_api_pg import membership

    w, g, st, ms = world(pg_session, clock, joins=(D(2026, 10, 1),))
    from tests.ops_helpers import new_group
    from tests.payf_helpers import slots
    from tests.pg_world import give_group_price

    other = new_group(w.db, w.org, w.ids, "G1", "Вторая", w.A, slots())
    give_group_price(w.db, other, 600_000, valid_from=D(2026, 1, 1))
    w.db.commit()
    pay(w, ms[0], 2_400_000, D(2026, 10, 12))                       # far ahead: 4 months of 600 000 on the account of the first group
    r = client(w, "owner").post(f"{API}/org/groups/{other.id}/students/bulk", params={"mode": "transfer"}, json={"student_ids": [str(st[0].id)], "from_group_id": str(g.id)})
    assert r.status_code == 200, r.text
    moved = membership(w, st[0], other)
    s = statement(w, moved)
    rows = {x["type"]: x for x in s["rows"]}
    assert "transfer_in" in rows, [x["type"] for x in s["rows"]]
    came = rows["transfer_in"]
    assert came["open_name"] == "advance" and came["open_amount"] == s["summary"]["advance"] > 0
    check_equalities(s)
    old = statement(w, membership(w, st[0], g))
    out = [x for x in old["rows"] if x["type"] == "transfer_out"]
    assert out and all(x["open_name"] is None and x["open_amount"] is None for x in out)


def test_16b1_the_term_of_the_row_open_sub_in_the_statement_and_in_the_same_cell_of_both_files(pg_session, clock):
    """PAY-DUE 16Б.1.  Term 5 days, «Свой месяц», today Mon 12.10: A (joined 01.10) and B (06.10) are past the term, C (09.10) pays until 13.10 - tomorrow."""
    import csv
    import io
    from openpyxl import load_workbook

    w, g, st, ms = world(pg_session, clock)
    def charge(m):
        return [r for r in statement(w, m)["rows"] if r["type"] == "charge"][0]
    a, b, c = charge(ms[0]), charge(ms[1]), charge(ms[2])
    assert (a["open_name"], a["open_sub"]) == ("debt", "срок был 05.10")            # first day 01.10 + 5 days (the 1st counts): 05.10
    assert (b["open_name"], b["open_sub"]) == ("debt", "срок был 10.10")
    assert (c["open_name"], c["open_sub"]) == ("due", "до завтра, 13.10")
    pay(w, ms[1], 700_000, D(2026, 10, 12))                                          # B pays: the row is «Оплачено», the payment row says the advance - neither has a term
    s = statement(w, ms[1])
    assert [r["open_sub"] for r in s["rows"]] == [None] * len(s["rows"])
    cell = f"Долг 600{NB}000{NB}сум · срок был 05.10"                                # the same words in the cell «Остаток» of the files
    csv_rows = list(csv.reader(io.StringIO(client(w, "owner").get(f"{API}/org/memberships/{ms[0].id}/statement/export", params={"format": "csv"}).content.decode("utf-8-sig")), delimiter=";"))
    assert cell.replace(NB, " ") in [row[6] for row in csv_rows if len(row) > 6]       # the csv writer turns the non-breaking spaces into plain ones (as before)
    book = load_workbook(io.BytesIO(client(w, "owner").get(f"{API}/org/memberships/{ms[2].id}/statement/export", params={"format": "xlsx"}).content))
    cells = [str(x.value).replace(NB, " ") for sheet in book for row in sheet.iter_rows() for x in row if x.value]       # the file writer turns the non-breaking spaces into plain ones (as the csv)
    assert "К оплате 600 000 сум · до завтра, 13.10" in cells


def test_16b1_the_term_of_a_package_debt_is_the_last_day_of_its_oldest_lesson_and_none_when_the_student_left_before_the_term():
    from datetime import date
    from src.auth.pay_due import _package_parts, DEBT

    lessons = [(date(2026, 10, 1), 100, "p"), (date(2026, 10, 5), 100, "p"), (date(2026, 10, 9), 100, "p")]        # M = 1: only the newest is «к оплате»
    parts = {p.name: p for p in _package_parts(lessons, {"p": False}, date(2026, 10, 12), 1, True)}
    assert (parts[DEBT].lessons, parts[DEBT].lapsed, parts[DEBT].term) == (2, date(2026, 10, 4), None)        # the lesson of 05.10 pushed the one of 01.10 out on 04.10
    assert parts["due"].lapsed is None and parts["due"].due_until == date(2026, 10, 23)


def test_17_1_the_recalculation_row_carries_the_advance_it_freed_and_the_equalities_hold(pg_session, clock):
    """A month is paid (600 000), the centre cancels the lesson of Wed 21.10 (the recalculation is on): 46 000 is taken off the month and lies as an advance - the summary says «Аванс 46 000» and so does the row of the recalculation."""
    from tests.payf_helpers import build_org, cancel
    from tests.test_payf_recalc_pg import month_world

    w = build_org(pg_session)
    g, student, m = month_world(w, clock, paid=600_000)
    cancel(w, g, "lesson-wed", "2026-10-21")
    s = statement(w, m)
    (recalc,) = [r for r in s["rows"] if r["type"] == "recalc"]
    # R4: the deduction takes its place INSIDE the month (money 554 000 + deduction 46 000), the 46 000 of the payment it pushes out are MONEY that lies ahead: the advance is carried by the row of the PAYMENT, the
    # row of the deduction has none (before R4 the payment was "fully laid" and the deduction travelled on as the advance).  The summary and the equalities are the same.
    assert (recalc["open_name"], recalc["open_amount"], recalc["open_text"]) == (None, None, None)
    (payment,) = [r for r in s["rows"] if r["type"] == "payment"]
    assert (payment["open_name"], payment["open_amount"], payment["open_text"]) == ("advance", 46_000, f"Аванс 46{NB}000{NB}сум")
    assert s["summary"]["advance"] == 46_000
    check_equalities(s)
    assert open_sums(s)["advance"] == 46_000


def test_17_7_a_charge_forgiven_whole_is_forgiven_and_not_paid_a_charge_paid_in_money_is_paid(pg_session, clock):
    w, g, st, ms = world(pg_session, clock)
    r = client(w, "owner").post(f"{API}/org/memberships/{ms[0].id}/writeoffs", json={"all": True, "reason": "Льгота"})
    assert r.status_code == 201, r.text
    s = statement(w, ms[0])
    (charge,) = [x for x in s["rows"] if x["type"] == "charge"]
    assert (charge["open_name"], charge["open_amount"], charge["open_text"], charge["open_sub"]) == ("forgiven", 0, "Прощено", None)
    assert [p["name"] for p in charge["open_parts"]] == ["forgiven"]
    check_equalities(s)
    assert s["summary"]["written_off"] == 600_000 and s["summary"]["unpaid"] == 0
    pay(w, ms[1], 600_000, D(2026, 10, 12))
    (paid,) = [x for x in statement(w, ms[1])["rows"] if x["type"] == "charge"]
    assert paid["open_name"] == "paid" and paid["open_text"] == "Оплачено"
