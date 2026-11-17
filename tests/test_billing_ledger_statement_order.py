"""«Деньги-2», проверка 5 (правило 142 п.5): the lines of ONE day of the statement go in the order they were ENTERED (``created_at``), the charges of the day first,
the pauses last; the balance after a line is the balance the student really had.  Only the order of the lines and their balances change: every other field of the
``Ledger`` stays as it was (the hashes below were taken from the code BEFORE the change)."""
import hashlib
from dataclasses import replace
from datetime import datetime

import pytest

from src.auth import billing_ledger as bl
from src.auth import payment_status as ps
from tests.pay2_ledger_helpers import D, credit, flat, make, outflow

FIXED1 = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
DAY = D(2026, 11, 10)


def _w(**kw):
    """A student in the group since 01.11, a calendar month of 277 000 (full period) - the charge of the period is 277 000."""
    return bl.run_ledger(make(today=D(2026, 11, 20), stays=[bl.Stay(D(2026, 11, 1))], price_at=flat(277_000), regimes=[FIXED1], **kw))


def _rows(ledger, day=DAY):
    return [(e.kind, e.ref_id, e.balance_after) for e in ledger.statement if e.day == day]


def test_transfer_there_and_back_in_one_day_goes_pay_out_in_and_no_923000():
    # charge 277 000 (01.11) -> -277 000; payment 600 000 -> +323 000; "to B" -600 000 -> -277 000; "from B" +600 000 -> +323 000 (the student never had 923 000)
    ledger = _w(credits=[credit("pay", DAY, 600_000, minutes=1), credit("t_in", DAY, 600_000, bl.TRANSFER_IN, minutes=3)],
                outflows=[outflow("t_out", DAY, 600_000, bl.TRANSFER_OUT, minutes=2)])
    assert _rows(ledger) == [(bl.PAYMENT, "pay", 323_000), (bl.TRANSFER_OUT, "t_out", -277_000), (bl.TRANSFER_IN, "t_in", 323_000)]
    assert 923_000 not in [e.balance_after for e in ledger.statement]
    assert ledger.reconciles


def test_a_refund_entered_before_the_payment_of_the_same_day_stands_above_it():
    # the advance 277 000 + 100 000 is paid on 05.11; on 10.11 the refund 50 000 (minute 1) is entered BEFORE the payment 200 000 (minute 2)
    ledger = _w(credits=[credit("early", D(2026, 11, 5), 400_000), credit("pay", DAY, 200_000, minutes=2)],
                outflows=[outflow("ref", DAY, 50_000, bl.REFUND, minutes=1)])
    # balances: 05.11 -277+400 = +123 000; refund -> +73 000; payment -> +273 000 (the old order put the payment first: +323 000 then +273 000)
    assert _rows(ledger) == [(bl.REFUND, "ref", 73_000), (bl.PAYMENT, "pay", 273_000)]


def test_two_payments_of_a_day_go_in_the_order_of_entering_not_by_id():
    # "z" (id after "a") was entered first: 100 000 -> -177 000; then "a" 50 000 -> -127 000 (the old key (day, rank, id) gave a, z)
    ledger = _w(credits=[credit("a", DAY, 50_000, minutes=5), credit("z", DAY, 100_000, minutes=1)])
    assert _rows(ledger) == [(bl.PAYMENT, "z", -177_000), (bl.PAYMENT, "a", -127_000)]


def test_a_forgiveness_entered_before_the_payment_stands_above_it():
    # forgiveness 77 000 (minute 1) -> -200 000, payment 100 000 (minute 2) -> -100 000
    ledger = _w(credits=[credit("w", DAY, 77_000, bl.WRITEOFF, minutes=1), credit("p", DAY, 100_000, minutes=2)])
    assert _rows(ledger) == [(bl.WRITEOFF, "w", -200_000), (bl.PAYMENT, "p", -100_000)]


