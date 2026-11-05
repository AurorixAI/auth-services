"""Money-1 pack B7 / ДЫРА-05 (PostgreSQL): «Ждём оплату» - the tabs and the numbers of the red banner.

06.11, a month of «свой месяц» began on 05.11: S1 paid October only (waits for three days), S0 paid nothing since October (a debtor), S2 paid ahead.  The debt is ONE number for everybody (I-1: 900 000 = 600 000 + 300 000);
the debtors tab and the banner count the real debtors only."""
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
    seed_payment(w, membership(w, w.students[1]), 300_000, D(2026, 10, 5))
    seed_payment(w, membership(w, w.students[2]), 600_000, D(2026, 10, 5))


def page(w, bucket):
    return owner(w).get(f"{API}/org/payments/status", params={"bucket": bucket, "limit": 100}).json()


def test_b7_the_tabs_the_summary_and_the_numbers_of_the_banner(world, fixed_today):
    w = world
    waiting_world(w, fixed_today)
    s = owner(w).get(f"{API}/org/payments/summary").json()
    assert s["buckets"] == {"debt": 1, "waiting": 1, "expiring": 0, "active": 1, "paused": 0}
    assert (s["debt_total"], s["debt_waiting_total"], s["debtors_debt_total"]) == (900_000, 300_000, 600_000)             # I-1: the same debt everywhere; the banner takes the debt of the debtors
    assert (s["debtors"], s["students_waiting"], s["students_with_debt"], s["memberships_with_debt"], s["students_overdue"]) == (1, 1, 2, 2, 0)           # the student who waits is «просрочено» by the status of his paid period but not by the banner; S0 never paid: no paid period to be past
    assert s["debtors"] + s["students_waiting"] == s["students_with_debt"]
    debt, waiting = page(w, "debt"), page(w, "waiting")
    assert (debt["total"], waiting["total"]) == (s["buckets"]["debt"], s["buckets"]["waiting"]) == (1, 1)
    assert sum(i["debt"] for i in debt["items"]) + sum(i["debt"] for i in waiting["items"]) == s["debt_total"]
    (row,) = waiting["items"]
    assert (row["bucket"], row["debt_reason"], row["reason"], row["waiting_until"], row["debt"]) == ("waiting", "waiting", "Ждём оплату до 07.11", "2026-11-07", 300_000)
    (bad,) = debt["items"]
    assert (bad["bucket"], bad["debt"], bad["waiting_until"]) == ("debt", 600_000, None) and bad["debt_reason"] == "never_paid"


def test_b7_on_the_fourth_day_the_same_student_is_a_debtor_and_in_the_banner(world, fixed_today):
    w = world
    waiting_world(w, fixed_today, today=D(2026, 11, 8))
    s = owner(w).get(f"{API}/org/payments/summary").json()
    assert s["buckets"]["waiting"] == 0 and s["buckets"]["debt"] == 2 and s["debtors"] == 2 and s["debt_waiting_total"] == 0
    assert s["debtors_debt_total"] == s["debt_total"] == 900_000 and s["students_overdue"] == 1
