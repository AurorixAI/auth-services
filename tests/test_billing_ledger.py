"""PAY-2 stream A: ``billing_ledger.run_ledger`` against the golden fixtures of the design examples E1-E9."""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.auth import billing_ledger as bl
from src.auth import payment_status as ps

from tests.pay2_ledger_helpers import (
    A_PAY, D, TODAY, alloc_tuple, credit, e1, e2, e3, e5, fixture, fixture_alloc_tuple, fixture_seg, flat, make,
    outflow, seg_dict, weekly,
)


def check_preview(fx: dict, inp: bl.LedgerInput, new: bl.Credit):
    """The preview fixture == the engine before / after adding ``new``."""
    before, after = bl.ledger_with_extra_credit(replace(inp, credits=tuple(c for c in inp.credits if c.id != new.id)), new)
    due = fx["due"]
    assert (before.debt, before.past_debt, before.current_due, before.advance) == (
        due["debt"], due["past_debt"], due["current_due"], due["advance"])
    shown = [seg_dict(s) for s in before.segments if s.begun and s.unpaid > 0]
    assert shown == [fixture_seg(d) for d in due["segments"]]
    if before.status.status != ps.NONE:
        assert before.status.paid_until.isoformat() == due["paid_until"] if due["paid_until"] else True
        assert before.status.days_overdue == due["days_overdue"]
    else:
        assert due["paid_until"] is None
    res = fx["result"]
    assert [alloc_tuple(a) for a in after.allocations if a.credit_id == new.id] == [
        fixture_alloc_tuple(a) for a in res["allocations"]]
    assert (after.debt, after.past_debt, after.current_due, after.advance) == (
        res["debt_after"], res["past_debt_after"], res["current_due_after"], res["advance_after"])
    assert after.status.status == res["status_after"]
    assert after.status.days_overdue == res["days_overdue_after"]
    assert after.partial_credit == res["partial_credit_after"]
    if after.mode == bl.CALENDAR:
        assert (after.status.paid_until.isoformat() if after.status.paid_until else None) == res["paid_until_after"]
        assert (before.status.paid_until.isoformat() if before.status.paid_until else None) == res["paid_until_before"]
    else:
        lv = res["lessons"]
        assert (after.lessons.covered, after.lessons.left, after.lessons.unpaid) == (
            lv["covered_after"], lv["left_after"], lv["unpaid_after"])
        assert before.lessons.covered == lv["covered_before"]
    assert after.reconciles and before.reconciles


@pytest.mark.parametrize("amount", [250_000, 300_000, 769_000, 790_000])
def test_e1_owner_example(amount):
    fx = fixture(f"e1_preview_{amount}")
    check_preview(fx, e1(), credit("new", TODAY, amount, minutes=5))


def test_e1_numbers_of_the_design_table():
    """The table of §1A E1, number by number."""
    base = bl.run_ledger(e1())
    assert (base.debt, base.past_debt, base.current_due) == (769_000, 269_000, 500_000)
    assert base.status.paid_until == D(2026, 9, 27) and base.status.days_overdue == 24
    after = bl.run_ledger(e1([credit("p18", TODAY, 250_000, minutes=1)]))
    assert after.status.paid_until == D(2026, 10, 12) and after.status.days_overdue == 9
    assert (after.debt, after.past_debt, after.current_due, after.partial_credit) == (519_000, 19_000, 500_000, 14_334)
    after = bl.run_ledger(e1([credit("p18", TODAY, 300_000, minutes=1)]))
    assert after.status.paid_until == D(2026, 10, 15) and (after.debt, after.partial_credit) == (469_000, 14_871)
    after = bl.run_ledger(e1([credit("p18", TODAY, 790_000, minutes=1)]))
    assert (after.debt, after.advance, after.status.paid_until) == (0, 21_000, D(2026, 11, 15))
    assert after.charged - after.paid == -21_000 == after.debt - after.advance and after.reconciles


def test_e1_statement_matches_the_fixture():
    fx = fixture("e1_statement_790000")
    ledger = bl.run_ledger(e1([credit("p18", TODAY, 790_000, minutes=1)]))
    got = [(e.day.isoformat(), e.kind, e.debit, e.credit, e.balance_after) for e in ledger.statement]
    want = [(r["date"], r["type"], r["debit"], r["credit"], r["balance_after"]) for r in fx["rows"]]
    assert got == want
    assert ledger.statement[-1].balance_after == ledger.advance - ledger.debt
    # the segment details of the charge rows and the upcoming list
    by_id = {s.id: seg_dict(s) for s in ledger.segments}
    for row in fx["rows"]:
        if row["type"] == "charge":
            assert by_id[row["details"]["segment"]["id"]] == fixture_seg(row["details"]["segment"])
    upcoming = [seg_dict(s) for s in ledger.segments if not s.begun and s.money]
    assert upcoming == [fixture_seg(d) for d in fx["upcoming"]]


