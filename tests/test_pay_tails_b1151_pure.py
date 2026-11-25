"""PAY-TAILS Б11.51 С1' (step 8.14.15): the layout «Куда пойдут деньги» of a payment says what is left of the period the way the ACCOUNT counts it (the deductions for the lessons the centre cancelled, the future ones too, take their place in the period at once), and the word «остаток» is said only when money or a used forgiveness lay into the period before this payment.  Pure: no database.

The world («Геометрия 8»: Mon/Thu, 360 000 a month, 9 lessons of 40 000, the centre cancelled 9, 12, 23 and 26.11, the common month from the 1st, the working dates 25.11 and 26.11):
the place of November = 360 000 - 4 x 40 000 = 200 000 on both dates (the account counts the cancelled 26.11 the day the period begins; ON 25.11 THE OLD LAYOUT COUNTED 3 deductions: «из 240 000»).
Hand figures:
  W1  pay 200 000                         -> «Ноябрь», closed, of None, left 0, debt 0
  W1b pay 150 000                         -> «Ноябрь», part, of 200 000, left 50 000, debt 50 000
  W2  from 25.11 (place 80 000 - 40 000 = 40 000), pay 15 000 -> «Ноябрь», part, of 40 000, left 25 000, debt 25 000
  W3  forgiveness 50 000 (used) + pay 100 000 -> «Ноябрь, остаток», part, of 150 000, left 50 000, debt 50 000
  W4  forgiveness 50 000 + pay 150 000    -> «Ноябрь, остаток», closed, left 0, debt 0
  W5  paid 100 000 on 03.11 + pay 100 000 -> «Ноябрь, остаток», closed (the first piece left 100 000), debt 0
  W6  a payment dated 10.11 of 200 000    -> as W1 (a deduction dated AFTER the payment is in the place all the same)
The money did not move: the pieces (amount, bucket, days) of every payment equal what the head f6784a3 gave (taken before the change)."""
from dataclasses import replace
from datetime import date as D

import pytest

from src.auth import billing_ledger as bl
from src.auth import payment_status as ps
from src.auth import payments_statement_texts as st
from src.auth import payments_views as pv
from src.auth.payments_schemas import StatementDetails, StatementRow
from tests.pay2_ledger_helpers import credit, flat, make, weekly

FIXED1 = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
MT = weekly((0, 3))
ON = (bl.RecalcVersion(D.min, True),)
CANCELLED = [D(2026, 11, d) for d in (9, 12, 23, 26)]
DATES = (D(2026, 11, 25), D(2026, 11, 26))
FORGIVEN = credit("w", D(2026, 11, 20), 50_000, bl.WRITEOFF, minutes=1)
PAID_EARLIER = credit("p0", D(2026, 11, 3), 100_000)


def ledger(today, first, credits=()):
    inp = make(today=today, stays=[bl.Stay(first)], price_at=flat(360_000), regimes=[FIXED1], start=D(2026, 10, 1), credits=credits, lesson_days=MT)
    return bl.run_ledger(replace(inp, centre_cancels=tuple(bl.CentreCancel(d, "l") for d in CANCELLED), recalc_versions=ON))


def probe(today, first, amount, extra=(), on=None):
    c = bl.Credit("probe", bl.PAYMENT, on or today, bl.PROBE_MOMENT, amount)
    return ledger(today, first, [*extra, c])


