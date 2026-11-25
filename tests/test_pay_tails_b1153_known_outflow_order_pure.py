"""PAY-TAILS step 8.14.19 B11.53 (pure ledger): a DESCRIPTION of the CURRENT behaviour, a KNOWN DEFECT.

Known defect, LEDGER-FREEZE, spec B11.53: a transfer / a refund takes money from the END by the date (then by the moment), so a payment of the SAME DAY entered AFTER the outflow
(evening) gives it money although it did not exist at the outflow's moment.  When the rule is changed (money only from the entries that existed at the outflow's moment) this test
FAILS - then update the receipts, the income reports and the cut of the transfer sum together.

The world (the personal period, 420 000 = 14 000 a day; the stay 01.10 - 10.11; the clock 10.11): P0 = 700 000 on 29.10; the transfer 126 000 at 18:30 of the day D (minute 630);
the payment P1 = 50 000 on the same day D in the evening (minute 960).
By hand (BEFORE the run), the rule «from the end by the date»: the key (date, moment) puts P1 last -> the transfer takes P1 first: 50 000 from P1, the rest 126 000 - 50 000 = 76 000 from P0.
Net money: P0 700 000 - 76 000 = 624 000 (the charges 574 000 stay covered: net of P0 624 000 >= 574 000), P1 50 000 - 50 000 = 0."""
import pytest

from src.auth import billing_ledger as bl
from tests.pay2_ledger_helpers import D, credit, flat, make, outflow

DAY = D(2026, 11, 10)


def test_known_defect_the_evening_payment_of_the_day_gives_the_transfer_money_LEDGER_FREEZE_B1153():
    p0 = credit("p0", D(2026, 10, 29), 700_000)
    p1 = credit("p1", DAY, 50_000, minutes=960)
    inp = make(today=DAY, stays=[bl.Stay(D(2026, 10, 1), DAY)], price_at=flat(420_000), credits=[p0, p1],
               outflows=[outflow("o", DAY, 126_000, kind=bl.TRANSFER_OUT, minutes=630)])
    l = bl.run_ledger(inp)
    assert l.transferred_from == {"p0": 76_000, "p1": 50_000}
    assert l.refunded_from == {"p0": 0, "p1": 0}
    assert l.net_money == {"p0": 624_000, "p1": 0}
