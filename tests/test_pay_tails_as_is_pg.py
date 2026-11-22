"""PAY-TAILS Б11.8 п. 10 / Б11.9 п. 4, step 8.9: the tests «as it is» (the behaviour that LEDGER-FREEZE will change - every such test names the number «as it should be») and the paths of the leaving
that no test had through the routes.  PostgreSQL, through the routes of the product; every number is worked out by hand in the comment."""
from datetime import date

from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)
from tests.test_pay_tails_b11_pg import november_with_forgiveness_and_money, om_at
from tests.test_pay_tails_b77_pg import pause_on
from tests.test_pay_tails_money_pg import check, forgive, leave, lp
from tests.test_money2_fix_r3_pg import void_it
from tests.test_payments_service_pg import pay
from tests.money1b_helpers import add

pytestmark = needs_pg
D = date


def chain_2_world(world, fixed_today):
    # the student since 01.11; 02.11 a pause 24 - 30.11 (November 9 lessons = 450 000); 05.11 the whole 450 000 are forgiven; 10.11 the parent pays 600 000 (December).  Advance 600 000, used 450 000.
    return november_with_forgiveness_and_money(world, fixed_today)


def test_taking_a_pause_off_and_entering_a_new_one_is_not_an_edit_as_it_is_now(world, fixed_today):
    # LEDGER-FREEZE (the finding 3, a blocker of the launch): the same facts through two actions.  12.11 the pause 24 - 30.11 is EDITED to 17 - 30.11 (November 300 000, 6 lessons): the forgiveness entered before the
    # edit loses 150 000 and keeps 300 000 (used 300 000), the money is not touched: advance 600 000.  Taken off and entered anew (a new record, a new moment: the old one left the past worlds): used 150 000,
    # advance 450 000, and from 01.12 a debt of 150 000.  AS IT SHOULD BE: the same account in both ways - 600 000 / 300 000 (the property «the same final facts - the same account», in LEDGER-FREEZE).
    g, s = chain_2_world(world, fixed_today)
    fixed_today(D(2026, 11, 12))
    pause = owner(world).get(f"{API}/org/memberships/{membership(world, s, g).id}/pauses").json()[0]["id"]
    assert owner(world).post(f"{API}/org/pauses/{pause}/void", json={"reason": "Поставили не так"}).status_code == 200
    pause_on(world, s, g, "2026-11-17", "2026-11-30")
    check(world, s, g, D(2026, 11, 12), charged=300_000, debt=0, advance=450_000, used=150_000, granted=450_000)       # as it is; should be advance 600 000, used 300 000


def test_the_same_edit_through_the_edit_of_the_pause_gives_the_account_that_the_other_way_should_give(world, fixed_today):
    g, s = chain_2_world(world, fixed_today)
    fixed_today(D(2026, 11, 12))
    pause = owner(world).get(f"{API}/org/memberships/{membership(world, s, g).id}/pauses").json()[0]["id"]
    assert owner(world).patch(f"{API}/org/pauses/{pause}", json={"first_day": "2026-11-17", "reason": "Продлили"}).status_code == 200
    check(world, s, g, D(2026, 11, 12), charged=300_000, debt=0, advance=600_000, used=300_000, granted=450_000)


def test_a_payment_cancelled_and_entered_again_with_the_same_sum_and_date_is_a_new_record_as_it_is_now(world, fixed_today):
    # LEDGER-FREEZE (the warning of the night reconciliation is there too): November 600 000 (no pause yet); 05.11 forgiven 300 000; 10.11 paid 300 000; 12.11 a pause 17 - 30.11 (November 300 000: the money covers it,
    # the forgiveness entered before the pause is cut to nothing: advance 0, used 0).  Then 12.11 the payment is CANCELLED and entered again - the same sum and date (a new record: a NEW moment after the pause, not a
    # correction): the forgiveness is not cut by the pause (it was entered earlier, in the world before it nothing else lay on November) and covers 300 000, the new payment is an advance of 300 000: advance 300 000,
    # used 300 000.  AS IT SHOULD BE: nothing changed - advance 0, used 0 (a correction of the same payment gives exactly that: Б11.1).
    g, s = om_at(world, fixed_today, D(2026, 11, 2))
    fixed_today(D(2026, 11, 5))
    forgive(world, s, g, 300_000)
    fixed_today(D(2026, 11, 10))
    payment = pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 10))["payment"]
    fixed_today(D(2026, 11, 12))
    pause_on(world, s, g, "2026-11-17", "2026-11-30")
    check(world, s, g, D(2026, 11, 12), charged=300_000, debt=0, advance=0, used=0, granted=300_000)
    void_it(world, payment)
    pay(world, s, g, amount=300_000, paid_on=D(2026, 11, 10))
    check(world, s, g, D(2026, 11, 12), charged=300_000, debt=0, advance=300_000, used=300_000, granted=300_000)      # as it is; should be advance 0, used 0


def test_u1_the_leaving_through_the_archive_of_the_student_takes_off_the_future_pause_too(world, fixed_today):
    # The world of У1 (advance 600 000, used 450 000, the pause 24 - 30.11); 20.11 the student is ARCHIVED (the leaving of all his groups): the pause that begins after the day of the leaving is taken off, November
    # 01 - 20.11 is 9 lessons = 450 000, the forgiveness 450 000 stays alive (it was entered when the pause was known), the 600 000 are the advance: advance 600 000, used 450 000, debt 0.
    g, s = november_with_forgiveness_and_money(world, fixed_today)
    fixed_today(D(2026, 11, 20))
    r = owner(world).post(f"{API}/org/students/{s.id}/archive", json={})
    assert r.status_code == 200, r.text
    check(world, s, g, D(2026, 11, 20), charged=450_000, debt=0, advance=600_000, used=450_000, granted=450_000)


def test_u8_in_a_personal_period_forgiven_at_the_settlement_returned_and_paid_the_next_period_the_payment_pays_it(world, fixed_today):
    # Personal period 420 000 for a month; the student since 01.10 (the period 01 - 31.10, 31 days).  09.10 he leaves: 9 days = 420 000 x 9 / 31 = 121 935, rounded to the thousand 122 000.  12.10 the 122 000 are forgiven (debt 0).
    # 01.11 he comes back: a new personal period 01 - 30.11 = 420 000.  03.11 the parent pays 420 000: it pays the period of November, the first one is closed by the forgiveness.  Charged 122 000 + 420 000 = 542 000, debt 0,
    # advance 0, used 122 000.
    g, s = lp(world, fixed_today, joined=D(2026, 10, 1), accounting="2026-09-01")
    fixed_today(D(2026, 10, 9))
    leave(world, g, s)
    fixed_today(D(2026, 10, 12))
    forgive(world, s, g, 122_000)
    fixed_today(D(2026, 11, 1))
    add(world, s, group=g, mode="add")
    fixed_today(D(2026, 11, 3))
    pay(world, s, g, amount=420_000, paid_on=D(2026, 11, 3))
    check(world, s, g, D(2026, 11, 3), charged=542_000, debt=0, advance=0, used=122_000, granted=122_000)