WORLDS = {
    "W1": (D(2026, 11, 1), 200_000, (), None),
    "W1b": (D(2026, 11, 1), 150_000, (), None),
    "W2": (D(2026, 11, 25), 15_000, (), None),
    "W3": (D(2026, 11, 1), 100_000, (FORGIVEN,), None),
    "W4": (D(2026, 11, 1), 150_000, (FORGIVEN,), None),
    "W5": (D(2026, 11, 1), 100_000, (PAID_EARLIER,), None),
    "W6": (D(2026, 11, 1), 200_000, (), D(2026, 11, 10)),
}
# world -> (label, amount, of, state, closes, left after the payment, debt after): the hand figures above, the same on BOTH dates
EXPECTED = {
    "W1": ("Ноябрь", 200_000, None, "closed", True, 0, 0),
    "W1b": ("Ноябрь", 150_000, 200_000, "part", False, 50_000, 50_000),
    "W2": ("Ноябрь", 15_000, 40_000, "part", False, 25_000, 25_000),
    "W3": ("Ноябрь, остаток", 100_000, 150_000, "part", False, 50_000, 50_000),
    "W4": ("Ноябрь, остаток", 150_000, None, "closed", True, 0, 0),
    "W5": ("Ноябрь, остаток", 100_000, None, "closed", True, 0, 0),
    "W6": ("Ноябрь", 200_000, None, "closed", True, 0, 0),
}
# the pieces (credit, amount, bucket, days) of the head f6784a3, measured BEFORE the change (artifacts/money1-stage/.cache/paytails-r7/base1_head.txt)
HEAD_PIECES = {
    ("W1", D(2026, 11, 25)): [("probe", 200000, "current", 16)],
    ("W1b", D(2026, 11, 25)): [("probe", 150000, "current", 12)],
    ("W2", D(2026, 11, 25)): [("probe", 15000, "current", 1)],
    ("W3", D(2026, 11, 25)): [("probe", 100000, "current", 8), ("w", 50000, "current", 4)],
    ("W4", D(2026, 11, 25)): [("probe", 150000, "current", 12), ("w", 50000, "current", 4)],
    ("W5", D(2026, 11, 25)): [("p0", 100000, "current", 8), ("probe", 100000, "current", 8)],
    ("W6", D(2026, 11, 25)): [("probe", 200000, "current", 17)],
    ("W1", D(2026, 11, 26)): [("probe", 200000, "current", 17)],
    ("W1b", D(2026, 11, 26)): [("probe", 150000, "current", 12)],
    ("W2", D(2026, 11, 26)): [("probe", 15000, "current", 1)],
    ("W3", D(2026, 11, 26)): [("probe", 100000, "current", 8), ("w", 50000, "current", 4)],
    ("W4", D(2026, 11, 26)): [("probe", 150000, "current", 13), ("w", 50000, "current", 4)],
    ("W5", D(2026, 11, 26)): [("p0", 100000, "current", 8), ("probe", 100000, "current", 9)],
    ("W6", D(2026, 11, 26)): [("probe", 200000, "current", 17)],
}


def world(name, today):
    first, amount, extra, on = WORLDS[name]
    return probe(today, first, amount, extra, on)


@pytest.mark.parametrize("today", DATES)
@pytest.mark.parametrize("name", sorted(WORLDS))
def test_the_layout_of_the_payment_says_the_place_of_the_period_the_way_the_account_counts_it_on_both_working_dates(name, today):
    after = world(name, today)
    label, amount, of, state, closes, left, debt = EXPECTED[name]
    rows = pv.money_rows(after, "probe", today)
    assert [(r.label, r.amount, r.of, r.state) for r in rows] == [(label, amount, of, state)], name
    piece = [a for a in after.allocations if a.credit_id == "probe"][-1]
    assert (piece.closes, piece.remaining_after) == (closes, left)
    assert (after.debt, after.advance) == (debt, 0)             # the verdict of the window: «Всё оплачено» (debt 0) / «Останется … долг» (debt > 0)
    assert sum(r.amount for r in rows) == amount                  # the lines add up to the payment


@pytest.mark.parametrize("today", DATES)
@pytest.mark.parametrize("name", sorted(WORLDS))
def test_the_money_did_not_move_the_pieces_and_the_buckets_equal_the_head(name, today):
    after = world(name, today)
    pieces = sorted(((a.credit_id, a.amount, a.bucket, a.days) for a in after.allocations), key=lambda p: (p[0] != "p0", p[0]))
    assert sorted(pieces) == sorted(HEAD_PIECES[(name, today)])


def test_a_payment_of_the_probe_world_w2_reads_the_same_on_the_two_dates_nothing_jumps_on_the_day_of_the_lesson():
    assert [pv.money_rows(world("W2", d), "probe", d)[0].of for d in DATES] == [40_000, 40_000]


def _statement_line(after, today):
    row = StatementRow(seq=1, date=today, type="payment", title="", debit=0, credit=int(after.allocations[-1].amount), outflow=0, balance_after=None,
                       details=StatementDetails(receipt_no=1, method="cash", allocations=pv.allocations_of(after, "probe")))
    entry = [e for e in st.build_entries([row]) if e.allocations][0]
    return entry.allocations


@pytest.mark.parametrize("today", DATES)
def test_the_statement_says_the_month_is_paid_where_the_period_is_closed_and_how_much_is_left_where_it_is_not(today):
    assert _statement_line(world("W1", today), today) == ["за ноябрь — 200\xa0000 (ноябрь оплачен)"]      # the old layout said «(осталось 40 000)» on 25.11
    assert _statement_line(world("W1b", today), today) == ["за ноябрь — 150\xa0000 (осталось 50\xa0000)"]
    assert _statement_line(world("W2", today), today) == ["за ноябрь — 15\xa0000 (осталось 25\xa0000)"]
