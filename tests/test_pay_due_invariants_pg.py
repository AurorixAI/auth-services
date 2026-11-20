"""PAY-DUE 17.12–17.16 (the tests the check of 20.11 found missing; PostgreSQL, through the routes of the product, numbers by hand, the clock is moved explicitly).

* 17.12  the two equalities of the names over several worlds;
* 17.13  К2: one name and one set of sums for a student of two groups on every road that shows them;
* 17.14  В8: «убрать и вернуть» does not move the term (a personal period and a shared month);
* 17.15  packages: «урок + 14 дней», 23:50 -> 00:10;
* 17.16  the snapshot of the money for a SECOND world (packages, M = 0..4) with the allocations of the payments, the findings of the reconciliation and the cash;
* 17.17  the Excel file is built FROM THE ANSWER OF THE SERVER and holds the equalities of 10.11."""
import io
from datetime import date

import pytest

from tests.money1b_helpers import account, add, fresh_student, hold_lessons
from tests.ops_helpers import new_group
from tests.payf_helpers import slots
from tests.pay_due_helpers import API, D, client, pay, put_settings, status_page, summary, world, clock, pg_session, needs_pg  # noqa: F401  (fixtures)
from tests.pg_world import give_group_price

pytestmark = needs_pg
NB = "\xa0"


def rows_of(page):
    return {r["membership_id"]: r for r in page["items"]}


def sum_of_ledgers(w, ms):
    return sum(account(w, m.id).debt for m in ms)


def assert_the_two_equalities(w, ms):
    page = status_page(w, "all")
    for r in page["items"]:
        assert r["due_amount"] == r["unpaid"] - r["debt_amount"], r                                   # the name «к оплате» is the rest of the unpaid sum, not a sum of its own
        assert 0 <= r["debt_amount"] <= r["unpaid"]
    s = summary(w)
    assert s["debt_total"] + s["due_total"] == sum_of_ledgers(w, ms), s                               # the two cards are the whole unpaid sum of the centre


def package_world(pg_session, clock, **block):
    w, g, st, ms = world(pg_session, clock, joins=(D(2026, 10, 1),), period_mode="lessons", lessons_per_payment=8, **block)
    hold_lessons(w, st[0], [D(2026, 10, 2), D(2026, 10, 5), D(2026, 10, 7)], group=g)
    return w, g, st, ms


@pytest.mark.parametrize("days,lessons", [(0, 0), (5, 2), (14, 4)])
def test_17_12_the_equalities_hold_in_the_calendar_world_for_every_term(pg_session, clock, days, lessons):
    w, g, st, ms = world(pg_session, clock)
    pay(w, ms[1], 250_000, D(2026, 10, 7))
    put_settings(w, pay_due_days=days, pay_due_lessons=lessons)
    assert_the_two_equalities(w, ms)


@pytest.mark.parametrize("lessons", [0, 1, 2, 3, 4])
def test_17_12_the_equalities_hold_in_the_package_world_for_every_m(pg_session, clock, lessons):
    w, g, st, ms = package_world(pg_session, clock)
    put_settings(w, pay_due_lessons=lessons)
    assert_the_two_equalities(w, ms)
    pay(w, ms[0], 75_000, D(2026, 10, 12))                                                           # one lesson paid: the oldest
    assert_the_two_equalities(w, ms)


def test_17_12_the_equalities_hold_after_a_forgiveness_a_refund_and_a_leaving(pg_session, clock):
    w, g, st, ms = world(pg_session, clock)
    owner = client(w, "owner")
    assert owner.post(f"{API}/org/memberships/{ms[0].id}/writeoffs", json={"amount": 100_000, "reason": "Договорённость"}).status_code == 201
    pay(w, ms[2], 700_000, D(2026, 10, 12))
    assert owner.post(f"{API}/org/groups/{g.id}/students/{st[1].id}/leave", json={}).status_code == 200
    assert_the_two_equalities(w, ms)


