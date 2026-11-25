"""PAY-TAILS step 8.14.18 B11.52 on the routes (PostgreSQL): a refund of the morning is taken from the payments that existed at its moment, never from a payment entered in the evening of the same day.

World (the personal period, 420 000 for 30 days, joined 01.11, the clock 05.11): the period 01.11-30.11 is begun = charged 420 000.  P0 = 600 000 (01.11) -> advance 180 000.
Refund of the advance 100 000 on 05.11 (in the morning): only P0 existed -> P0 gives all 100 000, net 500 000, advance 80 000, debt 0.
In the evening of 05.11 P1 = 50 000: the window «Внести оплату» says ONE row «advance 50 000» (nothing is owed: the whole sum is an advance), the receipt of P1 = the window (lesson 164), the account:
advance 80 000 + 50 000 = 130 000, debt 0.  The receipt of P0 is the same before and after the evening payment (its pieces: the period 01.11-30.11 = 420 000 closed, the rest 80 000 = advance).
The old rule (by date) took the 100 000 from P1 first: 50 000 from P1 and 50 000 from P0 - the receipt of P0 changed without a trace and P1 had no piece."""
import pytest

from tests.money1b_helpers import account
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import fixed_today, membership  # noqa: F401  (fixture)
from tests.test_pay_tails_money_pg import lp, refund
from tests.test_pay_tails_money_rows_pg import probe, receipt_of, row
from tests.test_payments_service_pg import pay
from datetime import date as D

pytestmark = needs_pg


def test_a_refund_of_the_morning_is_taken_from_the_old_payment_and_the_evening_payment_keeps_its_piece(world, fixed_today):
    g, s = lp(world, fixed_today, joined=D(2026, 11, 1))
    p0 = pay(world, s, g, amount=600_000, paid_on=D(2026, 11, 1))
    fixed_today(D(2026, 11, 5))
    mid = membership(world, s, g).id
    assert (account(world, mid, D(2026, 11, 5)).advance, account(world, mid, D(2026, 11, 5)).debt) == (180_000, 0)
    refund(world, s, g, 100_000, expect=201)
    first = receipt_of(world, p0)
    after_refund = account(world, mid, D(2026, 11, 5))
    assert (after_refund.advance, after_refund.debt, after_refund.refunded_from) == (80_000, 0, {str(p0["payment"]["id"]): 100_000}), after_refund.refunded_from
    window = probe(world, s, g, 50_000, D(2026, 11, 5))
    assert [r["state"] for r in window["result"]["money_rows"]] == ["advance"] and sum(r["amount"] for r in window["result"]["money_rows"]) == 50_000, window["result"]
    p1 = pay(world, s, g, amount=50_000, paid_on=D(2026, 11, 5))
    ledger = account(world, mid, D(2026, 11, 5))
    assert (ledger.advance, ledger.debt) == (130_000, 0)
    assert ledger.refunded_from == {str(p0["payment"]["id"]): 100_000, str(p1["payment"]["id"]): 0}, ledger.refunded_from       # the evening payment gives the refund nothing
    assert receipt_of(world, p1)["money_rows"] == window["result"]["money_rows"]                                                   # the receipt = the window
    again = receipt_of(world, p0)
    moved = {k for k in first if first[k] != again.get(k)}
    assert moved <= {"state_on_issue", "receipt_state_text", "issued_at"}, moved                                                    # the old receipt: only the live state of the account moves (80 000 -> 130 000 of advance), never its pieces
    assert again["money_rows"] == first["money_rows"]
