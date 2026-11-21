"""PAY-TAILS Б5.4 / Б1.5 (step 1): the words of a state.  One field of the account (``covered_by``) and one sign of a closed membership feed every text of «оплачено до».  PostgreSQL, through the routes of the
product; every number is worked out by hand in the comment.  ЛП 420 000 for 30 days (01.11 - 30.11)."""
from datetime import date

from src.auth import payments_texts as tx

from tests.money1b_helpers import add, fresh_student, set_schedule
from tests.pg_world import ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block  # noqa: F401  (fixtures)
from tests.test_pay_tails_money_pg import TTF, forgive, leave, lp, om, pay, refund
from tests.test_pay_tails_golden_pg import get

pytestmark = needs_pg
D = date
M = tx.money


def row(w, s, g, bucket="all"):
    """The row of the student in the list of the centre (``all`` - the tracked ones; the other tabs read the closed memberships too)."""
    page = get(w, "/org/payments/status", bucket=bucket, limit=200)
    (found,) = [r for r in page["items"] if r["student_id"] == str(s.id) and r["group_id"] == str(g.id)]
    return found


def test_a_period_closed_by_money_says_paid_until(world, fixed_today):
    g, s = lp(world, fixed_today)
    pay(world, s, g, amount=420_000, paid_on=D(2026, 11, 1))
    r = row(world, s, g)
    assert (r["state_text"], r["covered_by"], r["state_sub"], r["state"]) == ("Оплачено до 30.11", "money", None, "paid")


def test_a_period_closed_only_by_a_forgiveness_says_forgiven_until(world, fixed_today):
    g, s = lp(world, fixed_today)
    forgive(world, s, g, 420_000)
    r = row(world, s, g)
    assert (r["state_text"], r["covered_by"], r["state_sub"]) == ("Прощено до 30.11", "forgiven", None)
    assert r["paid_until"] == "2026-11-30" and r["status"] == "active"                       # the date and the status of the list did not move


def test_money_and_forgiveness_in_one_period_say_paid_until_and_how_much_was_forgiven(world, fixed_today):
    # 120 000 paid, 300 000 forgiven: both cover the period (420 000); money is there -> «Оплачено до», the forgiven part in the sub-line
    g, s = lp(world, fixed_today)
    pay(world, s, g, amount=120_000, paid_on=D(2026, 11, 1))
    forgive(world, s, g, 300_000)
    r = row(world, s, g)
    assert (r["state_text"], r["covered_by"], r["state_sub"]) == ("Оплачено до 30.11", "money", "из них прощено " + M(300_000))


def test_a_closed_membership_never_says_paid_until_whatever_the_day(world, fixed_today):
    # С2: paid 200 000, forgiven 220 000 (94 000 used), left on the 21st day: charged 294 000, nothing is left to pay or to give back -> «Расчёт закрыт»; the same the day after
    g, s = lp(world, fixed_today)
    pay(world, s, g, amount=200_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 21))
    forgive(world, s, g, 220_000)
    leave(world, g, s)
    for day in (D(2026, 11, 21), D(2026, 11, 22)):                                             # the state does not depend on the hour of the day
        fixed_today(day)
        page = get(world, "/org/memberships/%s/statement" % membership(world, s, g).id)["summary"]
        assert (page["state"], page["state_text"], page["state_sub"], page["covered_by"]) == ("settled", "Расчёт закрыт", "ушёл 21.11", "money")
        assert "оплачено до" not in str(page).lower()


def test_a_closed_membership_with_an_advance_says_the_advance_and_with_nothing_ever_says_no_charges(world, fixed_today):
    # paid 420 000, left on the 10th day: charged 140 000, advance 280 000 -> «Аванс 280 000 сум», sub «ушёл 10.11»; a student of a centre whose accounting starts in December: nothing charged, no money -> «Расчётов нет»
    g, s = lp(world, fixed_today)
    pay(world, s, g, amount=420_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 10))
    leave(world, g, s)
    sm = get(world, "/org/memberships/%s/statement" % membership(world, s, g).id)["summary"]
    assert (sm["state"], sm["state_text"], sm["state_sub"], sm["advance"]) == ("paid", "Аванс " + M(280_000), "ушёл 10.11", 280_000)
    refund(world, s, g, 280_000, expect=201)                                                  # paid and everything given back: charged 140 000, money 140 000 -> «Расчёт закрыт», not «Расчётов нет»
    sm = get(world, "/org/memberships/%s/statement" % membership(world, s, g).id)["summary"]
    assert (sm["state"], sm["state_text"]) == ("settled", "Расчёт закрыт")


