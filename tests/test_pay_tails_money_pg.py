"""PAY-TAILS part 2 (А2, А6.1-А6.2, А6.6): a forgiveness is not money.  PostgreSQL, through the routes of the product; every number is worked out by hand in the comment.

Worlds.  «Личный период» (ЛП): 420 000 for 30 days, the student joined 01.11, the period is 01.11 - 30.11; leaving on the 21st day = 21 charged days: 420 000 x 21 / 30 = 294 000.
«Общий месяц» (ОМ): 600 000, Tue / Thu / Fri (3, 5, 6, 10, 12, 13, 17, 19, 20, 24, 26, 27 = 12 lessons = 50 000 each); leaving 11.11 after four lessons = 200 000.
Credits of a period are tied to it by their ORDER on the nominal line; inside a period money comes first, the forgiveness covers what is left (what no charge needs is thrown away)."""
from datetime import date

import pytest

from tests.money1b_helpers import account, add, fresh_student, set_schedule, slot
from tests.pg_world import ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_payments_service_pg import pay
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block  # noqa: F401  (fixture)

pytestmark = needs_pg
D = date
TTF = [slot("s2", 2, "2026-01-01", "14:00", "15:00"), slot("s4", 4, "2026-01-01", "14:00", "15:00"), slot("s5", 5, "2026-01-01", "14:00", "15:00")]


def lp(w, fixed_today, name="Личный", price=420_000, joined=D(2026, 11, 1), accounting="2026-10-01"):
    """A group of the personal period and a student who joined on ``joined``; the clock stands on ``joined``."""
    fixed_today(joined)
    settings_block(w, accounting_start_date=accounting, billing_anchor="payment_date")
    g = make_group(w.db, w.org, w.teacher, name)
    give_group_price(w.db, g, price)
    ensure_billing(w, g, price=None)
    s = fresh_student(w, "Ученик ЛП")
    add(w, s, group=g, mode="add")
    return g, s


def om(w, fixed_today, price=600_000, joined=D(2026, 11, 2)):
    fixed_today(joined)
    settings_block(w, accounting_start_date="2026-10-01", billing_anchor="fixed_day", billing_day=1)
    g = make_group(w.db, w.org, w.teacher, "Общий")
    give_group_price(w.db, g, price)
    set_schedule(w, TTF, g)
    ensure_billing(w, g, price=None)
    s = fresh_student(w, "Ученик ОМ")
    add(w, s, group=g, mode="add")
    fixed_today(D(2026, 11, 3))               # the first charged day is the first lesson (Tue 03.11): the period has begun
    return g, s


