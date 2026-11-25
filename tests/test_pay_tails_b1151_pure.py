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


def _input(today=D(2026, 11, 25)):
    inp = make(today=today, stays=[bl.Stay(D(2026, 11, 1))], price_at=flat(360_000), regimes=[FIXED1], start=D(2026, 10, 1), lesson_days=MT)
    return replace(inp, centre_cancels=tuple(bl.CentreCancel(d, "l") for d in CANCELLED), recalc_versions=ON)


def test_the_generator_property_of_the_layout_is_true_and_counts_what_it_checked():
    # By hand (Геометрия 8 on 25.11, nothing paid): the world A: debt 200 000 (360 000 - 4 x 40 000, the 26.11 counted at once) -> probes 100 000 (part) and 200 000 (closed); the world B = A + the lesson 30.11 cancelled
    # (ahead, entered today): debt 160 000 -> probes 80 000 (part), 160 000 (closed).  Counters: 2 worlds, 1 with a future cancel, 4 probes, 2 part rows, 2 closed rows, 0 rows with the word (nothing lay in before).
    from tests import pay_tails_b7_tools as tools

    start = dict(tools.LAYOUT)
    assert tools.layout_bad(_input()) is False
    got = {k: tools.LAYOUT[k] - start[k] for k in tools.LAYOUT}
    assert got == {"worlds": 2, "with future cancel": 1, "probes": 4, "part rows": 2, "closed rows": 2, "rows with the word": 0, "outflow took the probe": 0}, got


def test_the_generator_property_of_the_layout_has_teeth_the_old_formula_is_red(monkeypatch):
    # By hand: the OLD formula counted only the deductions dated up to the payment: the piece of the payment today 25.11 kept the deductions of 26.11 (and of 30.11 in the world B) in «what is left»: remaining_after + 40 000
    # (+ 80 000 in B).  The shape is right, the sums of the lines too: only the strict property sees it: red in both worlds.
    from tests import pay_tails_b7_tools as tools

    real = bl.run_ledger

    def old(inp):
        L = real(inp)
        ahead = {}
        for line in L.recalcs:
            if line.day > inp.today:
                ahead[line.segment_id] = ahead.get(line.segment_id, 0) + line.amount
        pieces = tuple(replace(a, remaining_after=a.remaining_after + ahead.get(a.segment_id, 0), closes=a.remaining_after + ahead.get(a.segment_id, 0) == 0) if a.credit_id == "layout" else a for a in L.allocations)
        return replace(L, allocations=pieces)

    monkeypatch.setattr(bl, "run_ledger", old)
    assert tools.layout_bad(_input()) is True


def _sum_off(monkeypatch, took, formula_old=False):
    """B11.53: the world of the property (the same two worlds A and B as above) where the lines of every probe sum to 10 000 more than the payment; ``took``: a transfer took 10 000 of the probe (the known class)."""
    from tests import pay_tails_b7_tools as tools

    real, real_rows = bl.run_ledger, pv.money_rows

    def ledger(inp):
        L = real(inp)
        if formula_old:
            ahead = {}
            for line in L.recalcs:
                if line.day > inp.today:
                    ahead[line.segment_id] = ahead.get(line.segment_id, 0) + line.amount
            pieces = tuple(replace(a, remaining_after=a.remaining_after + ahead.get(a.segment_id, 0), closes=a.remaining_after + ahead.get(a.segment_id, 0) == 0) if a.credit_id == "layout" else a for a in L.allocations)
            L = replace(L, allocations=pieces)
        return replace(L, transferred_from={**L.transferred_from, "layout": 10_000}) if took else L

    def rows(L, credit_id, today):
        r = real_rows(L, credit_id, today)
        return [*r, r[-1].model_copy(update={"state": "advance", "label": "Аванс", "amount": 10_000, "periods": 0})] if r else r

    monkeypatch.setattr(bl, "run_ledger", ledger)
    monkeypatch.setattr(pv, "money_rows", rows)
    start = dict(tools.LAYOUT)
    bad = tools.layout_bad(_input())
    return bad, tools.LAYOUT["outflow took the probe"] - start["outflow took the probe"]


def test_the_known_class_sum_off_because_an_outflow_took_the_probe_is_counted_not_red(monkeypatch):
    # By hand: the worlds A and B give 2 probes each (half the debt and the whole debt) = 4 probes; in every one the lines sum to +10 000 and a transfer took 10 000 of the probe -> the reference counter 4, the strict property False.
    assert _sum_off(monkeypatch, took=True) == (False, 4)


def test_the_same_sum_off_with_no_outflow_on_the_probe_stays_red_and_counts_nothing(monkeypatch):
    assert _sum_off(monkeypatch, took=False) == (True, 0)


def test_the_known_class_does_not_hide_the_old_formula_it_stays_red(monkeypatch):
    # The outflow took the probe AND the old «remaining_after» formula: the sum is the exempt class, the shape of the pieces is not -> red; the counter still counts the sum.
    bad, counted = _sum_off(monkeypatch, took=True, formula_old=True)
    assert bad is True and counted == 4


def _paused(forgiven=False):
    from tests.pay2_ledger_helpers import pause

    today = D(2026, 11, 25)
    inp = _input(today)
    if forgiven:
        inp = replace(inp, credits=(credit("w", D(2026, 11, 20), 200_000, bl.WRITEOFF, minutes=1),))
    return inp, replace(inp, pauses=(pause(D(2026, 11, 26), D(2026, 11, 30), today),))


def test_the_generator_identity_of_the_answer_of_a_pause_is_true_and_counts_what_it_checked():
    # By hand («Геометрия 8» on 25.11, the pause 26-30.11): debt 200 000 -> 160 000, charged_delta -80 000, recalc_delta -40 000, forgiven_delta 0: -40 000 = -80 000 + 40 000.  With the debt forgiven (200 000): 0 -> 0,
    # forgiven_delta -40 000: 0 = -80 000 + 40 000 + 40 000.  Counters: 2 events, 2 with a changed deduction, 1 with a changed used forgiveness.
    from tests import pay_tails_b7_tools as tools

    start = dict(tools.IMPACT)
    for forgiven in (False, True):
        assert tools.impact_bad(*_paused(forgiven)) is False
    assert {k: tools.IMPACT[k] - start[k] for k in tools.IMPACT} == {"events": 2, "recalc nonzero": 2, "forgiven nonzero": 1}


def test_the_generator_identity_of_the_answer_of_a_pause_has_teeth_an_answer_without_the_two_fields_is_red(monkeypatch):
    # By hand: the answer of the head f6784a3 had neither field (0 and 0): -40 000 is not -80 000 - 0 - 0: red in both worlds.
    from src.auth import logica_service as lg
    from tests import pay_tails_b7_tools as tools

    real = lg.impact_of
    monkeypatch.setattr(lg, "impact_of", lambda before, after: real(before, after).model_copy(update={"recalc_delta": 0, "forgiven_delta": 0}))
    assert [tools.impact_bad(*_paused(f)) for f in (False, True)] == [True, True]