def test_17_13_k2_a_student_of_two_groups_has_one_name_and_the_same_sums_on_every_road(pg_session, clock):
    """C joined 09.10 (term 13.10: «к оплате»), B and A are debtors; the student of two groups is added to the second group on the same day: both rows have the same name, whatever road shows them."""
    w, g, st, ms = world(pg_session, clock, joins=(D(2026, 10, 9),))
    other = new_group(w.db, w.org, w.ids, "G1", "Вторая", w.A, slots())
    give_group_price(w.db, other, 400_000, valid_from=D(2026, 1, 1))
    w.db.commit()
    add(w, st[0], group=other, mode="add", pay_from="2026-10-09")
    owner = client(w, "owner")
    by_group = {r["group_id"]: r for r in status_page(w, "all")["items"] if r["student_id"] == str(st[0].id)}
    assert len(by_group) == 2
    names = {(r["state"], r["debt_amount"] > 0, r["due_amount"] > 0) for r in by_group.values()}
    assert names == {("to_pay", False, True)}, by_group                                               # both rows are «к оплате»: the student is in the centre, in both groups
    for group_id, row in by_group.items():
        mine = status_page(w, "to_pay", group_id=group_id)
        (same,) = [r for r in mine["items"] if r["student_id"] == str(st[0].id)]
        assert (same["state"], same["debt_amount"], same["due_amount"], same["due_until"]) == (row["state"], row["debt_amount"], row["due_amount"], row["due_until"])
        assert mine["students"] == len(mine["items"]) == 1 and mine["amount_total"] == row["due_amount"]
        found = status_page(w, "all", search="Ученик0", group_id=group_id)["items"][0]
        assert (found["state"], found["due_amount"], found["due_until"]) == (row["state"], row["due_amount"], row["due_until"])
    card = {r["group_id"]: r for r in owner.get(f"{API}/org/students/{st[0].id}/memberships").json()}
    for group_id, row in by_group.items():
        assert (card[group_id]["unpaid"], card[group_id].get("state")) == (row["unpaid"], row.get("state"))
        statement = owner.get(f"{API}/org/memberships/{row['membership_id']}/statement").json()["summary"]
        assert (statement["debt_amount"], statement["due_amount"], statement["due_until"]) == (row["debt_amount"], row["due_amount"], row["due_until"])
    total = summary(w)
    assert total["due_students"] == 1 and total["debtors"] == 0 and total["due_total"] == sum(r["due_amount"] for r in by_group.values())


@pytest.mark.parametrize("block", [{}, {"billing_anchor": "fixed_day", "billing_day": 1}], ids=["personal", "shared"])
def test_17_14_v8_taking_a_student_off_and_back_does_not_move_the_term(pg_session, clock, block):
    """В8.  The term of the period is the 5th day from its first charged day; to take the student off and put him back on the 4th day must not give a fresh term."""
    w, g, st, ms = world(pg_session, clock, today="2026-10-12", joins=(D(2026, 10, 9),), **block)
    before = status_page(w, "all")["items"][0]
    assert before["state"] in ("to_pay", "debt") and before["due_until"] is not None or before["state"] == "debt"
    owner = client(w, "owner")
    assert owner.post(f"{API}/org/groups/{g.id}/students/{st[0].id}/leave", json={}).status_code == 200
    add(w, st[0], group=g, mode="add")
    after = [r for r in status_page(w, "all", scope="all")["items"] if r["student_id"] == str(st[0].id)]
    assert {(r["debt_amount"], r["due_amount"], r["due_until"]) for r in after} == {(before["debt_amount"], before["due_amount"], before["due_until"])}, (before, after)


def test_17_15_packages_the_lesson_plus_14_days_at_23_50_and_00_10(pg_session, clock):
    """M = 4: the three lessons (02.10, 05.10, 07.10) are all «к оплате» while the oldest is not older than 14 days: until Fri 16.10 inclusive; at 23:50 it still is, at 00:10 of 17.10 it is a debt."""
    w, g, st, ms = package_world(pg_session, clock)
    put_settings(w, pay_due_lessons=4)
    clock.set("2026-10-16 23:50")
    a = rows_of(status_page(w, "all"))[str(ms[0].id)]
    assert (a["debt_amount"], a["due_amount"], a["due_until"]) == (0, 225_000, "2026-10-16") and a["due_sub"] == "до сегодня"
    clock.set("2026-10-17 00:10")
    a = rows_of(status_page(w, "all"))[str(ms[0].id)]
    assert (a["debt_amount"], a["due_amount"], a["due_until"]) == (75_000, 150_000, "2026-10-19")
    assert a["overdue_days"] == 1 and a["debt_sub"] == "просрочено 1 день"