def forgive(w, s, g, amount=None, *, effective_on=None, expect=201, expected_debt=None):
    body = {"reason": "Договорённость с родителем"}
    body.update({"all": True} if amount is None else {"amount": amount})
    if effective_on:
        body["effective_on"] = effective_on.isoformat()
    if expected_debt is not None:
        body["expected_debt"] = expected_debt
    r = owner(w).post(f"{API}/org/memberships/{membership(w, s, g).id}/writeoffs", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def leave(w, g, s):
    r = owner(w).post(f"{API}/org/groups/{g.id}/students/{s.id}/leave", json={})
    assert r.status_code == 200, r.text


def led(w, s, g, today):
    return account(w, membership(w, s, g).id, today)


def refund(w, s, g, amount, expect=None):
    r = owner(w).post(f"{API}/org/memberships/{membership(w, s, g).id}/refunds", json={"amount": amount, "method": "cash", "reason": "ученик ушёл"})
    assert expect is None or r.status_code == expect, r.text
    return r


def statement(w, s, g):
    r = owner(w).get(f"{API}/org/memberships/{membership(w, s, g).id}/statement")
    assert r.status_code == 200, r.text
    return r.json()


def centre(w):
    r = owner(w).get(f"{API}/org/payments/summary")
    assert r.status_code == 200, r.text
    return r.json()


def check(w, s, g, today, *, charged, debt, advance, used, granted):
    """The ledger, the statement summary and the card of the centre say the same: the advance is money only (Р4)."""
    L = led(w, s, g, today)
    assert (L.charged, L.debt, L.advance, L.refundable, L.written_off, L.written_off_granted) == (charged, debt, advance, advance, used, granted), L
    assert L.reconciles and not L.flags
    sm = statement(w, s, g)["summary"]
    assert (sm["charged"], sm["advance"], sm["debt_amount"] + sm["due_amount"], sm["reconciles"]) == (charged, advance, debt, True)      # unpaid = debt + «к оплате»
    assert centre(w)["advance_total"] == advance
    inc = owner(w).get(f"{API}/org/payments/income", params={"from": "2026-10-01", "to": "2026-12-31"})                    # «Поступления»: the strip never counts more advance than the centre has (И4)
    assert inc.status_code == 200, inc.text
    body = inc.json()
    assert body["current"]["to_advance"] <= body["position"]["advance_total"] and all(c["left"] == c["right"] for c in body["checks"]), body["checks"]
    return L


# ---------------------------------------------------------------------------------------------------------------------------- С1 - С2'


def test_c1_a_period_forgiven_whole_then_cut_by_a_leaving_gives_no_advance(world, fixed_today):
    # forgiven 420 000 on 01.11 (the whole period), left on the 21st day: charged 294 000; money 0 -> advance 0 (was 126 000, «can't be refunded» but shown); used 294 000 of 420 000
    g, s = lp(world, fixed_today)
    forgive(world, s, g)
    fixed_today(D(2026, 11, 21))
    leave(world, g, s)
    check(world, s, g, D(2026, 11, 21), charged=294_000, debt=0, advance=0, used=294_000, granted=420_000)
    r = refund(world, s, g, 126_000, expect=422)
    assert r.json()["detail"]["code"] == "REFUND_EXCEEDS_ADVANCE" and r.json()["detail"]["refundable"] == 0
    assert owner(world).get(f"{API}/org/payments/balances", params={"kind": "advance"}).json()["total"] == 0


def test_c2_paid_then_forgiven_then_cut_gives_no_cash_back(world, fixed_today):
    # paid 200 000 (01.11), forgiven 220 000 (21.11), left on the 21st day: charged 294 000 > 200 000 + ...: money 200 000, forgiveness covers 94 000 only; advance 0 (was 126 000 and the refund WENT THROUGH)
    g, s = lp(world, fixed_today)
    pay(world, s, g, amount=200_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 21))
    forgive(world, s, g, 220_000)
    leave(world, g, s)
    L = check(world, s, g, D(2026, 11, 21), charged=294_000, debt=0, advance=0, used=94_000, granted=220_000)
    assert L.forgiven_used_by and sum(L.forgiven_used_by.values()) == 94_000
    assert refund(world, s, g, 1, expect=422)


def test_c2b_forgiven_first_then_paid_is_the_same_account(world, fixed_today):
    # forgiven 220 000 on 01.11 (debt 420 000), paid 200 000 on 02.11: both lie on the same period -> the same 94 000 used, advance 0 (before: 126 000 refundable)
    g, s = lp(world, fixed_today)
    forgive(world, s, g, 220_000)
    fixed_today(D(2026, 11, 2))
    pay(world, s, g, amount=200_000, paid_on=D(2026, 11, 2))
    fixed_today(D(2026, 11, 21))
    leave(world, g, s)
    check(world, s, g, D(2026, 11, 21), charged=294_000, debt=0, advance=0, used=94_000, granted=220_000)


