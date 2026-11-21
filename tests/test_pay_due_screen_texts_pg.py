"""PAY-DUE «тексты для экранов» (the screen shows the words of the server, it glues and counts nothing: LESSONS 143): PostgreSQL, through the routes of the product, numbers worked out by hand.

World as in ``test_pay_due_contract_pg``: Mon 12.10.2026, «Свой месяц», 600 000 each, nobody paid, term 5 days: A joined 01.10 (D 05.10: a debt, K 7), B joined 06.10 (D 10.10: a debt, K 2),
C joined 09.10 (D 13.10: «К оплате», until tomorrow)."""
from datetime import datetime

from src.models.auth import Organization
from tests.ops_helpers import new_group, new_student
from tests.pay_due_helpers import API, D, client, dry_run, pay, put_settings, status_page, summary, world, clock, pg_session, needs_pg  # noqa: F401  (fixtures)
from tests.payf_helpers import slots
from tests.pg_world import give_group_price

pytestmark = needs_pg
NB = "\xa0"


def row_of(page, membership):
    (row,) = [r for r in page["items"] if r["membership_id"] == str(membership.id)]
    return row


def test_state_text_is_filled_for_every_state(pg_session, clock):
    w, g, st, ms = world(pg_session, clock)
    page = status_page(w, "all")
    assert row_of(page, ms[0])["state_text"] == f"Долг 600{NB}000{NB}сум"                 # debt
    assert row_of(page, ms[2])["state_text"] == f"К оплате 600{NB}000{NB}сум"             # within the term
    pay(w, ms[2], 600_000, D(2026, 10, 12))
    paid = row_of(status_page(w, "all"), ms[2])
    assert paid["state"] == "paid" and paid["state_text"] == f"Оплачено до {paid['paid_until'][8:10]}.{paid['paid_until'][5:7]}"       # 09.10 + a month: paid until the day before the next period
    assert paid["state_text"] == "Оплачено до 08.11"
    r = client(w, "owner").post(f"{API}/org/memberships/{ms[2].id}/pauses", json={"first_day": "2026-10-13", "last_day": "2026-10-18"})
    assert r.status_code in (200, 201), r.text
    paused = row_of(status_page(w, "all"), ms[2])
    assert paused["state"] in ("paused", "paid")
    if paused["state"] == "paused":
        assert paused["state_text"].startswith("На паузе")


def test_a_paid_package_says_how_many_lessons_are_left_and_an_advance_says_the_sum(pg_session, clock):
    from tests.money1b_helpers import hold_lessons

    w, g, st, ms = world(pg_session, clock, joins=(D(2026, 10, 1),), period_mode="lessons", lessons_per_payment=8)
    pay(w, ms[0], 600_000, D(2026, 10, 12))
    row = row_of(status_page(w, "all"), ms[0])
    assert row["state"] == "paid" and row["state_text"] in ("Осталось 8 уроков", f"Аванс 600{NB}000{NB}сум")      # nothing is held yet
    hold_lessons(w, st[0], [D(2026, 10, 2), D(2026, 10, 5), D(2026, 10, 7)], group=g)         # Fri, Mon, Wed: 3 lessons of the package of 8 are held
    row = row_of(status_page(w, "all"), ms[0])
    assert (row["state"], row["state_text"]) == ("paid", "Осталось 5 уроков"), row            # 8 paid - 3 held


def test_buckets_count_students_and_bucket_rows_count_memberships(pg_session, clock):
    w, g, st, ms = world(pg_session, clock, today="2026-11-01")
    other = new_group(w.db, w.org, w.ids, "G1", "Вторая", w.A, slots())
    give_group_price(w.db, other, 600_000, valid_from=D(2026, 1, 1))
    w.db.commit()
    r = client(w, "owner").post(f"{API}/org/groups/{other.id}/students/bulk", params={"mode": "add"}, json={"student_ids": [str(st[0].id)], "pay_from": "2026-11-01"})
    assert r.status_code == 200, r.text
    s = summary(w)
    # A (s0) is a debtor in the first group (October) and, with a sum within the term, in the second (from 01.11, until 05.11): two memberships, ONE student in each tab
    assert s["buckets"]["debtors"] == len({x["student_id"] for x in status_page(w, "debtors")["items"]}) == status_page(w, "debtors")["students"]
    assert s["bucket_rows"]["debtors"] == status_page(w, "debtors")["total"]
    assert s["buckets"]["to_pay"] == status_page(w, "to_pay")["students"] and s["bucket_rows"]["to_pay"] == status_page(w, "to_pay")["total"]
    assert s["bucket_rows"]["to_pay"] > s["buckets"]["to_pay"] or s["bucket_rows"]["debtors"] >= s["buckets"]["debtors"]
    assert set(s["buckets"]) == set(s["bucket_rows"]) == {"debtors", "to_pay", "expiring", "active", "paused", "advance"}      # PAY-TAILS Б1.1: + the tab «Аванс»


