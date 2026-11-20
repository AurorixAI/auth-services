"""PAY-DUE «Е»: the scenarios of sections 5 and 10.12 that had no test (PostgreSQL, through the routes of the product, numbers worked out by hand, the clock is moved explicitly).

World A (``pay_due_helpers.world``): Mon 12.10.2026, «Свой месяц», 600 000 each, a group of Mon / Wed / Fri, term 5 days: A joined 01.10 (D 05.10), B 06.10 (D 10.10), C 09.10 (D 13.10).
World B (packages): ``period_mode = lessons`` of 8 lessons for 600 000 = 75 000 a lesson, M = 2, the unpaid lessons are the held ones."""
from datetime import date

from tests.money1b_helpers import add, fresh_student, hold_lessons
from tests.pay_due_helpers import API, D, client, dry_run, pay, put_settings, status_page, summary, world, clock, pg_session, needs_pg  # noqa: F401  (fixtures)

pytestmark = needs_pg
NB = "\xa0"


def row_of(page, membership):
    (row,) = [r for r in page["items"] if r["membership_id"] == str(membership.id)]
    return row


def test_the_midnight_of_tashkent_moves_a_sum_from_to_pay_to_a_debt(pg_session, clock):
    w, g, st, ms = world(pg_session, clock)                       # C: D = 13.10
    clock.set("2026-10-13 23:50")
    c = row_of(status_page(w, "all"), ms[2])
    assert (c["state"], c["due_until"], c["due_sub"], c["debt_amount"]) == ("to_pay", "2026-10-13", "до сегодня", 0)
    clock.set("2026-10-14 00:10")
    c = row_of(status_page(w, "all"), ms[2])
    assert (c["state"], c["debt_amount"], c["overdue_days"], c["debt_sub"]) == ("debt", 600_000, 1, "просрочено 1 день")


def test_the_number_of_the_dry_run_is_the_real_difference_after_saving_in_both_directions(pg_session, clock):
    w, g, st, ms = world(pg_session, clock)                       # debtors now: A (D 05.10) and B (D 10.10)
    assert summary(w)["debtors"] == 2
    up = dry_run(w, pay_due_days=10)["pay_due_impact"]            # D(A) = 10.10 (still a debt on 12.10), D(B) = 15.10 (within the term): B becomes «к оплате»
    assert (up["to_due"], up["to_debt"]) == (1, 0)
    assert summary(w)["debtors"] == 2                             # a dry run writes nothing
    put_settings(w, pay_due_days=10)
    assert summary(w)["debtors"] == 1                             # 2 - to_due
    down = dry_run(w, pay_due_days=5)["pay_due_impact"]
    assert (down["to_due"], down["to_debt"]) == (0, 1)
    put_settings(w, pay_due_days=5)
    assert summary(w)["debtors"] == 2                             # 1 + to_debt


def test_an_adding_on_a_day_with_a_lesson_and_on_a_day_without_one_both_count_the_term_from_the_first_lesson(pg_session, clock):
    w, g, st, ms = world(pg_session, clock, today="2026-10-13", billing_anchor="fixed_day", billing_day=1)       # Tuesday: no lesson (Mon / Wed / Fri)
    s, s2 = fresh_student(w, "Новичок"), fresh_student(w, "Второй")

    def window(student):
        r = client(w, "owner").post(f"{API}/org/groups/{g.id}/students/bulk", params={"mode": "add", "dry_run": "true"}, json={"student_ids": [str(student.id)]})
        assert r.status_code == 200, r.text
        return r.json()["students"][0]["billing"]["immediate"]

    assert window(s) is None                                                                  # a day without a lesson: the charge starts with the first lesson (Wed 14.10) - nothing is owed AT ONCE, the server is silent
    r = client(w, "owner").post(f"{API}/org/groups/{g.id}/students/bulk", params={"mode": "add"}, json={"student_ids": [str(s.id)]})
    assert r.status_code == 200, r.text
    clock.set("2026-10-14 09:00")                                                             # Wednesday: the lesson day
    (mine,) = [x for x in status_page(w, "all")["items"] if x["student_id"] == str(s.id)]
    assert (mine["state"], mine["due_until"]) == ("to_pay", "2026-10-18")                     # the term counts from the first lesson: 14, 15, 16, 17, 18
    imm = window(s2)                                                                          # an adding on the day of a lesson: the same term
    assert (imm["due_until"], imm["debt_amount"]) == ("2026-10-18", 0) and imm["due_amount"] > 0 and imm["text"].startswith("К оплате до 18.10:")


def test_a_package_the_third_unpaid_lesson_and_the_15th_day_make_a_debt_and_a_payment_of_one_lesson_takes_the_oldest(pg_session, clock):
    w, g, st, ms = world(pg_session, clock, joins=(D(2026, 10, 1),), period_mode="lessons", lessons_per_payment=8)
    hold_lessons(w, st[0], [D(2026, 10, 2), D(2026, 10, 5), D(2026, 10, 7)], group=g)        # Fri, Mon, Wed
    a = row_of(status_page(w, "all"), ms[0])
    # 3 unpaid lessons of 75 000: the 2 NEWEST (05.10, 07.10) are «к оплате» until the oldest of them + 14 days = 19.10, the third (02.10) is a debt
    assert (a["debt_amount"], a["due_amount"], a["due_until"], a["due_lessons"]) == (75_000, 150_000, "2026-10-19", {"used": 2, "limit": 2})
    # the oldest lesson (Fri 02.10) became a debt on 06.10, the day before the lesson of 07.10 pushed it out of the 2 newest: on 12.10 it is 6 days overdue
    assert (a["debt_text"], a["due_text"]) == (f"Долг 75{NB}000{NB}сум · 1 урок", f"К оплате 150{NB}000{NB}сум · 2 урока")
    assert a["debt_also_text"] == f"и долг 75{NB}000{NB}сум · 1 урок · просрочено 6 дней" and a["due_also_text"] == f"и к оплате 150{NB}000{NB}сум · 2 урока · до 19.10"
    clock.set("2026-10-20 09:00")                              # 20.10: 05.10 + 14 = 19.10 has passed: 02.10 and 05.10 are debts, 07.10 (until 21.10) is the only one «к оплате»
    a = row_of(status_page(w, "all"), ms[0])
    assert (a["debt_amount"], a["due_amount"], a["due_until"]) == (150_000, 75_000, "2026-10-21")
    clock.set("2026-10-12 09:00")
    pay(w, ms[0], 75_000, D(2026, 10, 12))                    # one lesson: the oldest is paid
    a = row_of(status_page(w, "all"), ms[0])
    assert (a["debt_amount"], a["due_amount"], a["state"]) == (0, 150_000, "to_pay")
    hold_lessons(w, st[0], [D(2026, 10, 9)], group=g)         # a lesson marked later (Fri 09.10): the newest two are 07.10 and 09.10, 05.10 becomes a debt
    a = row_of(status_page(w, "all"), ms[0])
    assert (a["debt_amount"], a["due_amount"]) == (75_000, 150_000)