def test_c3_common_month_paid_300_forgiven_300_left_after_four_lessons(world, fixed_today):
    # 600 000, left 11.11 after 4 lessons: charged 200 000; money 300 000 -> advance 100 000 (money); the forgiveness 300 000 is not used (the money covers all)
    g, s = om(world, fixed_today)
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 3))
    forgive(world, s, g, 300_000)
    fixed_today(D(2026, 11, 11))
    leave(world, g, s)
    check(world, s, g, D(2026, 11, 11), charged=200_000, debt=0, advance=100_000, used=0, granted=300_000)
    assert refund(world, s, g, 100_000, expect=201)
    # C10: the 100 000 are out; one more sum is refused with the true word
    r = refund(world, s, g, 1, expect=422)
    assert r.json()["detail"]["refundable"] == 0


def test_c4a_forgiveness_of_the_first_period_then_money_of_the_second(world, fixed_today):
    # ЛП from 01.10: October 420 000 forgiven on 31.10; money 420 000 on 05.11; left 21.11 (21st day of the November period).  Order of dates: the forgiveness lies on October, the money on November:
    # charged 420 000 + 294 000 = 714 000; money 420 000 + used 420 000 = 840 000 -> advance 126 000 (money), refundable
    g, s = lp(world, fixed_today, joined=D(2026, 10, 1), accounting="2026-09-01")
    fixed_today(D(2026, 10, 31))
    forgive(world, s, g, 420_000)
    fixed_today(D(2026, 11, 5))
    pay(world, s, g, amount=420_000, paid_on=D(2026, 11, 5))
    fixed_today(D(2026, 11, 21))
    leave(world, g, s)
    check(world, s, g, D(2026, 11, 21), charged=714_000, debt=0, advance=126_000, used=420_000, granted=420_000)
    assert refund(world, s, g, 126_000, expect=201)


def test_c4b_money_first_then_forgiveness_lies_on_the_second_period(world, fixed_today):
    # the same, other dates: money 420 000 on 01.11 (the oldest debt is October), forgiveness 420 000 on 05.11 (the debt of that day is November) -> advance 0, the forgiveness used 294 000
    g, s = lp(world, fixed_today, joined=D(2026, 10, 1), accounting="2026-09-01")
    fixed_today(D(2026, 11, 1))
    pay(world, s, g, amount=420_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 420_000)
    fixed_today(D(2026, 11, 21))
    leave(world, g, s)
    check(world, s, g, D(2026, 11, 21), charged=714_000, debt=0, advance=0, used=294_000, granted=420_000)


def test_c9_leaving_and_returning_the_same_day_gives_the_forgiveness_back(world, fixed_today):
    # С2 and the student comes back the same day: the stay is whole again, charged 420 000, money 200 000 + forgiveness 220 000 (all used), debt 0
    g, s = lp(world, fixed_today)
    pay(world, s, g, amount=200_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 21))
    forgive(world, s, g, 220_000)
    leave(world, g, s)
    add(world, s, group=g, mode="add")
    check(world, s, g, D(2026, 11, 21), charged=420_000, debt=0, advance=0, used=220_000, granted=220_000)


# ---------------------------------------------------------------------------------------------------------------------------- С14 - С21


def test_c14_several_forgivenesses_and_payments_in_one_period(world, fixed_today):
    # forgive 100, pay 100, forgive 100, pay 100 (thousand), debt 20 000 left; left on the 21st day: charged 294 000; money 200 000, forgiveness tied 200 000 -> used 94 000; advance 0
    g, s = lp(world, fixed_today)
    forgive(world, s, g, 100_000)
    pay(world, s, g, amount=100_000, paid_on=D(2026, 11, 1))
    forgive(world, s, g, 100_000)
    pay(world, s, g, amount=100_000, paid_on=D(2026, 11, 1))
    check(world, s, g, D(2026, 11, 1), charged=420_000, debt=20_000, advance=0, used=200_000, granted=200_000)
    # (i) the fifth entry forgives the 20 000 left
    fixed_today(D(2026, 11, 21))
    leave(world, g, s)
    check(world, s, g, D(2026, 11, 21), charged=294_000, debt=0, advance=0, used=94_000, granted=200_000)