def test_nothing_ever_charged_and_no_money_is_no_charges(world, fixed_today):
    g, s = lp(world, fixed_today, accounting="2026-12-01")                                    # the accounting of the centre begins in December: November charges nothing
    fixed_today(D(2026, 11, 5))
    leave(world, g, s)
    sm = get(world, "/org/memberships/%s/statement" % membership(world, s, g).id)["summary"]
    assert (sm["state"], sm["state_text"], sm["state_sub"], sm["charged"]) == ("no_charges", "Расчётов нет", "ушёл 05.11", 0)


def test_the_receipt_of_a_closed_period_says_paid_until_and_of_a_part_says_sums(world, fixed_today):
    g, s = lp(world, fixed_today)
    made = pay(world, s, g, amount=420_000, paid_on=D(2026, 11, 1))
    rc = owner(world).get(f"{API}/org/payments/{made['payment']['id'] if 'payment' in made else made['id']}/receipt").json()
    assert rc["receipt_state_text"] == "всё оплачено, оплачено до 30.11" and "text" not in rc["state_on_issue"]      # the one state of the day: debt-free + the coverage; the date lives in as_of
    t = fresh_student(world, "Частичный")
    add(world, t, group=g, mode="add")
    made = pay(world, t, g, amount=100_000, paid_on=D(2026, 11, 1))
    rc = owner(world).get(f"{API}/org/payments/{made['payment']['id'] if 'payment' in made else made['id']}/receipt").json()
    text = rc["receipt_state_text"]
    assert text.startswith("оплачено " + tx.num(100_000) + " из " + M(420_000) + " · ") and M(320_000) in text and "оплачено до" not in text      # 420 000 - 100 000 = 320 000: no day inside the period
    assert "text" not in rc["state_on_issue"] and rc["state_on_issue"]["debt"] == 320_000


def test_the_receipt_of_an_old_payment_says_what_is_not_paid_now_and_up_to_which_day_the_money_goes(world, fixed_today):
    # Б9.5: the route of the receipt, the phrase with BOTH parts.  420 000 paid on 01.11 for the personal period 01.11 - 30.11.  On 03.12 the second period (01.12 - 30.12, 420 000) has begun and is not paid:
    # the state of the day of issue is «не оплачено 420 000 сум, оплачено до 30.11» (the days of the first period are covered, the whole second one is a debt).
    g, s = lp(world, fixed_today)
    made = pay(world, s, g, amount=420_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 12, 3))
    rc = owner(world).get(f"{API}/org/payments/{made['payment']['id'] if 'payment' in made else made['id']}/receipt").json()
    assert rc["receipt_state_text"] == f"не оплачено {M(420_000)}, оплачено до 30.11" and rc["state_on_issue"]["debt"] == 420_000 and rc["state_on_issue"]["as_of"] == "2026-12-03"


def test_the_sub_line_of_a_debtor_has_no_day_inside_a_period(world, fixed_today):
    g, s = lp(world, fixed_today)
    pay(world, s, g, amount=100_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 20))
    r = row(world, s, g, "debtors")
    assert r["debt_amount"] == 320_000 and "оплачено до" not in (r["debt_sub"] or "")


def test_a_closed_debtor_says_that_he_left_before_the_overdue_days(world, fixed_today):
    g, s = lp(world, fixed_today)
    fixed_today(D(2026, 11, 10))
    leave(world, g, s)
    fixed_today(D(2026, 11, 20))
    r = row(world, s, g, "debtors")
    assert r["debt_sub"].startswith("ушёл 10.11") and r["state_sub"] == "ушёл 10.11" and r["left_on"] == "2026-11-10"


def test_a_closed_membership_receipt_has_no_paid_until(world, fixed_today):
    g, s = lp(world, fixed_today)
    made = pay(world, s, g, amount=420_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 10))
    leave(world, g, s)
    rc = owner(world).get(f"{API}/org/payments/{made['payment']['id'] if 'payment' in made else made['id']}/receipt").json()
    assert rc["receipt_state_text"] == "расчёт закрыт" and "text" not in rc["state_on_issue"]      # a closed membership: no coverage day, no debt/advance words


