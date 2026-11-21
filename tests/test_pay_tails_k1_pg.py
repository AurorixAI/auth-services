"""PAY-TAILS А7 п.1 (К1): the capacity of a period is counted for EACH credit separately - the nominal sum minus the pauses that existed when the credit was entered.  PostgreSQL, through the routes of
the product; every number is worked out by hand in the comment.

World (ОМ, as in ``test_pay_tails_money_pg``): 600 000 a month, Tue / Thu / Fri, billing day 1.  The student joined 02.11; on 03.11 a pause 17.11 - 30.11 is entered (six of the twelve November lessons are
charged: 3, 5, 6, 10, 12, 13 = 6 x 50 000 = 300 000, the nominal sum of November stays 600 000).  December is a whole month: 600 000.  Today 03.12: charged 300 000 + 600 000 = 900 000, nothing is paid.
Whatever the order of «forgave the rest of November» and «paid December», the debt ends at 0: the pause was known to both records, November can hold only 300 000 of them."""
from datetime import date

from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)
from tests.test_payments_service_pg import pay
from tests.test_pay_tails_money_pg import check, forgive, led, om

pytestmark = needs_pg
D = date


def with_pause_in_november(w, fixed_today):
    g, s = om(w, fixed_today)                        # clock 03.11
    r = owner(w).post(f"{API}/org/memberships/{membership(w, s, g).id}/pauses", json={"first_day": "2026-11-17", "last_day": "2026-11-30", "reason": "Болезнь"})
    assert r.status_code == 201, r.text
    return g, s


def periods(w, s, g, today):
    L = led(w, s, g, today)
    nov, dec = [x for x in L.segments if x.kind == "period"][:2]
    assert (nov.amount, dec.amount) == (300_000, 600_000), (nov.amount, dec.amount)
    return nov, dec


def test_k1a_forgave_the_rest_of_the_paused_month_then_paid_the_next_one(world, fixed_today):
    # 02.12 debt 900 000 (November 300 000 + December 600 000); forgiven 300 000 -> it lies on November (the oldest, capacity 300 000: the pause is older than the forgiveness).
    # 03.12 paid 600 000: November is full, so all 600 000 are December's.  November: forgiveness 300 000 (used); December: money 600 000.  Debt 0, advance 0.
    # BEFORE the fix (nominal 600 000 for every credit): the payment's first 300 000 filled November's nominal sum, money pushed the forgiveness out of the period (used 0) and December got
    # only 300 000: debt 300 000.
    g, s = with_pause_in_november(world, fixed_today)
    fixed_today(D(2026, 12, 2))
    forgive(world, s, g, 300_000)
    fixed_today(D(2026, 12, 3))
    pay(world, s, g, amount=600_000, paid_on=D(2026, 12, 3))
    check(world, s, g, D(2026, 12, 3), charged=900_000, debt=0, advance=0, used=300_000, granted=300_000)
    nov, dec = periods(world, s, g, D(2026, 12, 3))
    assert (nov.money, nov.forgiven, dec.money, dec.forgiven) == (0, 300_000, 600_000, 0)


def test_k1b_paid_the_next_month_first_then_forgave_the_rest(world, fixed_today):
    # 01.12 paid 600 000: November holds 300 000 of it (capacity), December 300 000.  02.12 forgiven the 300 000 left of December (debt 900 000 - 600 000 = 300 000): December money 300 000 + forgiveness
    # 300 000.  Debt 0, advance 0, used 300 000.
    g, s = with_pause_in_november(world, fixed_today)
    fixed_today(D(2026, 12, 1))
    pay(world, s, g, amount=600_000, paid_on=D(2026, 12, 1))
    fixed_today(D(2026, 12, 2))
    forgive(world, s, g, 300_000)
    check(world, s, g, D(2026, 12, 2), charged=900_000, debt=0, advance=0, used=300_000, granted=300_000)
    nov, dec = periods(world, s, g, D(2026, 12, 2))
    assert (nov.money, nov.forgiven, dec.money, dec.forgiven) == (300_000, 0, 300_000, 300_000)


def test_k1c_partly_paid_and_partly_forgiven_then_paid_the_next_month(world, fixed_today):
    # 01.12 paid 100 000, 02.12 forgiven 200 000: November is full (money 100 000 + forgiveness 200 000 = 300 000, its capacity).  03.12 paid 600 000: all of it is December's.  Debt 0, advance 0, used 200 000.
    # BEFORE the fix: the 600 000 started inside November's nominal 600 000 (300 000 of it pushed the forgiveness out) and December got 300 000 + the 100 000 that November could not hold: debt 200 000.
    g, s = with_pause_in_november(world, fixed_today)
    fixed_today(D(2026, 12, 1))
    pay(world, s, g, amount=100_000, paid_on=D(2026, 12, 1))
    fixed_today(D(2026, 12, 2))
    forgive(world, s, g, 200_000)
    fixed_today(D(2026, 12, 3))
    pay(world, s, g, amount=600_000, paid_on=D(2026, 12, 3))
    check(world, s, g, D(2026, 12, 3), charged=900_000, debt=0, advance=0, used=200_000, granted=200_000)
    nov, dec = periods(world, s, g, D(2026, 12, 3))
    assert (nov.money, nov.forgiven, dec.money, dec.forgiven) == (100_000, 200_000, 600_000, 0)
