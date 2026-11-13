"""Money-1 / G-15 (N17): «Всего к оплате» of a student whose current period is paid exactly is the price of the NEXT period, not the price of today (PostgreSQL).

Numbers: price 500 000 up to 14.11, 600 000 from 15.11; he joined 15.09 and paid 500 000 on 15.09 and on 15.10 - the periods 15.09-14.10 and 15.10-14.11 are paid exactly; today 21.10."""
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, D, fixed_today, give_group_price, membership, owner, seed_payment, settings_block, stay  # noqa: F401

pytestmark = needs_pg


def paid_exactly_world(w, fixed_today, next_price=600_000):
    fixed_today(D(2026, 10, 21))
    settings_block(w, accounting_start_date="2026-09-01")
    give_group_price(w.db, w.group, 500_000, valid_from=D(2026, 1, 1))
    if next_price:
        give_group_price(w.db, w.group, next_price, valid_from=D(2026, 11, 15))
    m = membership(w)
    stay(w, m, D(2026, 9, 15))
    seed_payment(w, m, 500_000, D(2026, 9, 15), receipt_no=1)
    seed_payment(w, m, 500_000, D(2026, 10, 15), receipt_no=2)
    return m


def preview(w, m):
    r = owner(w).get(f"{API}/org/memberships/{m.id}/payments/preview")
    assert r.status_code == 200, r.text
    return r.json()


def test_g15_a_period_paid_exactly_suggests_the_price_of_the_next_period(world, fixed_today):
    m = paid_exactly_world(world, fixed_today)
    body = preview(world, m)
    assert body["due"]["debt"] == 0 and body["suggested_amount"] == 600_000


def test_g15_the_old_quote_says_the_same_number(world, fixed_today):
    m = paid_exactly_world(world, fixed_today)
    q = owner(world).get(f"{API}/org/memberships/{m.id}/payments/quote", params={"paid_on": "2026-10-21", "valid_from": "", "valid_to": ""})
    assert q.status_code == 200 and q.json()["suggested_amount"] == 600_000


def test_g15_without_a_new_price_the_next_period_costs_the_price_of_today(world, fixed_today):
    m = paid_exactly_world(world, fixed_today, next_price=None)
    assert preview(world, m)["suggested_amount"] == 500_000


def test_g15_an_unpaid_current_period_still_suggests_what_is_left_of_it(world, fixed_today):
    fixed_today(D(2026, 10, 21))
    settings_block(world, accounting_start_date="2026-09-01")
    give_group_price(world.db, world.group, 500_000, valid_from=D(2026, 1, 1))
    give_group_price(world.db, world.group, 600_000, valid_from=D(2026, 11, 15))
    m = membership(world)
    stay(world, m, D(2026, 9, 15))
    seed_payment(world, m, 500_000, D(2026, 9, 15), receipt_no=1)
    seed_payment(world, m, 300_000, D(2026, 10, 15), receipt_no=2)
    assert preview(world, m)["suggested_amount"] == 200_000