def test_e1_payment_allocations_of_the_statement():
    fx = fixture("e1_statement_790000")
    ledger = bl.run_ledger(e1([credit("p18", TODAY, 790_000, minutes=1)]))
    for row in fx["rows"]:
        if row["type"] == "payment":
            cid = "p17" if row["details"]["receipt_no"] == 17 else "p18"
            assert [alloc_tuple(a) for a in ledger.allocations if a.credit_id == cid] == [
                fixture_alloc_tuple(a) for a in row["details"]["allocations"]]


def test_e8_replace_is_void_plus_insert():
    """300 000 replaced by 250 000 == the state of the 250 000 preview; nothing to recalculate."""
    with_300 = bl.run_ledger(e1([credit("p18", TODAY, 300_000, minutes=1)]))
    replaced = bl.run_ledger(e1([credit("p19", TODAY, 250_000, minutes=2)]))
    fx = fixture("e8_replace_300000_by_250000")
    assert replaced.debt == fx["allocation"]["debt_after"] == 519_000 and with_300.debt == 469_000
    assert replaced.status.paid_until.isoformat() == fx["allocation"]["paid_until_after"]
    assert [alloc_tuple(a) for a in replaced.allocations if a.credit_id == "p19"] == [
        fixture_alloc_tuple(a) for a in fx["allocation"]["items"]]


def test_e9_price_rise_ahead_reduces_the_days_an_advance_buys():
    fx = fixture("e9_preview_price_ahead_1069000")
    inp = e1(price_at=flat(500_000, changes={D(2026, 11, 1): 600_000}))
    check_preview(fx, inp, credit("new", TODAY, 1_069_000, minutes=5))
    after = bl.run_ledger(replace(inp, credits=inp.credits + (credit("new", TODAY, 1_069_000, minutes=5),)))
    s3 = after.segments[2]
    assert (s3.price.amount, s3.covered_days) == (600_000, 15) and 300_000 * 30 // 500_000 == 18


@pytest.mark.parametrize("amount", [200_000, 323_000, 900_000])
def test_e2_fixed_day_incomplete_first_period_by_lessons(amount):
    fx = fixture(f"e2_preview_{amount}")
    check_preview(fx, e2(), credit("new", D(2026, 10, 21), amount))
    first = bl.run_ledger(e2()).segments[0]
    assert (first.amount, first.basis, first.lessons_in_part, first.lessons_in_period) == (323_000, "lessons", 7, 13)
    assert len(first.days) == 16


@pytest.mark.parametrize("amount", [100_000, 213_000])
def test_e3_gap_between_stays(amount):
    fx = fixture(f"e3_preview_{amount}")
    check_preview(fx, e3(), credit("new", D(2026, 10, 25), amount))
    seg = bl.run_ledger(e3()).segments[0]
    assert (seg.amount, len(seg.days), seg.basis) == (213_000, 22, "days")


def test_e4_transfer_b_side_and_x():
    reg = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
    a = make(today=D(2026, 10, 25), stays=[bl.Stay(D(2026, 10, 1), D(2026, 10, 11))], price_at=flat(500_000),
             regimes=[reg], credits=[credit("p21", D(2026, 10, 1), 500_000)])
    x = bl.refundable_at(a, D(2026, 10, 11))
    assert x == 323_000 and bl.run_ledger(a).charged == 177_000
    # the group being left: the transfer out is an outflow of exactly X
    a_out = replace(a, outflows=(outflow("t1", D(2026, 10, 11), x, bl.TRANSFER_OUT),))
    la = bl.run_ledger(a_out)
    assert (la.debt, la.advance, la.transferred_out) == (0, 0, 323_000) and la.reconciles
    fx = fixture("e4_statement_A")
    assert [(e.day.isoformat(), e.kind, e.balance_after) for e in la.statement] == [
        (r["date"], r["type"], r["balance_after"]) for r in fx["rows"]]
    b = make(today=D(2026, 10, 25), stays=[bl.Stay(D(2026, 10, 11))], price_at=flat(400_000), regimes=[reg],
             floor=D(2026, 10, 12), credits=[credit("tin", D(2026, 10, 11), x, bl.TRANSFER_IN)])
    lb = bl.run_ledger(b)
    fxb = fixture("e4_statement_B")
    assert [(e.day.isoformat(), e.kind, e.balance_after) for e in lb.statement] == [
        (r["date"], r["type"], r["balance_after"]) for r in fxb["rows"]]
    assert (lb.debt, lb.advance, lb.status.paid_until, lb.partial_credit) == (0, 65_000, D(2026, 11, 4), 11_667)
    assert [alloc_tuple(al) for al in lb.allocations] == [fixture_alloc_tuple(al) for al in fxb["rows"][0]["details"]["allocations"]]
    # voiding the 500 000 in A takes X away: B gets a debt (everything is derived)
    a_void = replace(a, credits=())
    assert bl.refundable_at(a_void, D(2026, 10, 11)) == 0
    b_void = replace(b, credits=(credit("tin", D(2026, 10, 11), 0, bl.TRANSFER_IN),)) if False else replace(b, credits=())
    assert bl.run_ledger(b_void).debt == 258_000


