"""PAY-DUE П1, пункты 1-2: the contract of ONE meaning of the unpaid sum (PostgreSQL, through the routes of the product), numbers worked out by hand.

Mon 12.10.2026, «Свой месяц», 600 000 each, nobody paid, term 5 days: A joined 01.10 (D 05.10: a debt, 7 days overdue), B joined 06.10 (D 10.10: a debt, 2 days), C joined 09.10 (D 13.10: «К оплате», до завтра).
On Sun 01.11 A's second period (01.11-30.11) has begun: A owes a debt (October) AND a sum within its term (until 05.11): both tabs."""
from datetime import date, datetime
from decimal import Decimal

from src.models.auth import PayrollRate

from tests.ops_helpers import history_row
from tests.pay_due_helpers import API, D, client, dry_run, pay, put_settings, status_page, summary, world, clock, pg_session, needs_pg  # noqa: F401  (fixtures)

pytestmark = needs_pg


def test_the_summary_names_the_two_parts_and_they_add_up_to_the_unpaid_sum(pg_session, clock):
    w, g, st, ms = world(pg_session, clock)
    s = summary(w)
    assert (s["debt_total"], s["debtors"], s["due_total"], s["due_students"]) == (1_200_000, 2, 600_000, 1)
    assert s["due_nearest"] == {"date": "2026-10-13", "text": "завтра, 13.10"}
    assert s["buckets"]["debtors"] == 2 and s["buckets"]["to_pay"] == 1
    assert s["debt_total"] + s["due_total"] == 1_800_000
    ages = {b["bucket"]: b for b in s["debt_ages"]}
    assert (ages["to_30"]["students"], ages["to_30"]["debt_total"]) == (2, 1_200_000)
    for gone in ("due", "expired", "students_due", "students_expired", "students_with_debt", "debt_split", "students_waiting", "debt_waiting_total"):
        assert gone not in s, gone


def test_the_tabs_rows_numbers_and_words_come_from_the_server(pg_session, clock):
    w, g, st, ms = world(pg_session, clock)
    page = status_page(w, "debtors")
    assert (page["total"], page["students"], page["amount_total"]) == (2, 2, 1_200_000)
    a, b = page["items"]
    assert a["membership_id"] == str(ms[0].id)       # equal sums: the older debt (K 7) first
    assert (a["debt_amount"], a["due_amount"], a["overdue_days"], a["state"]) == (600_000, 0, 7, "debt")
    assert (a["debt_text"], a["debt_sub"], a["state_text"]) == ("Долг 600\xa0000\xa0сум", "просрочено 7 дней", "Долг 600\xa0000\xa0сум")
    assert (b["overdue_days"], b["debt_sub"]) == (2, "просрочено 2 дня")
    assert a["status"] != "expired" and a["days_overdue"] is None                                      # a row that owes something does not say «expired»
    due = status_page(w, "to_pay")
    assert (due["total"], due["students"], due["amount_total"]) == (1, 1, 600_000)
    (c,) = due["items"]
    assert (c["due_amount"], c["debt_amount"], c["due_until"], c["state"]) == (600_000, 0, "2026-10-13", "to_pay")
    assert (c["due_text"], c["due_sub"], c["overdue_days"]) == ("К оплате 600\xa0000\xa0сум", "до завтра, 13.10", None)
    assert status_page(w, "debt")["total"] == 2                                                       # the old name of the tab
    assert client(w, "owner").get(f"{API}/org/payments/status", params={"bucket": "due"}).status_code == 422
    assert client(w, "owner").get(f"{API}/org/payments/status", params={"bucket": "waiting"}).status_code == 422
    for gone in ("past_debt", "current_due", "tab", "waiting_until", "waiting_reason", "debt_reason", "reason", "bucket", "debt_age_days"):
        assert gone not in a, gone


def test_a_student_with_a_debt_and_a_sum_within_the_term_is_in_both_tabs_and_the_sums_agree(pg_session, clock):
    w, g, st, ms = world(pg_session, clock, today="2026-11-01")
    s = summary(w)
    assert (s["debt_total"], s["debtors"], s["due_total"], s["due_students"]) == (1_800_000, 3, 600_000, 1)      # October of A, B, C; November of A
    assert s["due_nearest"] == {"date": "2026-11-05", "text": "05.11"}
    debtors, to_pay = status_page(w, "debtors"), status_page(w, "to_pay")
    assert (debtors["students"], debtors["amount_total"]) == (s["debtors"], s["debt_total"])
    assert (to_pay["students"], to_pay["amount_total"]) == (s["due_students"], s["due_total"])
    assert sum(r["debt_amount"] for r in debtors["items"]) == s["debt_total"] and sum(r["due_amount"] for r in to_pay["items"]) == s["due_total"]
    (a_in_debtors,) = [r for r in debtors["items"] if r["membership_id"] == str(ms[0].id)]
    (a_in_to_pay,) = to_pay["items"]
    assert a_in_debtors["membership_id"] == a_in_to_pay["membership_id"] == str(ms[0].id)
    assert (a_in_debtors["debt_amount"], a_in_debtors["due_amount"], a_in_debtors["overdue_days"]) == (600_000, 600_000, 27)      # 01.11 - 05.10
    assert (a_in_debtors["state"], a_in_debtors["state_text"]) == ("debt", "Долг 600\xa0000\xa0сум")                                # the one label: the worst state
    assert (a_in_debtors["due_text"], a_in_debtors["due_sub"], a_in_debtors["debt_sub"]) == ("К оплате 600\xa0000\xa0сум", "до 05.11", "просрочено 27 дней")
    assert a_in_debtors["unpaid"] == 1_200_000                                                       # everything unpaid of the membership
    # the old tail and the new month are two named parts of the statement of A
    r = client(w, "owner").get(f"{API}/org/memberships/{ms[0].id}/statement")
    assert r.status_code == 200
    summary_of = r.json()["summary"]
    assert (summary_of["debt_amount"], summary_of["due_amount"], summary_of["state"]) == (600_000, 600_000, "debt")
    parts = [(row["details"]["segment"]["id"][-10:], row["details"]["segment"]["parts"]) for row in r.json()["rows"] if row["type"] == "charge"]
    assert [[p["name"] for p in ps] for _, ps in parts] == [["debt"], ["due"]]
    # balances of the centre: the same sums
    bal = client(w, "owner").get(f"{API}/org/payments/balances", params={"kind": "to_pay"}).json()
    assert (bal["total"], bal["due_total"], bal["debt_total"]) == (1, 600_000, 0)
    assert client(w, "owner").get(f"{API}/org/payments/balances", params={"kind": "debtors"}).json()["debt_total"] == 1_800_000


