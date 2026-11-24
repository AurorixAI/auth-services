"""PAY-TAILS Б11.33 п. 2 + Б11.34 (step 8.14.7 p.2): the window «Внести оплату» says WHERE the typed sum goes: ``money_rows`` (the layout by periods, the words of the server), ``debt_rest`` / ``due_rest`` (what stays unpaid, by periods).
PostgreSQL, through the routes of the product (LESSONS 135); every number is worked out by hand in the comment of its world.  In EVERY world the invariants are checked one by one (lesson 164: the probe = the write):
the sum of the rows = the payment; the sum of ``debt_rest`` = ``debt_amount_after``; the sum of ``due_rest`` = ``due_after``; the layout of the window = the layout of the receipt of the same payment written; the account after = the probe.
The whole answer of the probe of every world is written to ``$ANSWERS_DIR`` (when it is set) for the screens (Б11.34 А7)."""
from datetime import date

from tests.answers_dump import dump
from tests.money1b_helpers import add, fresh_student, hold_lessons
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)
from tests.test_pay_tails_b11_pg import om_at
from tests.test_pay_tails_b78_pg import centre_of, days
from tests.test_pay_tails_money_pg import led, lp
from tests.test_payments_service_pg import pay

pytestmark = needs_pg
D = date


def row(label, amount, state, of=None, periods=1):
    return {"label": label, "amount": amount, "of": of, "state": state, "periods": periods}


def probe(w, s, g, amount, paid_on):
    r = owner(w).get(f"{API}/org/memberships/{membership(w, s, g).id}/payments/preview", params={"amount": amount, "paid_on": paid_on.isoformat()})
    assert r.status_code == 200, r.text
    return r.json()


def receipt_of(w, written):
    r = owner(w).get(f"{API}/org/payments/{written['payment']['id']}/receipt")
    assert r.status_code == 200, r.text
    return r.json()


def check_world(w, s, g, today, amount, name, *, rows, debt_rest, due_rest, debt_before, due_before, debt_after, due_after, advance_after):
    """The probe is read, the payment is written, and the four invariants are checked one by one."""
    window = probe(w, s, g, amount, today)
    dump(f"payment_probe_{name}", window)
    res, head = window["result"], window["due"]
    assert (head["debt_amount"], head["due_amount"]) == (debt_before, due_before), ("the head of the window", head["debt_amount"], head["due_amount"])
    assert res["money_rows"] == rows, ("rows", res["money_rows"])
    assert res["debt_rest"] == [{"label": a, "amount": b} for a, b in debt_rest], ("debt_rest", res["debt_rest"])
    assert res["due_rest"] == [{"label": a, "amount": b} for a, b in due_rest], ("due_rest", res["due_rest"])
    assert (res["debt_amount_after"], res["due_after"], res["advance_after"]) == (debt_after, due_after, advance_after), res
    # I1: the rows add up to the payment; I2: the rests add up to the sums of the window; I3: an «advance» row is there exactly when the advance grew
    assert sum(r["amount"] for r in res["money_rows"]) == amount
    assert sum(r["amount"] for r in res["debt_rest"]) == res["debt_amount_after"]
    assert sum(r["amount"] for r in res["due_rest"]) == res["due_after"]
    assert any(r["state"] == "advance" for r in res["money_rows"]) == (res["advance_after"] > window["due"]["advance"])
    written = pay(w, s, g, amount=amount, paid_on=today)
    # I4: the account after the write = the probe (the same sums in the answer of the write, the row and the account)
    account = led(w, s, g, today)
    assert (account.debt, account.advance) == (res["debt_amount_after"] + res["due_after"], res["advance_after"])
    assert (written["row"]["debt_amount"], written["row"]["due_amount"], written["row"]["advance"]) == (res["debt_amount_after"], res["due_after"], res["advance_after"])
    # I5: the layout of the window = the layout of the receipt of the same payment (the pieces of the receipt, in order, add up to the rows; a closing piece = a closed row)
    items = receipt_of(w, written)["allocations"]
    assert [i["amount"] for i in items] == [i["amount"] for i in written["payment"]["allocation"]["items"]]
    money = [i for i in items if i["bucket"] != "advance"]
    advance = [i for i in items if i["bucket"] == "advance"]
    at = 0
    for r in res["money_rows"]:
        if r["state"] == "advance":
            continue
        pieces = money[at:at + r["periods"]]
        at += r["periods"]
        assert sum(p["amount"] for p in pieces) == r["amount"] and len(pieces) == r["periods"], (r, pieces)
        assert all(p["closes"] for p in pieces) == (r["state"] == "closed"), (r, pieces)
    assert at == len(money)
    assert sum(r["amount"] for r in res["money_rows"] if r["state"] == "advance") == sum(i["amount"] for i in advance)
    return window