def test_17_16_the_snapshot_of_the_money_in_the_package_world_with_allocations_findings_and_cash(pg_session, clock):
    from tests.money1_helpers import changed_tables, db_fingerprint
    from src.auth import money_reconcile as mr

    w, g, st, ms = package_world(pg_session, clock)
    pay(w, ms[0], 100_000, D(2026, 10, 8))
    owner = client(w, "owner")

    def snapshot():
        w.db.expire_all()
        statement = owner.get(f"{API}/org/memberships/{ms[0].id}/statement").json()
        allocations = [[(a["label"], a["amount"], a["bucket"]) for a in r["details"]["allocations"]] for r in statement["rows"] if r["type"] == "payment"]
        rows = [(r["date"], r["type"], r["debit"], r["credit"], r["outflow"], r["balance_after"]) for r in statement["rows"]]
        ledger = account(w, ms[0].id)
        findings = [mr.check_identity(w.org.id, ms[0].id, ledger), mr.check_statement(w.org.id, ms[0].id, ledger)]
        income = owner.get(f"{API}/org/payments/income", params={"from": "2026-10-01", "to": "2026-10-31"}).json()
        cash = {k: income[k] for k in ("current", "by_method", "strip", "cash", "checks", "ok", "refunded_total")}        # the money of the period; «collection» (the debt of the month by name) is the one thing that may move
        status = {k: v for k, v in status_page(w, "all")["items"][0].items() if k in ("unpaid", "advance", "paid_until", "partial_credit", "refundable")}
        return allocations, rows, findings, cash, status

    base = snapshot()
    assert base[0] and base[3], "the payment and the cash must exist"
    assert base[2] == [None, None], "the reconciliation finds nothing in this world"
    money_tables = ("student_payments", "student_writeoffs", "student_refunds", "payment_receipt_snapshots", "money_reconcile_findings")
    before = db_fingerprint(w.db)
    for lessons in range(0, 5):
        put_settings(w, pay_due_lessons=lessons)
        assert snapshot() == base, lessons
    for days in (0, 3, 14):
        put_settings(w, pay_due_days=days)
        assert snapshot() == base, days
    assert not set(changed_tables(before, db_fingerprint(w.db))) & set(money_tables)


def test_17_17_the_excel_is_built_from_the_answer_of_the_server_and_holds_the_equalities_of_10_11(pg_session, clock):
    """The golden xlsx tests check a statement put together by hand; here the file comes from the route (a debt, a part «к оплате», a payment, a forgiveness) and the numbers of its column «Остаток» are the cards of the summary."""
    import re
    from openpyxl import load_workbook

    w, g, st, ms = world(pg_session, clock, today="2026-11-01")                                    # A: October is a debt, November «к оплате»
    owner = client(w, "owner")
    assert owner.post(f"{API}/org/memberships/{ms[0].id}/writeoffs", json={"amount": 100_000, "reason": "Договорённость"}).status_code == 201
    pay(w, ms[0], 100_000, D(2026, 11, 1))
    answer = owner.get(f"{API}/org/memberships/{ms[0].id}/statement").json()
    book = load_workbook(io.BytesIO(owner.get(f"{API}/org/memberships/{ms[0].id}/statement/export", params={"format": "xlsx"}).content))
    cells = [str(x.value).replace(NB, " ") for sheet in book for row in sheet.iter_rows() for x in row if x.value is not None]
    s = answer["summary"]

    def number(text):
        return int(re.sub(r"\D", "", text.split(" · ")[0]))

    debt = sum(number(c) for c in cells if re.fullmatch(r"Долг [\d ]+ сум(?: · срок был \d\d\.\d\d)?", c))
    due = sum(number(c) for c in cells if re.fullmatch(r"К оплате [\d ]+ сум(?: · до [^·]+)?", c))
    assert (debt, due) == (s["debt_amount"], s["due_amount"]), (cells, s)                              # the cells of the rows of the file add up to the cards of the summary
    assert s["charged"] - s["paid"] - s["transferred_in"] - s["written_off"] - s["recalculated"] + s["refunded"] + s["transferred_out"] == s["debt_amount"] + s["due_amount"] - s["advance"]
    tile = f"Остаток: Долг {s['debt_amount']:,} сум · К оплате {s['due_amount']:,} сум".replace(",", " ")
    assert any(tile in c for c in cells), (tile, cells)                                              # the tile of the file says the same two names with the same sums