def test_the_collection_of_a_month_splits_by_the_parts_not_by_subtraction(pg_session, clock):
    w, *_ = world(pg_session, clock)
    total = client(w, "owner").get(f"{API}/org/payments/income", params={"from": "2026-10-01", "to": "2026-10-31", "period": "month"}).json()["collection"]["total"]
    assert (total["charged"], total["debt"], total["due"], total["debtors"], total["due_students"]) == (1_800_000, 1_200_000, 600_000, 2, 1)
    position = client(w, "owner").get(f"{API}/org/payments/income", params={"from": "2026-10-01", "to": "2026-10-31", "period": "month"}).json()["position"]
    assert (position["debt_total"], position["due_total"], position["debtors"], position["due_students"]) == (1_200_000, 600_000, 2, 1)


def test_the_number_of_the_dry_run_is_the_real_difference_after_saving_both_ways(pg_session, clock):
    w, *_ = world(pg_session, clock)
    assert summary(w)["debtors"] == 2
    said = dry_run(w, pay_due_days=10)["pay_due_impact"]
    put_settings(w, pay_due_days=10)
    assert summary(w)["debtors"] == 2 - said["to_due"] + said["to_debt"] == 1
    said = dry_run(w, pay_due_days=5)["pay_due_impact"]
    assert (said["to_due"], said["to_debt"]) == (0, 1)
    put_settings(w, pay_due_days=5)
    assert summary(w)["debtors"] == 1 + said["to_debt"] - said["to_due"] == 2


def test_a_part_of_a_debt_a_payment_settles_the_oldest_first_and_the_window_says_it_before(pg_session, clock):
    w, g, st, ms = world(pg_session, clock, today="2026-11-01")
    r = client(w, "owner").get(f"{API}/org/memberships/{ms[0].id}/payments/preview", params={"amount": 700_000, "paid_on": "2026-11-01"}).json()
    assert (r["due"]["debt_amount"], r["due"]["due_amount"], r["due"]["due_until"]) == (600_000, 600_000, "2026-11-05")
    assert r["suggested_amount"] == 1_200_000
    assert r["result"]["summary_text"].startswith("Погасит долг 600\xa0000\xa0сум · к оплате 100\xa0000\xa0сум из 600\xa0000\xa0сум")
    assert (r["result"]["debt_amount_after"], r["result"]["due_amount_after"]) == (0, 500_000)
    pay(w, ms[0], 700_000, D(2026, 11, 1))
    page = status_page(w, "to_pay")
    (row,) = page["items"]
    assert (row["debt_amount"], row["due_amount"]) == (0, 500_000)


def test_the_term_never_touches_the_money_or_the_payroll(pg_session, clock):
    """The snapshot: the money records and the salary sheet of a centre are the same for every term 0..14 days and 0..4 lessons."""
    w, g, st, ms = world(pg_session, clock, today="2026-10-12")
    w.db.add(PayrollRate(org_id=w.org.id, teacher_id=w.A.id, lesson_type="group", percent=Decimal("50"), valid_from=date(2020, 1, 1), created_by=w.owner.id))
    w.db.commit()
    history_row(w.db, w.org, g, "lesson-mon", "2026-10-05", w.A, st, start="10:00", end="11:00", records={str(s.id): "present" for s in st})
    w.db.commit()
    pay(w, ms[1], 250_000, D(2026, 10, 7))
    pay(w, ms[2], 600_000, D(2026, 10, 9))

    def snapshot():
        w.db.expire_all()
        out = {}
        for m in ms:
            out[str(m.id)] = {
                "payments": [(p["amount"], p["paid_on"], p["kind"]) for p in client(w, "owner").get(f"{API}/org/memberships/{m.id}/payments").json()],
                "statement": [(r["date"], r["type"], r["debit"], r["credit"], r["outflow"], r["balance_after"]) for r in client(w, "owner").get(f"{API}/org/memberships/{m.id}/statement").json()["rows"]],
            }
        rows = {r["membership_id"]: (r["unpaid"], r["advance"], r["paid_until"], r["partial_credit"], r["refundable"]) for r in status_page(w, "all")["items"]}
        calc = client(w, "owner").post(f"{API}/org/payroll/months/2026-10/calculate", json={"teacher_ids": [str(w.A.id)]})
        assert calc.status_code == 200, calc.text
        return out, rows, calc.json()["sheets"]

    base = snapshot()
    assert base[2], "the payroll sheet must exist"
    for days in range(0, 15):
        put_settings(w, pay_due_days=days, pay_due_lessons=days % 5)
        assert snapshot() == base, days
    for lessons in range(0, 5):
        put_settings(w, pay_due_lessons=lessons)
        assert snapshot() == base, lessons