def test_c14b_the_rest_forgiven_by_a_fifth_entry(world, fixed_today):
    g, s = lp(world, fixed_today)
    forgive(world, s, g, 100_000)
    pay(world, s, g, amount=100_000, paid_on=D(2026, 11, 1))
    forgive(world, s, g, 100_000)
    pay(world, s, g, amount=100_000, paid_on=D(2026, 11, 1))
    forgive(world, s, g, 20_000)
    fixed_today(D(2026, 11, 21))
    leave(world, g, s)
    check(world, s, g, D(2026, 11, 21), charged=294_000, debt=0, advance=0, used=94_000, granted=220_000)


def test_c15_money_moved_in_from_another_group_is_money(world, fixed_today):
    # A (420 000): joined 01.11, paid 200 000, moved to B on 05.11: A charges 420 000 x 5 / 30 = 70 000, 130 000 move to B as money.  B: period 06.11 - 05.12 (420 000), debt 290 000, forgiven 290 000;
    # left B on 20.11 = 15 days: 420 000 x 15 / 30 = 210 000.  Money 130 000 first, forgiveness covers 80 000: advance 0
    a, s = lp(world, fixed_today, "Группа А")
    pay(world, s, a, amount=200_000, paid_on=D(2026, 11, 1))
    b = make_group(world.db, world.org, world.teacher, "Группа Б")
    give_group_price(world.db, b, 420_000)
    ensure_billing(world, b, price=None)
    fixed_today(D(2026, 11, 5))
    add(world, s, group=b, mode="transfer", from_group_id=str(a.id))
    fixed_today(D(2026, 11, 6))
    mb = led(world, s, b, D(2026, 11, 6))
    assert mb.transferred_in == 130_000 and mb.debt == 290_000
    forgive(world, s, b, 290_000)
    fixed_today(D(2026, 11, 20))
    leave(world, b, s)
    check(world, s, b, D(2026, 11, 20), charged=210_000, debt=0, advance=0, used=80_000, granted=290_000)


def test_c16_void_of_the_unused_part_changes_nothing_void_of_the_used_part_gives_the_debt_back(world, fixed_today):
    # paid 200 000; forgiven 100 000 (a) and 120 000 (b); left on the 21st day: charged 294 000, money 200 000: a used 94 000, b unused
    g, s = lp(world, fixed_today)
    pay(world, s, g, amount=200_000, paid_on=D(2026, 11, 1))
    a = forgive(world, s, g, 100_000)["writeoff"]["id"]
    b = forgive(world, s, g, 120_000)["writeoff"]["id"]
    fixed_today(D(2026, 11, 21))
    leave(world, g, s)
    L = led(world, s, g, D(2026, 11, 21))
    assert (L.debt, L.advance, L.written_off, L.written_off_granted) == (0, 0, 94_000, 220_000)
    assert L.forgiven_used_by == {a: 94_000}                                                 # b was not needed at all
    r = owner(world).post(f"{API}/org/writeoffs/{b}/void", json={"reason": "Ошибка"})
    assert r.status_code == 200, r.text
    check(world, s, g, D(2026, 11, 21), charged=294_000, debt=0, advance=0, used=94_000, granted=100_000)       # the unused one is gone: nothing moved
    r = owner(world).post(f"{API}/org/writeoffs/{a}/void", json={"reason": "Ошибка"})
    assert r.status_code == 200, r.text
    check(world, s, g, D(2026, 11, 21), charged=294_000, debt=94_000, advance=0, used=0, granted=0)               # the used one is gone: 294 000 - 200 000