def test_the_charge_of_the_day_is_first_even_when_the_payment_is_entered_on_the_first_day_of_the_period():
    ledger = _w(credits=[credit("p", D(2026, 11, 1), 277_000, minutes=1)])
    first = _rows(ledger, D(2026, 11, 1))
    assert first == [("charge", first[0][1], -277_000), (bl.PAYMENT, "p", 0)]


def test_the_pause_stays_last_on_its_day():
    inp = make(today=D(2026, 11, 20), stays=[bl.Stay(D(2026, 11, 1))], price_at=flat(277_000), regimes=[FIXED1],
               credits=[credit("p", DAY, 100_000, minutes=1)])
    ledger = bl.run_ledger(replace(inp, pauses=(bl.Pause(DAY, D(2026, 11, 12)),)))
    assert [e.kind for e in ledger.statement if e.day == DAY] == [bl.PAYMENT, "pause"]


def test_the_recalculation_has_no_entering_time_and_stays_right_after_the_charges_of_its_day():
    # a derived credit (kind recalc, created_at = datetime.min): it sorts before every entered line of its day and after the charge
    stmt = bl.run_ledger(make(today=D(2026, 11, 20), stays=[bl.Stay(D(2026, 11, 1))], price_at=flat(277_000), regimes=[FIXED1],
                              credits=[credit("p", D(2026, 11, 1), 100_000, minutes=1)])).statement
    assert [e.kind for e in stmt if e.day == D(2026, 11, 1)] == ["charge", bl.PAYMENT]
    line = bl.RecalcLine("rc", D(2026, 11, 1), "l1", 10_000, "seg")
    assert line.credit().created_at == datetime.min      # the key of the order: first among the money of the day


# ---- the rest of the Ledger does not change (hashes of the code before the change) --------------------------------------------------------------------------

def _scenarios():
    yield "back_and_forth", dict(credits=[credit("pay", DAY, 600_000, minutes=1), credit("t_in", DAY, 600_000, bl.TRANSFER_IN, minutes=3)],
                                 outflows=[outflow("t_out", DAY, 600_000, bl.TRANSFER_OUT, minutes=2)])
    yield "refund_pay", dict(credits=[credit("early", D(2026, 11, 5), 400_000), credit("pay", DAY, 200_000, minutes=2)],
                             outflows=[outflow("ref", DAY, 50_000, bl.REFUND, minutes=1)])
    yield "two_pays", dict(credits=[credit("a", DAY, 50_000, minutes=5), credit("z", DAY, 100_000, minutes=1)])
    yield "writeoff", dict(credits=[credit("w", DAY, 77_000, bl.WRITEOFF, minutes=1), credit("p", DAY, 100_000, minutes=2)])
    yield "first_day", dict(credits=[credit("p", D(2026, 11, 1), 277_000, minutes=1)])
    yield "chain", dict(credits=[credit("p1", D(2026, 11, 2), 700_000), credit("in", DAY, 90_000, bl.TRANSFER_IN, minutes=4), credit("p2", DAY, 30_000, minutes=2)],
                        outflows=[outflow("o1", DAY, 120_000, bl.TRANSFER_OUT, minutes=3), outflow("o2", D(2026, 11, 15), 10_000, bl.REFUND, minutes=1)])


GOLDEN = {"back_and_forth": "03af7157e6d9", "refund_pay": "1b1e9195ae39", "two_pays": "1aa96212efc8", "writeoff": "b582a521d8b2",
          "first_day": "218f36a81295", "chain": "7a687b9018ce"}     # taken from the code before the change


def _digest(kw):
    ledger = _w(**kw)
    return hashlib.sha1(repr(replace(ledger, statement=())).encode()).hexdigest()[:12]


@pytest.mark.parametrize("name,kw", list(_scenarios()))
def test_every_field_but_the_statement_is_unchanged(name, kw):
    assert _digest(kw) == GOLDEN[name]
    ledger = _w(**kw)
    assert ledger.statement[-1].balance_after == ledger.advance - ledger.debt      # the order changes the lines, not the end
