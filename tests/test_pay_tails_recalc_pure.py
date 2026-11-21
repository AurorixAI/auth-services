"""PAY-TAILS А6.3 (the deduction for a lesson the centre cancelled) and the pass of ``_place_credits`` that brings money back to a begun, unfilled period - pure ledger, numbers by hand."""
from dataclasses import replace
from datetime import date

from src.auth import billing_ledger as bl
from src.auth import payment_status as ps
from tests.pay2_ledger_helpers import D, credit, flat, make, pause, weekly

FIXED1 = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
TTF = weekly((1, 3, 4))                      # Tue Thu Fri: November 2026 has 12 lessons, 600 000 / 12 = 50 000 a lesson
ON = (bl.RecalcVersion(date.min, True),)


def nov(credits, *, cancels=(D(2026, 11, 12),), today=D(2026, 11, 20)):
    inp = make(today=today, stays=[bl.Stay(D(2026, 11, 1))], price_at=flat(600_000), regimes=[FIXED1], start=D(2026, 10, 1), credits=credits, lesson_days=TTF)
    return bl.run_ledger(replace(inp, centre_cancels=tuple(bl.CentreCancel(d, "l") for d in cancels), recalc_versions=ON))


def entered(cid, on, amount, minutes=0):
    return credit(cid, on, amount, bl.WRITEOFF, minutes=minutes)


def test_c6_paid_300_forgiven_300_and_the_centre_cancels_a_lesson():
    # 600 000 for 12 lessons: the lesson is 50 000 (was 25 000 = (600 000 - 300 000 forgiven) / 12).  Money 300 000 + deduction 50 000 + forgiveness 250 000 = 600 000: used 250 000, advance 0
    L = nov([credit("p", D(2026, 11, 2), 300_000), entered("w", D(2026, 11, 3), 300_000, 1)])
    assert [r.amount for r in L.recalcs] == [50_000]
    assert (L.charged, L.debt, L.advance, L.refundable, L.written_off, L.written_off_granted) == (600_000, 0, 0, 0, 250_000, 300_000) and L.reconciles and not L.flags


def test_c7_paid_in_full_the_deduction_is_money_back():
    # paid 600 000, a lesson cancelled: 50 000 are money the student paid for a lesson that did not take place -> advance 50 000 = refundable (money; as before)
    L = nov([credit("p", D(2026, 11, 2), 600_000)])
    assert (L.debt, L.advance, L.refundable, L.written_off) == (0, 50_000, 50_000, 0) and L.reconciles and not L.flags


def test_a_fully_forgiven_month_and_a_cancelled_lesson_use_550_000_and_give_no_advance():
    L = nov([entered("w", D(2026, 11, 3), 600_000)])
    assert [r.amount for r in L.recalcs] == [50_000]
    assert (L.debt, L.advance, L.refundable, L.written_off, L.written_off_granted) == (0, 0, 0, 550_000, 600_000) and L.reconciles


def test_a_payment_entered_backdated_after_a_forgiveness_pays_the_period_that_the_forgiveness_could_not_reach():
    # А7.3 (а), Б7.7: a payment is entered BACKDATED after a forgiveness.  Personal period from 01.10 (420 000 each).  25.10: October is forgiven, 420 000.  26.10: the cashier enters a payment of
    # 420 000 DATED 01.10 (it paid October after all).  05.11: paid 420 000.  By the order of the dates: the payment of 01.10 closes October; the forgiveness (25.10) is next on the line, but November had
    # not begun on 25.10 (the forgiveness SKIPS it and the periods after it: it burns and does not hold the line); the payment of 05.11 lies on November.  Today 10.11: charged October + November =
    # 840 000, money 840 000, debt 0, the forgiveness is not used (granted 420 000), advance 0 - not 420 000 of "advance" in December.
    inp = make(today=D(2026, 11, 10), stays=[bl.Stay(D(2026, 10, 1))], price_at=flat(420_000), start=D(2026, 9, 1),
               credits=[credit("p1", D(2026, 10, 1), 420_000, minutes=2), entered("w", D(2026, 10, 25), 420_000, 1), credit("p2", D(2026, 11, 5), 420_000, minutes=3)])
    L = bl.run_ledger(inp)
    periods = [s for s in L.segments if s.kind == "period"]
    assert [(s.period_start.isoformat(), s.money, s.forgiven) for s in periods[:3]] == [("2026-10-01", 420_000, 0), ("2026-11-01", 420_000, 0), ("2026-12-01", 0, 0)]
    assert (L.charged, L.debt, L.advance, L.written_off, L.written_off_granted) == (840_000, 0, 0, 0, 420_000) and L.reconciles
    assert sum(s.unpaid for s in L.segments if s.begun) == 0