def test_c17_pause_and_leaving_in_one_period(world, fixed_today):
    # ЛП 01.11; pause 06.11 - 10.11 (the period is extended by 5 days, a pause never adds a charge); left on 21.11: charged days 01-05 (5) + 11-21 (11) = 16: 420 000 x 16 / 30 = 224 000.
    # paid 200 000, forgiven 220 000 -> money 200 000 + used 24 000 = 224 000: advance 0
    g, s = lp(world, fixed_today)
    pay(world, s, g, amount=200_000, paid_on=D(2026, 11, 1))
    forgive(world, s, g, 220_000)
    r = owner(world).post(f"{API}/org/memberships/{membership(world, s, g).id}/pauses", json={"first_day": "2026-11-06", "last_day": "2026-11-10", "reason": "Болезнь"})
    assert r.status_code == 201, r.text
    fixed_today(D(2026, 11, 21))
    leave(world, g, s)
    check(world, s, g, D(2026, 11, 21), charged=224_000, debt=0, advance=0, used=24_000, granted=220_000)


def test_c18_a_forgiveness_dated_back_keeps_the_part_of_the_period_that_began_before_it_was_entered(world, fixed_today):
    # ЛП from 01.10: October 420 000 and November 420 000 unpaid on 05.11 (debt 840 000); forgiven 840 000 ENTERED 05.11 with effective date 15.10: both periods had begun by the day it was entered
    g, s = lp(world, fixed_today, joined=D(2026, 10, 1), accounting="2026-09-01")
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 840_000, effective_on=D(2026, 10, 15))
    check(world, s, g, D(2026, 11, 5), charged=840_000, debt=0, advance=0, used=840_000, granted=840_000)


def test_c19_two_people_forgive_at_once_the_second_is_refused_and_nothing_moves(world, fixed_today):
    g, s = lp(world, fixed_today)
    forgive(world, s, g, 420_000, expected_debt=420_000)
    r = forgive(world, s, g, 420_000, expected_debt=420_000, expect=409)
    assert r["detail"]["code"] in ("BALANCE_CHANGED", "balance_changed")
    check(world, s, g, D(2026, 11, 1), charged=420_000, debt=0, advance=0, used=420_000, granted=420_000)


def test_c20_half_forgiven_then_paid_more_than_the_rest_keeps_the_forgiveness(world, fixed_today):
    # ОМ 600 000: forgiven 300 000 first, then paid 600 000 (300 000 close November, 300 000 go to December).  No truncation: the forgiveness stays 300 000 and is not pushed out by money.
    g, s = om(world, fixed_today)
    forgive(world, s, g, 300_000)
    pay(world, s, g, amount=600_000, paid_on=D(2026, 11, 3))
    L = check(world, s, g, D(2026, 11, 3), charged=600_000, debt=0, advance=300_000, used=300_000, granted=300_000)
    nov, dec = [x for x in L.segments if x.kind == "period"][:2]
    assert (nov.money, nov.forgiven, dec.money, dec.forgiven) == (300_000, 300_000, 300_000, 0)


def test_c21_the_returned_student_of_the_next_month_gets_no_forgiveness_from_the_old_stay(world, fixed_today):
    # С2 (used 94 000 of 220 000), then he returns on 02.12: the unused 126 000 pay nothing of December (before: they flowed on and December was 294 000)
    g, s = lp(world, fixed_today)
    pay(world, s, g, amount=200_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 21))
    forgive(world, s, g, 220_000)
    leave(world, g, s)
    fixed_today(D(2026, 12, 2))
    add(world, s, group=g, mode="add")
    L = led(world, s, g, D(2026, 12, 2))
    assert L.written_off == 94_000 and L.refundable == L.advance == 0 and L.debt == 420_000