def test_om_a_period_forgiven_whole_says_forgiven_until_and_a_transfer_from_a_group_is_money(world, fixed_today):
    g, s = om(world, fixed_today)
    forgive(world, s, g, 600_000)
    r = row(world, s, g)
    assert (r["covered_by"], r["state_text"].startswith("Прощено до ")) == ("forgiven", True)


def test_the_money_that_came_from_another_group_covers_as_money(world, fixed_today):
    from tests.money1b_helpers import transfer
    from tests.pg_world import ensure_billing, give_group_price, make_group

    g, s = lp(world, fixed_today)
    b = make_group(world.db, world.org, world.teacher, "Вторая ЛП")
    give_group_price(world.db, b, 420_000)
    ensure_billing(world, b, price=None)
    pay(world, s, g, amount=420_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 2))
    transfer(world, s, b)
    r = row(world, s, b)
    assert (r["state"], r["state_text"]) == ("starts", "Начнётся 03.11")                       # the day of the transfer stays with the old group: the new one begins to charge tomorrow; the money came with him
    fixed_today(D(2026, 11, 3))
    r = row(world, s, b)
    assert r["covered_by"] == "money" and r["state_text"] == "К оплате " + M(28_000)      # the money that came (ЛП: 420 000 - 2 days x 14 000 = 392 000) covers the new period in part: 420 000 - 392 000 = 28 000 left


def pause(w, s, g, first, last):
    r = owner(w).post(f"{API}/org/memberships/{membership(w, s, g).id}/pauses", json={"first_day": first, "last_day": last, "reason": "Болезнь"})
    assert r.status_code == 201, r.text


def test_b6_3_a_debtor_who_paid_nothing_never_says_paid_until(world, fixed_today):
    # ЛП 420 000 from 01.11, nothing paid, a pause 01.11 - 10.11: the period begins 11.11 and falls due in 5 days (15.11); today 21.11: debt 420 000, overdue 21 - 15 = 6 days.  Nothing covers a single day:
    # no «оплачено до 10.11» (was: «просрочено 6 дней · оплачено до 10.11»); ``status`` and ``paid_until`` of the row are the same as before
    g, s = lp(world, fixed_today)
    pause(world, s, g, "2026-11-01", "2026-11-10")
    fixed_today(D(2026, 11, 21))
    r = row(world, s, g)
    assert (r["debt_amount"], r["debt_sub"], r["status"], r["paid_until"]) == (420_000, "просрочено 6 дней", "active", "2026-11-10")
    assert "оплачено до" not in str(r).lower() and r["covered_by"] is None


def test_b6_3_a_student_with_a_future_pause_and_nothing_paid_is_in_the_calm_tab_not_in_the_expiring_one(world, fixed_today):
    # ОМ 600 000, Tue / Thu / Fri; enrolled Sat 21.11, a pause 26.11 - 30.11, nothing paid.  The first lesson is Tue 24.11: «Начнётся 24.11» (the forecast 50 000 = 600 000 / 12).  Nothing is paid, so nothing ENDS:
    # the row is in «Без долга» (was: «Заканчивается оплата»); the cards of the sums are the same (debt 0, to pay 0), the status of the row is ``expiring`` as before
    fixed_today(D(2026, 11, 21))
    settings_block(world, accounting_start_date="2026-10-01", billing_anchor="fixed_day", billing_day=1)
    g = make_group(world.db, world.org, world.teacher, "Общий")
    give_group_price(world.db, g, 600_000)
    set_schedule(world, TTF, g)
    ensure_billing(world, g, price=None)
    s = fresh_student(world, "Будущий")
    add(world, s, group=g, mode="add")
    pause(world, s, g, "2026-11-26", "2026-11-30")
    r = row(world, s, g)
    assert (r["state"], r["state_text"], r["status"]) == ("starts", "Начнётся 24.11", "expiring")
    tabs = {b: get(world, "/org/payments/status", bucket=b, limit=50) for b in ("expiring", "active", "debtors", "to_pay")}
    assert [(b, t["total"], t["amount_total"]) for b, t in tabs.items()] == [("expiring", 0, 0), ("active", 1, 0), ("debtors", 0, 0), ("to_pay", 0, 0)]
    summary = get(world, "/org/payments/summary")
    assert (summary["buckets"]["expiring"], summary["buckets"]["active"], summary["debt_total"], summary["due_total"]) == (0, 1, 0, 0)