def test_payments_are_tied_by_the_key_of_the_credit_not_by_the_text_of_its_id_and_a_burnt_forgiveness_does_not_hold_the_line():
    # А7.3 (б), Б7.7: personal period from 01.10 (420 000).  p1 01.10 420 000 -> October.  w: 25.10 forgiven 320 000: November had not begun on 25.10 -> it burns and holds NO room of the line.
    # "zz" 05.11 300 000: November (420 000) -> 300 000.  "aa" 06.11 300 000: November has 120 000 left -> 120 000, and 180 000 are December's (money ahead).  (By the text of the id "aa" would have gone first:
    # "aa" 300 000, "zz" 120 000.)  Today 10.11: charged 840 000 (October, November), money 1 020 000: debt 0, advance 180 000 - money, December has not begun.
    inp = make(today=D(2026, 11, 10), stays=[bl.Stay(D(2026, 10, 1))], price_at=flat(420_000), start=D(2026, 9, 1),
               credits=[credit("p1", D(2026, 10, 1), 420_000, minutes=1), entered("w", D(2026, 10, 25), 320_000, 2), credit("zz", D(2026, 11, 5), 300_000, minutes=3),
                        credit("aa", D(2026, 11, 6), 300_000, minutes=4)])
    L = bl.run_ledger(inp)
    nov, dec = [s for s in L.segments if s.kind == "period" and s.period_start >= D(2026, 11, 1)][:2]
    assert {(a.credit_id, a.amount) for a in L.allocations if a.segment_id == nov.id} == {("zz", 300_000), ("aa", 120_000)}
    assert {(a.credit_id, a.amount) for a in L.allocations if a.segment_id == dec.id} == {("aa", 180_000)}
    assert (L.charged, L.debt, L.advance, L.written_off, L.written_off_granted) == (840_000, 0, 180_000, 0, 320_000) and L.reconciles and not L.flags


def test_a_forgiveness_lies_where_it_had_room_when_it_was_entered_and_a_pause_entered_later_takes_nothing_from_it():
    # А7.3 (в), Б7.7: common month 600 000 from 01.10.  25.09: forgiven 600 000 - October had not begun: it burns.  05.11 paid 300 000, 06.11 forgiven 300 000: both lie on OCTOBER (the oldest unpaid:
    # money 300 000 + forgiveness 300 000 = 600 000).  20.11: a pause 17.11 - 30.11 is entered: November is 6 lessons of 12 = 300 000.  The pause cuts nothing: it touches November, and nothing lies
    # there.  Charged 600 000 + 300 000 = 900 000, covered 600 000: debt 300 000 (November, not paid, not forgiven).  Granted 900 000, used 300 000.
    inp = make(today=D(2026, 11, 25), stays=[bl.Stay(D(2026, 10, 1))], price_at=flat(600_000), regimes=[FIXED1], start=D(2026, 9, 1), lesson_days=TTF,
               credits=[entered("w0", D(2026, 9, 25), 600_000, 0), credit("p", D(2026, 11, 5), 300_000, minutes=1), entered("w2", D(2026, 11, 6), 300_000, 2)])
    L = bl.run_ledger(replace(inp, pauses=(pause(D(2026, 11, 17), D(2026, 11, 30), D(2026, 11, 20)),)))
    octo, nov = [s for s in L.segments if s.kind == "period"][:2]
    assert (octo.amount, octo.money, octo.forgiven, octo.unpaid) == (600_000, 300_000, 300_000, 0)
    assert (nov.amount, nov.money, nov.forgiven, nov.unpaid) == (300_000, 0, 0, 300_000)
    assert (L.charged, L.debt, L.advance, L.written_off, L.written_off_granted) == (900_000, 300_000, 0, 300_000, 900_000) and L.reconciles and not L.flags
    assert L.forgiven_used_by == {"w2": 300_000}


def test_a_forgiveness_of_the_opening_balance_covers_it_even_when_the_debt_day_is_after_the_day_it_was_entered():
    # А7.7: a stay from 01.11 (420 000), the opening balance of the membership says «owed 100 000 on 10.11» (a debt «on a future start day»).  05.11: forgiven 100 000.  The rule «the period had begun by the day
    # of entering» is for periods of the stay; the debt of the opening is old by its nature, so it is covered: used 100 000 (before: 0, the forgiveness burned and the debt stayed).  Today 20.11:
    # charged 100 000 + 420 000 = 520 000, used 100 000, debt 420 000, advance 0.
    inp = make(today=D(2026, 11, 20), stays=[bl.Stay(D(2026, 11, 1))], price_at=flat(420_000), start=D(2026, 10, 1), credits=[entered("w", D(2026, 11, 5), 100_000)])
    L = bl.run_ledger(replace(inp, opening=bl.Opening(debt_amount=100_000, debt_on=D(2026, 11, 10))))
    assert (L.charged, L.debt, L.advance, L.written_off, L.written_off_granted) == (520_000, 420_000, 0, 100_000, 100_000) and L.reconciles and not L.flags


def test_b6_5_the_block_of_the_lines_is_always_lines_and_more():
    # RecalcItem(kind, label, before, after, basis): one period - one line, ``more`` 0; four periods - the first two lines (MAX_LINES = 2) and ``more`` = 4 - 2 = 2; nothing changes - no block; an archive
    # of two groups - the lines of both with the names of the groups, ``more`` summed
    from src.auth import leave_effects as le
    from src.auth import leave_recalc as lr

    def item(label):
        return lr.RecalcItem("charge", label, 600_000, 200_000, "пересчёт по день ухода · 4 урока из 12")

    one = lr.lines_of([item("Ноябрь")])
    assert one["more"] == 0 and [x["text"] for x in one["lines"]] == ["Ноябрь: 600\u00a0000 → 200\u00a0000\u00a0сум"]
    four = lr.lines_of([item(m) for m in ("Ноябрь", "Декабрь", "Январь", "Февраль")])
    assert four["more"] == 2 and [x["label"] for x in four["lines"]] == ["Ноябрь", "Декабрь"]
    assert lr.lines_of([]) is None
    two_groups = le.recalc_of([type("S", (), {"recalc": [item("Ноябрь")], "group_name": "А"})(), type("S", (), {"recalc": [item(m) for m in ("Ноябрь", "Декабрь", "Январь")], "group_name": "Б"})()])
    assert two_groups["more"] == 1 and [x["text"].split(":")[0] for x in two_groups["lines"]] == ["А", "Б", "Б"]