def owner_world(world, fixed_today, today):
    """Common month 550 000 (Tue / Thu / Fri), the student since 01.09; paid 508 000 on 05.09 (so 42 000 of September stay unpaid).  Charged by ``today`` 10.12: September 550 000, October, November, December (the term of December
    ended on 05.12) = 4 x 550 000 = 2 200 000; paid 508 000: the debt 1 692 000 = 42 000 + 3 x 550 000."""
    g, s = om_at(world, fixed_today, D(2026, 9, 1), price=550_000)
    fixed_today(D(2026, 9, 5))
    pay(world, s, g, amount=508_000, paid_on=D(2026, 9, 5))
    fixed_today(today)
    return g, s


def test_money_rows_the_world_of_the_owner_550_000_closes_the_rest_of_the_oldest_month_and_part_of_the_next(world, fixed_today):
    # 10.12: the debt 1 692 000.  The payment 550 000: 42 000 close September («Сентябрь, остаток»), 508 000 go to October (508 000 of 550 000: October stays 42 000).  Left: October 42 000 + November 550 000 + December 550 000 = 1 142 000.
    g, s = owner_world(world, fixed_today, D(2026, 12, 10))
    check_world(world, s, g, D(2026, 12, 10), 550_000, "owner_550000", rows=[row("Сентябрь, остаток", 42_000, "closed"), row("Октябрь", 508_000, "part", of=550_000)],
                debt_rest=[("Октябрь", 42_000), ("Ноябрь", 550_000), ("Декабрь", 550_000)], due_rest=[], debt_before=1_692_000, due_before=0, debt_after=1_142_000, due_after=0, advance_after=0)


def test_money_rows_a_payment_of_1_800_000_folds_three_closed_months_and_keeps_the_advance_for_next_year(world, fixed_today):
    # The same debt 1 692 000; 1 800 000: 42 000 close September («остаток» keeps its own line), October + November + December = 3 x 550 000 = 1 650 000 are closed in a row and FOLD into one line (periods 3), the advance 108 000 = 1 800 000 - 1 692 000
    # is January 2027 (the year is not this one: «Аванс на январь 2027»).  Nothing stays unpaid.
    g, s = owner_world(world, fixed_today, D(2026, 12, 10))
    check_world(world, s, g, D(2026, 12, 10), 1_800_000, "owner_1800000", rows=[row("Сентябрь, остаток", 42_000, "closed"), row("Октябрь – Декабрь", 1_650_000, "closed", periods=3), row("Аванс на январь 2027", 108_000, "advance")],
                debt_rest=[], due_rest=[], debt_before=1_692_000, due_before=0, debt_after=0, due_after=0, advance_after=108_000)


def test_money_rows_a_payment_within_the_term_is_part_of_the_period_and_no_debt_appears(world, fixed_today):
    # The student since 01.12 (Tue), December 550 000, 02.12: no debt, «к оплате» 550 000 (the term is until 05.12).  The payment 231 000: December 231 000 of 550 000; left «к оплате» 550 000 - 231 000 = 319 000 (one period), the debt 0.
    g, s = om_at(world, fixed_today, D(2026, 12, 1), price=550_000)
    fixed_today(D(2026, 12, 2))
    check_world(world, s, g, D(2026, 12, 2), 231_000, "within_term_231000", rows=[row("Декабрь", 231_000, "part", of=550_000)],
                debt_rest=[], due_rest=[("Декабрь", 319_000)], debt_before=0, due_before=550_000, debt_after=0, due_after=319_000, advance_after=0)


def test_money_rows_a_debt_and_a_sum_within_the_term_together(world, fixed_today):
    # The world of the owner on 03.12: October + November + the 42 000 of September are a debt (1 142 000), December (550 000) is «к оплате» until 05.12.  The payment 550 000: 42 000 close September, 508 000 go to October; left a debt
    # 42 000 + 550 000 = 592 000 and «к оплате» 550 000 (December: its own rest, the debt and the sum within the term are NOT mixed).
    g, s = owner_world(world, fixed_today, D(2026, 12, 3))
    check_world(world, s, g, D(2026, 12, 3), 550_000, "debt_and_due_550000", rows=[row("Сентябрь, остаток", 42_000, "closed"), row("Октябрь", 508_000, "part", of=550_000)],
                debt_rest=[("Октябрь", 42_000), ("Ноябрь", 550_000)], due_rest=[("Декабрь", 550_000)], debt_before=1_142_000, due_before=550_000, debt_after=592_000, due_after=550_000, advance_after=0)