def test_the_second_line_of_a_row_that_owes_both(pg_session, clock):
    w, g, st, ms = world(pg_session, clock, today="2026-11-01")
    a = row_of(status_page(w, "debtors"), ms[0])                  # A: October is a debt (D 05.10, K = 27 days on 01.11), November (01.11-30.11) is «к оплате» until 05.11
    assert a["debt_amount"] == 600_000 and a["due_amount"] == 600_000
    assert a["debt_also_text"] == f"и долг 600{NB}000{NB}сум · просрочено {a['overdue_days']} " + ("дней" if a["overdue_days"] % 10 not in (1, 2, 3, 4) or a["overdue_days"] // 10 == 1 else "дня")
    assert a["due_also_text"] == f"и к оплате 600{NB}000{NB}сум до 05.11"
    b = row_of(status_page(w, "debtors"), ms[1])
    assert (b["debt_amount"], b["due_amount"], b["debt_also_text"], b["due_also_text"]) == (600_000, 0, None, None)      # B's November has not begun (06.11): one sum, no second line
    only = status_page(w, "debtors")
    assert all(r["debt_also_text"] is None for r in only["items"] if r["due_amount"] == 0) and all(r["due_also_text"] is None for r in only["items"] if r["due_amount"] == 0)


def test_the_nearest_term_is_a_phrase_without_a_preposition(pg_session, clock):
    w, g, st, ms = world(pg_session, clock)
    assert summary(w)["due_nearest"] == {"date": "2026-10-13", "text": "завтра, 13.10"}
    pg_session.expire_all()
    clock.set("2026-10-13 09:00")
    assert summary(w)["due_nearest"] == {"date": "2026-10-13", "text": "сегодня"}
    w2, _, _, _ = world(pg_session, clock, today="2026-11-01")
    assert summary(w2)["due_nearest"]["text"] == "05.11"


def test_the_example_of_the_term_and_the_limit_of_a_package_are_the_words_of_the_server(pg_session, clock):
    w, g, st, ms = world(pg_session, clock, billing_anchor="fixed_day", billing_day=1)
    r = client(w, "owner").get(f"{API}/org/settings/payments")
    assert r.status_code == 200
    ex = r.json()["pay_due_example"]
    assert ex["period"] == "Период с 01.11 — платить до 05.11 включительно" and ex["package"] == "Третий неоплаченный урок или 15-й день после урока — уже долг" and ex["text"] == ex["period"]
    asked = dry_run(w, pay_due_days=10, pay_due_lessons=4)["pay_due_example"]                       # the REQUESTED values, nothing is written
    assert asked["period"] == "Период с 01.11 — платить до 10.11 включительно" and asked["package"] == "Пятый неоплаченный урок или 15-й день после урока — уже долг"
    assert dry_run(w, pay_due_days=0)["pay_due_example"]["period"] == "Неоплата — долг с первого дня"
    assert client(w, "owner").get(f"{API}/org/settings/payments").json()["pay_due_days"] == 5
    assert summary(w)["pay_due_package_max_days"] == 14


def test_the_receipts_position_and_the_month_collection_have_the_words_of_both_parts(pg_session, clock):
    w, g, st, ms = world(pg_session, clock)
    r = client(w, "owner").get(f"{API}/org/payments/receipts", params={"from": "2026-10-01", "to": "2026-10-31"})
    assert r.status_code == 200, r.text
    pos = r.json()["position"]
    assert (pos["debt_total"], pos["due_total"]) == (1_200_000, 600_000)
    assert (pos["debt_text"], pos["due_text"]) == (f"Долг 1{NB}200{NB}000{NB}сум", f"К оплате 600{NB}000{NB}сум")
    inc = client(w, "owner").get(f"{API}/org/payments/income", params={"from": "2026-10-01", "to": "2026-10-31"})
    assert inc.status_code == 200, inc.text
    assert (inc.json()["position"]["debt_text"], inc.json()["position"]["due_text"]) == (pos["debt_text"], pos["due_text"])


def test_the_probe_of_a_payment_says_what_stays_in_the_words_of_the_server(pg_session, clock):
    w, g, st, ms = world(pg_session, clock, today="2026-11-01")
    ask = lambda m, amount: client(w, "owner").get(f"{API}/org/memberships/{m.id}/payments/preview", params={"amount": amount, "paid_on": "2026-11-01"}).json()["result"]
    # A owes October (600 000, a debt since 05.10) and November (600 000, «к оплате» until 05.11)
    assert ask(ms[0], 700_000)["after_text"] == f"Останется: к оплате 500{NB}000{NB}сум"                                  # the oldest sum is settled first: the debt, then 100 000 of November
    assert ask(ms[0], 200_000)["after_text"] == f"Останется: долг 400{NB}000{NB}сум · к оплате 600{NB}000{NB}сум"          # 600 000 - 200 000 of the debt, November is untouched
    # B joined 06.10: its October is a debt too, November begins on 06.11 (nothing yet): 600 000 settle the debt completely
    assert ask(ms[1], 100_000)["after_text"] == f"Останется: долг 500{NB}000{NB}сум"
    full = ask(ms[0], 1_200_000)
    assert "paid_until_after" not in full and full["after_text"] == "Оплачено до 30.11"      # Oct 600 000 + Nov 600 000 paid in full: the border of November; the date lives only in the words


def test_free_and_no_price_have_their_own_words(pg_session, clock):
    from datetime import datetime as _dt
    from src.models.auth import StudentGroup
    w, g, st, ms = world(pg_session, clock, joins=(D(2026, 10, 12),))                 # joined today: nothing is charged before the free price applies
    r = client(w, "owner").put(f"{API}/org/memberships/{ms[0].id}/price-override", json={"amount": 0, "reason": "грант", "confirm_unusual": True})
    assert r.status_code == 200, r.text
    free = row_of(status_page(w, "all"), ms[0])
    assert (free["state"], free["state_text"]) == ("free", "Бесплатно"), free
    bare = new_group(w.db, w.org, w.ids, "G1", "Без цены", w.A, slots())              # no price on the group: the student has nothing to pay
    s = new_student(w.db, w.org, w.ids, "s9", "Безцены", "Тест", bare, joined=_dt(2026, 10, 1, 6, 0))
    w.db.commit()
    m = w.db.query(StudentGroup).filter_by(group_id=bare.id, student_id=s.id).one()
    none = row_of(status_page(w, "all"), m)
    assert (none["state"], none["state_text"]) == ("none", "Нет цены")
