"""PAY-TAILS step 8.14.15 B11.51 C1' on the routes (PostgreSQL): the window of a payment and the receipt of «Геометрия 8» (the world of ``geometry_8``: the centre cancelled 9, 12, 23 and 26.11, the place of November = 360 000 - 4 x 40 000 =
200 000) say the same on BOTH working dates 25.11 and 26.11.  By hand: 200 000 -> «Ноябрь», closed, nothing owed; 150 000 -> «Ноябрь — 150 000 из 200 000», part, 50 000 left."""
import pytest
from datetime import date as D

from tests.answers_dump import dump
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import fixed_today  # noqa: F401  (fixture)
from tests.test_pay_tails_b1140_pg import geometry_8
from tests.test_pay_tails_money_pg import led
from tests.test_pay_tails_money_rows_pg import probe, receipt_of, row
from tests.test_payments_service_pg import pay

pytestmark = needs_pg


@pytest.mark.parametrize("today", [D(2026, 11, 25), D(2026, 11, 26)])
@pytest.mark.parametrize("amount,rows,debt_after,name", [
    (200_000, [row("Ноябрь", 200_000, "closed")], 0, "payment_preview_future_cancel_closed"),
    (150_000, [row("Ноябрь", 150_000, "part", of=200_000)], 50_000, "payment_preview_future_cancel_part"),
])
def test_the_window_and_the_receipt_say_the_place_of_the_period_on_both_dates(world, fixed_today, today, amount, rows, debt_after, name):
    g, s = geometry_8(world, fixed_today, today)
    assert led(world, s, g, today).debt == 200_000
    window = probe(world, s, g, amount, today)
    if today == D(2026, 11, 25):
        dump(name, window)
    assert window["result"]["money_rows"] == rows and window["result"]["debt_amount_after"] + window["result"]["due_after"] == debt_after, window["result"]
    written = pay(world, s, g, amount=amount, paid_on=today)
    assert receipt_of(world, written)["money_rows"] == rows                      # the receipt of a new payment = the probe
    assert led(world, s, g, today).debt == debt_after