def test_c5_a_pause_cuts_the_period_and_lifting_it_gives_the_forgiveness_back(world, fixed_today):
    # ОМ 600 000: paid 300 000, forgiven 300 000.  Pause from the 7th lesson (Tue 17.11) to the end of the month: lessons 3, 5, 6, 10, 12, 13 = 6 of 12 -> charged 300 000: the money covers it, the forgiveness is NOT
    # used (advance 0; before: 300 000 «advance» from the forgiveness).  The pause is lifted: charged 600 000 again, money 300 000 + forgiveness 300 000 (used 300 000).
    g, s = om(world, fixed_today)
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 3))
    forgive(world, s, g, 300_000)
    fixed_today(D(2026, 11, 17))
    r = owner(world).post(f"{API}/org/memberships/{membership(world, s, g).id}/pauses", json={"first_day": "2026-11-17", "last_day": "2026-11-30", "reason": "Болезнь"})
    assert r.status_code == 201, r.text
    check(world, s, g, D(2026, 11, 17), charged=300_000, debt=0, advance=0, used=0, granted=300_000)
    r = owner(world).post(f"{API}/org/pauses/{r.json()['pause']['id']}/void", json={"reason": "Выздоровел"})
    assert r.status_code == 200, r.text
    check(world, s, g, D(2026, 11, 17), charged=600_000, debt=0, advance=0, used=300_000, granted=300_000)


def test_c8_a_transfer_takes_only_money_with_it(world, fixed_today):
    # С2 (paid 200 000, forgiven 220 000, the student moves to B on the 21st day): A charges 294 000, money 200 000 -> nothing is left to carry (the old code carried 126 000: the «advance from the forgiveness»;
    # FACT before: see the report).  B starts with transferred_in 0.
    a, s = lp(world, fixed_today, "Группа А")
    b = make_group(world.db, world.org, world.teacher, "Группа Б")
    give_group_price(world.db, b, 420_000)
    ensure_billing(world, b, price=None)
    pay(world, s, a, amount=200_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 21))
    forgive(world, s, a, 220_000)
    add(world, s, group=b, mode="transfer", from_group_id=str(a.id))
    check(world, s, a, D(2026, 11, 21), charged=294_000, debt=0, advance=0, used=94_000, granted=220_000)
    assert led(world, s, b, D(2026, 11, 22)).transferred_in == 0



def test_o3_the_change_of_the_mode_in_the_middle_of_a_period_is_deferred_by_the_product_and_the_window_shows_no_growth_of_debt(world, fixed_today):
    # А7.11.  The pure ledger keeps the known deviation О3 (a change of the mode that CUTS the running period: debt 210 000 although 220 000 were forgiven).  The product never cuts: a change is saved as a
    # DEFERRED version (the running period goes on, the new way reaches the student on his own switch day), so the window «что если» and the account agree: ЛП 420 000 from 01.11, paid 200 000, forgiven
    # 220 000 on 02.11; on 16.11 the owner asks about the common month: the student switches on 01.12, in the first 30 days he is charged 420 000 -> 420 000, and the sentence has NO «неоплаченная сумма»
    # part (his debt does not grow).  After the real save the account of 16.11 is the same: charged 420 000, debt 0, used 220 000.
    g, s = lp(world, fixed_today)
    pay(world, s, g, amount=200_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 2))
    forgive(world, s, g, 220_000)
    fixed_today(D(2026, 11, 16))
    body = {"period_mode": "calendar", "billing_anchor": "fixed_day", "billing_day": 1}
    r = owner(world).put(f"{API}/org/settings/payments", params={"dry_run": True}, json=body)
    assert r.status_code == 200, r.text
    items = r.json()["regime_impact"]["items"]
    (mine,) = [i for i in items if i["membership_id"] == str(membership(world, s, g).id)]
    assert mine["switch_on"] == "2026-12-01" and (mine["charged_before"], mine["charged_after"]) == (420_000, 420_000)
    assert "неоплаченная сумма" not in mine["text"] and "аванс" not in mine["text"], mine["text"]
    check(world, s, g, D(2026, 11, 16), charged=420_000, debt=0, advance=0, used=220_000, granted=220_000)
    assert owner(world).put(f"{API}/org/settings/payments", json=body).status_code == 200
    check(world, s, g, D(2026, 11, 16), charged=420_000, debt=0, advance=0, used=220_000, granted=220_000)             # saved: the running period is not cut
