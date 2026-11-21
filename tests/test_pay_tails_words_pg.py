"""PAY-TAILS Б5.4 / Б1.5 (step 1): the words of a state.  One field of the account (``covered_by``) and one sign of a closed membership feed every text of «оплачено до».  PostgreSQL, through the routes of the
product; every number is worked out by hand in the comment.  ЛП 420 000 for 30 days (01.11 - 30.11)."""
from datetime import date

from src.auth import payments_texts as tx

from tests.money1b_helpers import add, fresh_student
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixtures)
from tests.test_pay_tails_money_pg import forgive, leave, lp, om, pay, refund
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
    assert rc["receipt_state_text"] == "оплачено до 30.11" and rc["state_on_issue"]["text"].endswith("оплачено до 30.11")
    t = fresh_student(world, "Частичный")
    add(world, t, group=g, mode="add")
    made = pay(world, t, g, amount=100_000, paid_on=D(2026, 11, 1))
    rc = owner(world).get(f"{API}/org/payments/{made['payment']['id'] if 'payment' in made else made['id']}/receipt").json()
    text = rc["receipt_state_text"]
    assert text.startswith("оплачено " + tx.num(100_000) + " из " + M(420_000) + " · ") and M(320_000) in text and "оплачено до" not in text      # 420 000 - 100 000 = 320 000: no day inside the period
    assert "оплачено до" not in rc["state_on_issue"]["text"]


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
    assert rc["receipt_state_text"] is None and "оплачено до" not in rc["state_on_issue"]["text"]


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