def test_b6_4_money_ahead_of_the_next_period_names_the_border_and_the_sum_and_never_contradicts_the_term(world, fixed_today):
    # ЛП 420 000 (14 000 a day) for 30 days: November 01.11 - 30.11, December 01.12 - 30.12.  Paid 420 000 (01.11) and 98 000 ahead (20.11) = 7 days of December (98 000 / 14 000): the date INSIDE December is 07.12.
    # 20.11: the words «Оплачено до 30.11» (the border of the last period covered in full; was: 07.12) and under them «вперёд оплачено 98 000 сум»; ``paid_until`` of the row is still 07.12.
    # 01.12: December has begun, 98 000 of 420 000 are paid: «К оплате 322 000 до 05.12» (the term counts 5 days from 01.12) - the same sum that 98 000 ahead left.  06.12: the debt 322 000.
    g, s = lp(world, fixed_today)
    pay(world, s, g, amount=420_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 20))
    pay(world, s, g, amount=98_000, paid_on=D(2026, 11, 20))
    r = row(world, s, g)
    assert (r["state_text"], r["state_sub"], r["paid_until"], r["debt_amount"], r["due_amount"]) == ("Оплачено до 30.11", "вперёд оплачено " + M(98_000), "2026-12-07", 0, 0)
    fixed_today(D(2026, 11, 30))
    assert (row(world, s, g)["state_text"], row(world, s, g)["state_sub"]) == ("Оплачено до 30.11", "вперёд оплачено " + M(98_000))
    fixed_today(D(2026, 12, 1))
    r = row(world, s, g)
    assert (r["state"], r["due_amount"], r["due_until"], r["state_text"]) == ("to_pay", 322_000, "2026-12-05", "К оплате " + M(322_000))
    assert "оплачено до" not in str(r).lower()
    fixed_today(D(2026, 12, 6))
    r = row(world, s, g)
    assert (r["state"], r["debt_amount"], r["debt_sub"]) == ("debt", 322_000, "просрочено 1 день")


def charge_row(w, s, g):
    st = get(w, "/org/memberships/%s/statement" % membership(w, s, g).id)
    (line,) = [r for r in st["rows"] if r["type"] == "charge"]
    return line


def test_b6_12_the_column_open_of_a_period_closed_by_money_by_a_forgiveness_or_by_both(world, fixed_today):
    # ЛП 420 000 (01.11 - 30.11), today 05.11.  a: money 420 000 only -> «Оплачено» (paid); b: forgiven 420 000 only -> «Прощено» (forgiven); c: money 100 000 + the forgiveness 320 000 used together (100 000 + 320 000 =
    # 420 000) -> «Закрыто» (closed): «Оплачено» is about money only.  Excel says the same words (the cell of the column «Остаток»)
    import io

    from openpyxl import load_workbook

    g, a = lp(world, fixed_today)
    b, c = fresh_student(world, "Прощён"), fresh_student(world, "Вместе")
    for s in (b, c):
        add(world, s, group=g, mode="add")
    fixed_today(D(2026, 11, 5))
    pay(world, a, g, amount=420_000, paid_on=D(2026, 11, 1))
    forgive(world, b, g, 420_000)
    pay(world, c, g, amount=100_000, paid_on=D(2026, 11, 1))
    forgive(world, c, g, 320_000)
    got = {n: (charge_row(world, s, g)["open_name"], charge_row(world, s, g)["open_text"], charge_row(world, s, g)["open_amount"]) for n, s in (("a", a), ("b", b), ("c", c))}
    assert got == {"a": ("paid", "Оплачено", 0), "b": ("forgiven", "Прощено", 0), "c": ("closed", "Закрыто", 0)}
    r = owner(world).get(f"{API}/org/memberships/{membership(world, c, g).id}/statement/export", params={"format": "xlsx"})
    assert r.status_code == 200, r.text
    cells = [str(x.value) for row in load_workbook(io.BytesIO(r.content)).active.iter_rows() for x in row if x.value is not None]
    assert any(v.startswith("Закрыто") for v in cells)