@pytest.mark.parametrize("name,held,today,amount", [
    ("e5_preview_630000_held12", 12, D(2026, 10, 21), 630_000),
    ("e5_preview_700000_held14", 14, D(2026, 10, 27), 700_000),
    ("e5_preview_650000_held14", 14, D(2026, 10, 27), 650_000),
])
def test_e5_lessons_packages(name, held, today, amount):
    check_preview(fixture(name), e5(held, today), credit("new", today, amount))


def test_e5_text_cases_of_the_design():
    # 630 000, 12 held: 12 paid, partial 30 000 (advance), left 0; next payment due 600 000 - 30 000
    l = bl.run_ledger(e5(12, D(2026, 10, 21), [credit("p", D(2026, 10, 21), 630_000)]))
    assert (l.lessons.covered, l.lessons.left, l.advance, l.partial_credit) == (12, 0, 30_000, 30_000)
    assert l.status.status == ps.EXPIRED
    # 700 000 / 14 held: all paid, no debt;  650 000: 13 paid, the 14th unpaid, debt 50 000
    l = bl.run_ledger(e5(14, D(2026, 10, 27), [credit("p", D(2026, 10, 27), 700_000)]))
    assert (l.lessons.covered, l.lessons.unpaid, l.debt) == (14, 0, 0)
    l = bl.run_ledger(e5(14, D(2026, 10, 27), [credit("p", D(2026, 10, 27), 650_000)]))
    assert (l.lessons.covered, l.lessons.unpaid, l.debt) == (13, 1, 50_000)
    assert [l.lesson_paid(d.day, d.lesson_id) for d in l.counted] == [True] * 13 + [False]


def test_e6_writeoff():
    inp = make(today=D(2026, 10, 21), stays=[bl.Stay(D(2026, 10, 15))], price_at=flat(500_000),
               credits=[credit("p31", D(2026, 10, 15), 400_000)])
    assert bl.run_ledger(inp).debt == 100_000
    inp = replace(inp, credits=inp.credits + (credit("w1", D(2026, 10, 21), 40_000, bl.WRITEOFF),))
    l = bl.run_ledger(inp)
    fx = fixture("e6_writeoff_40000")
    assert (l.debt, l.written_off) == (60_000, 40_000) == (fx["debt_after"], fx["writeoff"]["amount"])
    assert l.status.paid_until.isoformat() == fx["row"]["paid_until"] and l.partial_credit == fx["row"]["partial_credit"]
    # the student pays the rest: debt 0, I2 holds
    inp = replace(inp, credits=inp.credits + (credit("p32", D(2026, 10, 21), 60_000, minutes=3),))
    l = bl.run_ledger(inp)
    assert (l.debt, l.advance) == (0, 0) and l.reconciles


def test_e7_leaving_and_refund():
    fx = fixture("e7_refund_344000")
    stays = [bl.Stay(D(2026, 9, 15), D(2026, 10, 25))]
    p18 = credit("p18", TODAY, 790_000, minutes=1)
    inp = e1([p18], today=D(2026, 10, 25), stays=stays)
    l = bl.run_ledger(inp)
    assert (l.charged, l.advance, l.refundable) == (677_000, 344_000, 344_000)
    assert l.segments[1].amount == 177_000 and l.segments[1].period_end == D(2026, 10, 25)
    inp2 = replace(inp, outflows=(outflow("r1", D(2026, 10, 25), 344_000),))
    l2 = bl.run_ledger(inp2)
    assert (l2.debt, l2.advance, l2.refunded, l2.reconciles) == (0, 0, 344_000, True)
    assert l2.refunded_from == {"p17": 0, "p18": 344_000} and l2.net_money["p18"] == 446_000
    assert fx["row"]["advance"] == 0 and fx["advance_before"] == l.refundable
    got = [alloc_tuple(a) for a in l2.allocations if a.credit_id == "p18"]
    assert got == [fixture_alloc_tuple(a) for a in fx["row"]["last_payment"]["allocation"]["items"]]
