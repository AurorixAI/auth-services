"""PAY-TAILS step 8.14.18 B11.52 (pure ledger): a transfer or a refund takes money only from the payments that EXISTED at its moment - an earlier date, or the same date entered not after it.

The world (the personal period, 420 000 = 14 000 a day; the stay 01.10 - 10.11; the clock 10.11): the period 01.10-30.10 = 30 days = 420 000, the second period is cut by the leaving (the core charged 560 000 in all - the hand count of 574 000 was a slip, it does not enter the checks: only the cut and the net do).
The payment P0 = 700 000 on 29.10: after the charges money is left over; the leaving takes 126 000 of it (net 574 000) on 10.11 at 18:30 (``at(day, minute)`` counts minutes from 03:00 UTC: minute 630 = 13:30 UTC = 18:30 Tashkent).
The evening payment P1 = 50 000 on the same day at minute 960 (the stay is over: it is all advance)."""
from dataclasses import replace

import pytest

from src.auth import billing_ledger as bl
from tests.pay2_ledger_helpers import D, at, credit, flat, make, outflow

DAY = D(2026, 11, 10)
OUT_AT = 630                       # the leaving / the refund of the day


def world(credits, outflows):
    return make(today=DAY, stays=[bl.Stay(D(2026, 10, 1), DAY)], price_at=flat(420_000), credits=credits, outflows=outflows)


P0 = credit("p0", D(2026, 10, 29), 700_000)


@pytest.mark.parametrize("kind", [bl.TRANSFER_OUT, bl.REFUND])
def test_a_payment_entered_after_the_outflow_of_the_same_day_gives_it_nothing(kind):
    # By hand: P0 700 000 - charged 574 000 = 126 000 left over; the outflow takes 126 000 and only P0 existed at its moment: P0 gives 126 000 (net 574 000), P1 gives 0 (net 50 000 = all advance).
    # The old rule (by date only) took the last P0-P1 by the key (date, moment): P1 first: 50 000 from P1 and 76 000 from P0.
    p1 = credit("p1", DAY, 50_000, minutes=960)
    l = bl.run_ledger(world([P0, p1], [outflow("o", DAY, 126_000, kind=kind, minutes=OUT_AT)]))
    cut = l.refunded_from if kind == bl.REFUND else l.transferred_from
    other = l.transferred_from if kind == bl.REFUND else l.refunded_from
    assert cut == {"p0": 126_000, "p1": 0} and other == {"p0": 0, "p1": 0}
    assert l.net_money == {"p0": 574_000, "p1": 50_000} and l.debt == 0


def test_the_receipt_of_the_old_payment_does_not_change_when_a_new_one_is_entered_and_the_new_one_has_one_piece():
    # By hand: P0 alone: net 574 000 = the two periods 420 000 + 154 000, in two pieces.  With P1 the pieces of P0 are the same, and P1 (50 000) is one piece of the advance (the next period).
    out = [outflow("o", DAY, 126_000, kind=bl.TRANSFER_OUT, minutes=OUT_AT)]
    p1 = credit("p1", DAY, 50_000, minutes=960)
    before = bl.run_ledger(world([P0], out))
    after = bl.run_ledger(world([P0, p1], out))
    pieces = lambda l, cid: [(a.segment_id, a.amount) for a in l.allocations if a.credit_id == cid]
    assert pieces(before, "p0") == pieces(after, "p0") and sum(a for _, a in pieces(after, "p0")) == 574_000
    assert len(pieces(after, "p1")) == 1 and pieces(after, "p1")[0][1] == 50_000
    # the window of a payment (the stand-in credit entered at the end of time) says the same for the same day
    probe = bl.Credit("probe", bl.PAYMENT, DAY, bl.PROBE_MOMENT, 50_000)
    window = bl.run_ledger(replace(world([P0], out), credits=(P0, probe)))
    assert window.transferred_from == {"p0": 126_000, "probe": 0}
    assert sum(a for _, a in pieces(window, "probe")) == 50_000


@pytest.mark.parametrize("minute", [100, OUT_AT])
def test_a_payment_entered_before_the_outflow_of_the_same_day_is_taken_last_as_before(minute):
    # By hand: P1 50 000 is entered at minute 100 (or at the very minute of the outflow): it existed -> the outflow 126 000 takes P1 first (50 000), then P0 (76 000); net P0 624 000, P1 0.
    p1 = credit("p1", DAY, 50_000, minutes=minute)
    l = bl.run_ledger(world([P0, p1], [outflow("o", DAY, 126_000, kind=bl.TRANSFER_OUT, minutes=OUT_AT)]))
    assert l.transferred_from == {"p0": 76_000, "p1": 50_000} and l.net_money == {"p0": 624_000, "p1": 0}


def test_a_payment_of_an_earlier_date_is_taken_last_as_before():
    # By hand: P1 50 000 is dated 09.11 (the day before the outflow, entered later in the real time does not matter: the date is earlier): the outflow takes P1 first: 50 000, then P0 76 000.
    p1 = credit("p1", D(2026, 11, 9), 50_000, minutes=960)
    l = bl.run_ledger(world([P0, p1], [outflow("o", DAY, 126_000, kind=bl.TRANSFER_OUT, minutes=OUT_AT)]))
    assert l.transferred_from == {"p0": 76_000, "p1": 50_000} and l.net_money == {"p0": 624_000, "p1": 0}