def test_money_rows_a_year_that_is_not_this_year_is_in_the_name_of_the_month(world, fixed_today):
    # The student since 01.12.2026, no payments; 10.02.2027: December 2026, January, February 2027 = 3 x 550 000 = 1 650 000 of debt.  The payment 1 000 000: «Декабрь 2026» 550 000 closed (the year is not this one), «Январь» 450 000 of 550 000 (this year: no year);
    # left: January 100 000 + February 550 000 = 650 000.
    g, s = om_at(world, fixed_today, D(2026, 12, 1), price=550_000)
    fixed_today(D(2027, 2, 10))
    check_world(world, s, g, D(2027, 2, 10), 1_000_000, "other_year_1000000", rows=[row("Декабрь 2026", 550_000, "closed"), row("Январь", 450_000, "part", of=550_000)],
                debt_rest=[("Январь", 100_000), ("Февраль", 550_000)], due_rest=[], debt_before=1_650_000, due_before=0, debt_after=650_000, due_after=0, advance_after=0)


def test_money_rows_the_personal_period_is_named_by_dates(world, fixed_today):
    # Personal period 420 000 for 30 days; joined 15.10, paid 100 000 that day.  Periods: 15.10 - 14.11 (420 000, paid 100 000, 320 000 left), 15.11 - 14.12 (420 000, begun 15.11; its term of five days has passed on 20.11: all of it is a debt):
    # 20.11 the debt is 320 000 + 420 000 = 740 000.  The payment 500 000: 320 000 close the first period («15.10–14.11, остаток», it was paid in part before), 180 000 go to the second («15.11–14.12»: 180 000 of 420 000).  Left: 420 000 - 180 000 = 240 000.
    g, s = lp(world, fixed_today, joined=D(2026, 10, 15))
    pay(world, s, g, amount=100_000, paid_on=D(2026, 10, 15))
    fixed_today(D(2026, 11, 20))
    check_world(world, s, g, D(2026, 11, 20), 500_000, "personal_500000", rows=[row("15.10–14.11, остаток", 320_000, "closed"), row("15.11–14.12", 180_000, "part", of=420_000)],
                debt_rest=[("15.11–14.12", 240_000)], due_rest=[], debt_before=740_000, due_before=0, debt_after=240_000, due_after=0, advance_after=0)


def test_money_rows_a_personal_period_that_is_a_calendar_month_is_still_named_by_dates(world, fixed_today):
    # Personal period 420 000, joined 01.11 (the period is 01.11 - 30.11, the same days as the month of November, but it is NOT the «Общий месяц»: the name is dates).  20.11: 420 000 of debt.  The payment 200 000: 200 000 of 420 000; left 220 000.
    g, s = lp(world, fixed_today)
    fixed_today(D(2026, 11, 20))
    check_world(world, s, g, D(2026, 11, 20), 200_000, "personal_month_200000", rows=[row("01.11–30.11", 200_000, "part", of=420_000)],
                debt_rest=[("01.11–30.11", 220_000)], due_rest=[], debt_before=420_000, due_before=0, debt_after=220_000, due_after=0, advance_after=0)


def packages_world(world, fixed_today):
    # Packages of 8 lessons, 400 000 (50 000 a lesson); the student since 01.10; held 01, 02, 06, 08, 09, 13, 15, 16, 20.10 = 9 lessons: package 1 is whole (400 000), package 2 has one lesson (50 000): 450 000 unpaid.  On 21.10 the lessons of the last five days (16.10 of package 1 and 20.10 of package 2 = 2 x 50 000) are «к оплате», the other seven of package 1 (350 000) are a debt.
    g, s = centre_of(world, fixed_today)
    hold_lessons(world, s, days(D(2026, 10, 1), D(2026, 10, 20)), group=g)
    fixed_today(D(2026, 10, 21))
    return g, s


def test_money_rows_packages_a_part_of_the_first_package(world, fixed_today):
    # The payment 300 000: package 1 gets 300 000 of 400 000 (part, the oldest lessons first: six lessons); left: package 1 two lessons (15.10 a debt 50 000, 16.10 «к оплате» 50 000) + package 2 (20.10) «к оплате» 50 000.
    g, s = packages_world(world, fixed_today)
    check_world(world, s, g, D(2026, 10, 21), 300_000, "packages_300000", rows=[row("Пакет № 1", 300_000, "part", of=400_000)],
                debt_rest=[("Пакет № 1", 50_000)], due_rest=[("Пакет № 1", 50_000), ("Пакет № 2", 50_000)], debt_before=350_000, due_before=100_000, debt_after=50_000, due_after=100_000, advance_after=0)


def test_money_rows_packages_two_closed_packages_fold_and_the_rest_is_the_advance_of_the_next_package(world, fixed_today):
    # The payment 700 000: package 1 (400 000) and package 2 (50 000, one lesson) are closed in a row: ONE line «Пакеты № 1–2» 450 000 (periods 2); the other 250 000 are an advance (five lessons of the next package).
    g, s = packages_world(world, fixed_today)
    check_world(world, s, g, D(2026, 10, 21), 700_000, "packages_700000", rows=[row("Пакеты № 1–2", 450_000, "closed", periods=2), row("Аванс: уроки следующего пакета", 250_000, "advance")],
                debt_rest=[], due_rest=[], debt_before=350_000, due_before=100_000, debt_after=0, due_after=0, advance_after=250_000)
