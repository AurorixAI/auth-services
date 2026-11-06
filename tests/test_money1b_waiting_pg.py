"""Money-1 pack B7 / ДЫРА-05 (PostgreSQL), R3 №20 / №21: «Ждём оплату» - ONE definition of a debtor on every screen, and the old fields keep their old meaning in this release.

06.11, a month of «свой месяц» began on 05.11: S1 and S2 paid October only (they wait for three days), S0 paid nothing since October (a debtor).  The debt is ONE number for everybody (I-1: 1 200 000 =
600 000 + 300 000 + 300 000).  The OLD fields (``debt_total``, ``debtors``, ``buckets.debt``, ``students_expired`` ...) keep their old meaning - the old front is not broken in the minutes between the
release of the server and of the front; the NEW fields (``debt_split``, ``tab``, ``waiting_until``, ``kind=debtors|waiting``, ``debtors_debt_total`` ...) say it by the one definition."""
from datetime import date

from tests.pg_world import give_group_price, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, seed_payment, settings_block, stay  # noqa: F401  (fixtures)

pytestmark = needs_pg
D = date


def waiting_world(w, fixed_today, today=D(2026, 11, 6)):
    fixed_today(today)
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 300_000, valid_from=D(2026, 1, 1))
    for student in w.students:
        stay(w, membership(w, student), D(2026, 10, 5))
    for student in w.students[1:]:
        seed_payment(w, membership(w, student), 300_000, D(2026, 10, 5))                    # S1, S2: October paid, November not


def page(w, bucket):
    return owner(w).get(f"{API}/org/payments/status", params={"bucket": bucket, "limit": 100}).json()


def test_b7_two_waiting_and_one_debtor_every_screen_and_field_agree(world, fixed_today):
    w = world
    waiting_world(w, fixed_today)
    s0, s1, s2 = (membership(w, x) for x in w.students)
    s = owner(w).get(f"{API}/org/payments/summary").json()
    # ---- the OLD fields: the old meaning (every debt, the waiting ones included)
    assert s["buckets"] == {"debt": 3, "expiring": 0, "active": 0, "paused": 0}
    assert (s["debt_total"], s["students_with_debt"], s["memberships_with_debt"], s["debtors"]) == (1_200_000, 3, 3, 3)
    assert (s["students_expired"], s["students_due"], s["expired"], s["due"]) == (2, 2, 2, 2)           # the status of the paid period: S1 and S2 are «просрочено» by it
    # ---- the NEW definition: one place, disjoint, adds up to the old numbers
    d = s["debt_split"]
    assert (d["debtors"], d["debtors_memberships"], d["debtors_debt_total"]) == (1, 1, 600_000)
    assert (d["waiting_students"], d["waiting_memberships"], d["waiting_debt_total"]) == (2, 2, 600_000)
    assert d["all_debt_total"] == d["debtors_debt_total"] + d["waiting_debt_total"] == s["debt_total"] and d["all_students_with_debt"] == s["students_with_debt"]
    assert (d["expired"], d["due"], d["students_expired"], d["students_due"]) == (0, 0, 0, 0)         # the banner: the waiting ones are NOT overdue, not due
    assert (s["students_waiting"], s["debt_waiting_total"]) == (2, 600_000)
    # ---- the lists: the old tab has everybody, the new tabs split them
    debt, debtors, waiting = page(w, "debt"), page(w, "debtors"), page(w, "waiting")
    assert (debt["total"], debtors["total"], waiting["total"]) == (3, 1, 2)
    assert {i["membership_id"] for i in debtors["items"]} == {str(s0.id)} and {i["membership_id"] for i in waiting["items"]} == {str(s1.id), str(s2.id)}
    assert sum(i["debt"] for i in debtors["items"]) == d["debtors_debt_total"] and sum(i["debt"] for i in waiting["items"]) == d["waiting_debt_total"]
    for row in debt["items"]:
        assert row["bucket"] == "debt" and row["debt_reason"] in ("overdue", "partial", "never_paid")      # the old row: unchanged
    for row in waiting["items"]:
        assert (row["tab"], row["waiting_until"], row["waiting_reason"]) == ("waiting", "2026-11-07", "Ждём оплату до 07.11")
    assert (debtors["items"][0]["tab"], debtors["items"][0]["waiting_until"], debtors["items"][0]["waiting_reason"]) == ("debtors", None, None)
    # ---- «Позиция» of the receipts and of the income, the «долги» list
    for url, params in ((f"{API}/org/payments/receipts", {"from": "2026-11-01", "to": "2026-11-06"}), (f"{API}/org/payments/income", {"from": "2026-11-01", "to": "2026-11-30", "period": "month"})):
        position = owner(w).get(url, params=params).json()["position"]
        assert (position["debt_total"], position["debtors_debt_total"], position["debt_waiting_total"]) == (1_200_000, 600_000, 600_000), url
        assert (position["students_with_debt"], position["students_debtors"], position["students_waiting"]) == (3, 1, 2), url
    collection = owner(w).get(f"{API}/org/payments/income", params={"from": "2026-11-01", "to": "2026-11-30", "period": "month"}).json()["collection"]["total"]
    assert (collection["debt"], collection["debt_waiting"], collection["students_waiting"]) == (900_000, 600_000, 2)        # the periods of November: 3 x 300 000, two of them wait
    balances = owner(w).get(f"{API}/org/payments/balances", params={"kind": "debt"}).json()
    assert (balances["total"], balances["debt_total"], balances["debtors_debt_total"], balances["debt_waiting_total"]) == (3, 1_200_000, 600_000, 600_000)
    assert sorted((r["tab"], r["debt"]) for r in balances["items"]) == [("debtors", 600_000), ("waiting", 300_000), ("waiting", 300_000)]
    only = owner(w).get(f"{API}/org/payments/balances", params={"kind": "waiting"}).json()
    assert only["total"] == 2 and only["debt_total"] == only["debt_waiting_total"] == 600_000 and only["debtors_debt_total"] == 0
    assert owner(w).get(f"{API}/org/payments/balances", params={"kind": "debtors"}).json()["total"] == 1


def test_b7_on_the_fourth_day_the_same_students_are_debtors_in_both_definitions(world, fixed_today):
    w = world
    waiting_world(w, fixed_today, today=D(2026, 11, 8))
    s = owner(w).get(f"{API}/org/payments/summary").json()
    d = s["debt_split"]
    assert (d["debtors"], d["waiting_students"], d["waiting_debt_total"]) == (3, 0, 0) and d["debtors_debt_total"] == d["all_debt_total"] == s["debt_total"] == 1_200_000
    assert (d["students_expired"], d["students_due"]) == (2, 2) and (s["students_waiting"], s["debt_waiting_total"]) == (0, 0)
    assert page(w, "waiting")["total"] == 0 and page(w, "debtors")["total"] == 3 == page(w, "debt")["total"]
