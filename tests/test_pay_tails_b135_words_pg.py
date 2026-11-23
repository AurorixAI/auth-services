"""PAY-TAILS Б11.21 (step 8.13.5): the words of the server - a sentence is made of the numbers it stands next to (LESSONS 168).  PostgreSQL, through the routes of the product; every number is worked out by hand in the comment."""
from datetime import date

from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)
from tests.test_pay_tails_money_pg import forgive, led
from tests.test_payments_service_pg import pay
from tests.money1b_helpers import add

pytestmark = needs_pg
D = date


# ---------------------------------------------------------------------------------------------------------------------------- Б11.21 п. 1, step 8.13.5: the sentence of the window of a package payment is made of the numbers of the window
def test_b11_21_p1_the_sentence_of_the_window_of_a_package_payment_says_the_advance_the_window_shows(world, fixed_today):
    # Packages of 8 lessons, 400 000 (50 000 a lesson); the student since 01.10; three lessons held (06, 08, 09.10 = 150 000); 09.10 all the debt is forgiven (150 000, debt 0).  The window of a payment of 400 000 DATED 05.10
    # (the money is older than the forgiveness, so the money pays the three held lessons - the forgiveness is not used - and the other five lessons are an advance): advance 400 000 - 150 000 = 250 000, debt 0,
    # «оплачено уроков 8, осталось 5».  The sentence says «аванс 250 000 сум» = the number of the window (it said 400 000: the whole payment).
    from tests.money1b_helpers import hold_lessons
    from tests.test_pay_tails_b78_pg import centre_of, days

    g, s = centre_of(world, fixed_today)
    hold_lessons(world, s, days(D(2026, 10, 6), D(2026, 10, 9)), group=g)
    fixed_today(D(2026, 10, 9))
    forgive(world, s, g, 150_000, expected_debt=150_000)
    probe = owner(world).get(f"{API}/org/memberships/{membership(world, s, g).id}/payments/preview", params={"amount": 400_000, "paid_on": "2026-10-05"})
    assert probe.status_code == 200, probe.text
    window = probe.json()["result"]
    assert (window["unpaid_after"], window["advance_after"]) == (0, 250_000), window
    assert window["summary_text"] == "Оплачено уроков 8, осталось 5 · аванс 250\u00a0000\u00a0сум", window["summary_text"]
    pay(world, s, g, amount=400_000, paid_on=D(2026, 10, 5))
    after = led(world, s, g, D(2026, 10, 9))
    assert (after.debt, after.advance) == (window["unpaid_after"], window["advance_after"])


# ---------------------------------------------------------------------------------------------------------------------------- Б11.21 п. 2, step 8.13.5: a refund after a transfer that carried NOTHING
def test_b11_21_p2_a_refund_after_a_transfer_that_carried_nothing_says_there_is_nothing_to_return(world, fixed_today):
    # The world of T8 (300 000 a month in A, the student since 02.11): paid 100 000 only; the transfer on 22.11 finds 21 x 10 000 = 210 000 charged - the money 100 000 is all spent: it carries 0, no advance went anywhere.
    # The refund of 100 000 in A is refused with the plain «Возвращать нечего: оплаченных денег сверх начисленного нет» (it said «Аванс ученика перешёл в группу «Бета»» - an advance that was never there).  Guard: the transfer
    # that DID carry money (test_t8_a_refund_in_the_old_group_names_the_group_where_the_advance_went...) keeps its words.
    from tests.test_money2_transfer_a_pg import groups3, joined

    a, b, c = groups3(world, fixed_today, D(2026, 11, 22))
    s0 = world.students[0]
    joined(world, s0, a, D(2026, 11, 2))
    pay(world, s0, a, amount=100_000)
    add(world, s0, group=b, mode="transfer")
    assert led(world, s0, a, D(2026, 11, 22)).transferred_out == 0
    r = owner(world).post(f"{API}/org/memberships/{membership(world, s0, a).id}/refunds", json={"amount": 100_000, "method": "cash", "reason": "Возврат по просьбе"})
    assert r.status_code == 422, r.text
    assert (r.json()["detail"]["code"], r.json()["detail"]["message"]) == ("REFUND_EXCEEDS_ADVANCE", "Возвращать нечего: оплаченных денег сверх начисленного нет")


# ---------------------------------------------------------------------------------------------------------------------------- Б11.21 п. 3, step 8.13.5: the receipt says the advance once
def test_b11_21_p3_the_state_of_the_receipt_says_the_advance_once(world, fixed_today):
    # The student of the personal period (420 000 for 30 days: November 01 - 30.11): paid 420 000 on 01.11 and 98 000 ahead on 20.11.  The account on 20.11: charged 420 000, money 518 000 - an advance of 98 000, and it is the
    # money ahead of the next period (December has not begun).  The receipt of the second payment says the state ONCE: «аванс 98 000 сум, оплачено до 30.11» (it said «… · вперёд оплачено 98 000 сум» after it - the same sum twice).
    from tests.test_pay_tails_money_pg import lp, pay as pay_lp

    g, s = lp(world, fixed_today)
    pay_lp(world, s, g, amount=420_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 20))
    made = pay_lp(world, s, g, amount=98_000, paid_on=D(2026, 11, 20))
    rc = owner(world).get(f"{API}/org/payments/{made['payment']['id'] if 'payment' in made else made['id']}/receipt").json()
    nb = " "
    assert (rc["state_on_issue"]["debt"], rc["state_on_issue"]["advance"]) == (0, 98_000), rc["state_on_issue"]
    assert rc["receipt_state_text"] == f"аванс 98{nb}000{nb}сум, оплачено до 30.11", rc["receipt_state_text"]
