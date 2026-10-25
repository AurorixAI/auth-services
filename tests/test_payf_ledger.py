"""PAY-F part 3 (the pure ledger): the recalculation for the lessons the CENTRE cancelled - scenarios 7, 9, 11-16 of ``PAY_F_SPEC.md``.

The deduction is a credit that ``run_ledger`` derives from the facts (``centre_cancels`` + the setting's versions); nothing is stored.  Every account here also
checks the invariant ``reconciles``.  The kinds (a holiday, a move, an old cancellation) are filtered where the facts are loaded (``tests/test_payf_pg.py``): a lesson
that is not in ``centre_cancels`` never costs a sum - the scenarios 8 and 10 are asserted there against the real tables."""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.auth import billing_ledger as bl
from tests.pay2_ledger_helpers import D, credit, flat, make, weekly

MWF = weekly((0, 2, 4))              # Mon Wed Fri
MON_FRI = weekly((0, 1, 2, 3, 4))
ON = (bl.RecalcVersion(date.min, True),)


def own_month(*, cancels=(), versions=ON, paid=600_000, stays=None, pauses=(), today=D(2026, 10, 25), price=600_000, lesson_days=MWF):
    """«Свой месяц у каждого ученика» (payment_date): joined 06.10, the period 06.10-05.11 has 13 Mon/Wed/Fri lessons."""
    inp = make(today=today, stays=stays or [bl.Stay(D(2026, 10, 6))], price_at=flat(price, D(2026, 1, 1)), start=D(2026, 9, 1),
               credits=[credit("p1", D(2026, 10, 6), paid)] if paid else [], lesson_days=lesson_days)
    return replace(inp, centre_cancels=tuple(bl.CentreCancel(d, "l") for d in cancels), recalc_versions=tuple(versions), pauses=tuple(pauses))


def test_the_period_has_13_lessons_by_the_schedule():
    assert len(MWF(D(2026, 10, 6), D(2026, 11, 5))) == 13


# ---- 7. «Вычитать»: -round_1000(600 000 / 13) = -46 000 ----------------------------------------------------------------------------------


def test_7_a_lesson_cancelled_by_the_centre_costs_46000_less_and_the_statement_has_the_line():
    base = bl.run_ledger(own_month(cancels=(), paid=0))
    led = bl.run_ledger(own_month(cancels=(D(2026, 10, 21),), paid=0))
    assert base.debt == 600_000 and led.debt == 554_000          # unpaid: the debt falls by the price of one lesson
    assert [(l.day, l.amount) for l in led.recalcs] == [(D(2026, 10, 21), 46_000)] and led.recalculated == 46_000
    assert 46_000 == bl.round_1000(600_000, 13)
    assert led.reconciles and led.charged == 600_000                # the charge itself is untouched: the deduction is a credit
    assert [e.kind for e in led.statement if e.kind == "recalc"] == ["recalc"]
    (event,) = [e for e in led.statement if e.kind == "recalc"]
    assert event.day == D(2026, 10, 21) and event.credit == 46_000 and event.balance_after == -554_000
    assert led.paid == 0 and led.written_off == 0                  # it is neither a payment nor a write-off


def test_7_paid_in_full_the_deduction_is_an_advance_and_the_next_payment_is_smaller():
    led = bl.run_ledger(own_month(cancels=(D(2026, 10, 21),), paid=600_000))
    assert led.debt == 0 and led.advance == 46_000 and led.reconciles
    assert led.refundable == 46_000                                 # the student paid cash for a lesson that did not happen: if he leaves, that is what comes back
    before, after = bl.ledger_with_extra_credit(own_month(cancels=(D(2026, 10, 21),), paid=600_000, today=D(2026, 11, 6)), credit("new", D(2026, 11, 6), 554_000))
    assert before.debt == 554_000                                   # the second period (600 000) minus the 46 000 deduction
    assert after.debt == 0 and after.reconciles


def test_7_the_deduction_comes_off_whatever_the_order_of_the_payments():
    cancels = (D(2026, 10, 21), D(2026, 10, 23))
    for paid_on in (D(2026, 10, 6), D(2026, 10, 22), D(2026, 10, 24)):
        inp = own_month(cancels=cancels, paid=0)
        inp = replace(inp, credits=(credit("p", paid_on, 300_000),))
        led = bl.run_ledger(inp)
        assert led.recalculated == 92_000 and led.debt == 600_000 - 92_000 - 300_000 and led.reconciles


# ---- 9. «Не пересчитывать» ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("versions", [(), (bl.RecalcVersion(date.min, False),)])
def test_9_off_means_no_line(versions):
    led = bl.run_ledger(own_month(cancels=(D(2026, 10, 21),), versions=versions, paid=0))
    base = bl.run_ledger(own_month(cancels=(), versions=versions, paid=0))
    assert led.recalcs == () and led.recalculated == 0 and led.debt == base.debt == 600_000 and led.reconciles
    assert [e.kind for e in led.statement] == [e.kind for e in base.statement]


# ---- 11. paused that day / enrolled later / left earlier ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("name,stays,pauses", [
    ("on a pause", None, [bl.Pause(D(2026, 10, 19), D(2026, 10, 23))]),
    ("enrolled later", [bl.Stay(D(2026, 10, 22))], ()),
    ("left earlier", [bl.Stay(D(2026, 10, 6), D(2026, 10, 20))], ()),
])
def test_11_a_student_who_was_not_charged_that_day_gets_nothing(name, stays, pauses):
    led = bl.run_ledger(own_month(cancels=(D(2026, 10, 21),), stays=stays, pauses=pauses, paid=0))
    assert led.recalcs == () and led.reconciles, name


def test_11_the_day_before_the_pause_still_counts():
    led = bl.run_ledger(own_month(cancels=(D(2026, 10, 16),), pauses=[bl.Pause(D(2026, 10, 19), D(2026, 10, 23))], paid=0))
    assert len(led.recalcs) == 1 and led.reconciles


# ---- 12. the cancellation is taken back -> the line is gone (a function of the facts) -----------------------------------------------------------


def test_12_without_the_cancelled_lesson_the_line_disappears_by_itself():
    with_it = bl.run_ledger(own_month(cancels=(D(2026, 10, 21),), paid=300_000))
    without = bl.run_ledger(own_month(cancels=(), paid=300_000))
    assert with_it.recalculated == 46_000 and without.recalculated == 0
    assert without.debt == with_it.debt + 46_000
    assert not [e for e in without.statement if e.kind == "recalc"]


# ---- 13. «Включили 25.10»: the lesson of 20.10 is not recalculated ------------------------------------------------------------------------------


def test_13_the_setting_acts_from_the_day_it_was_switched_on():
    versions = (bl.RecalcVersion(D(2026, 10, 25), True),)
    led = bl.run_ledger(own_month(cancels=(D(2026, 10, 20), D(2026, 10, 26)), versions=versions, paid=0, today=D(2026, 10, 28)))
    assert [l.day for l in led.recalcs] == [D(2026, 10, 26)] and led.reconciles


def test_13_switched_off_again_the_lessons_after_that_day_are_not_recalculated():
    versions = (bl.RecalcVersion(D(2026, 10, 1), True), bl.RecalcVersion(D(2026, 10, 23), False))
    led = bl.run_ledger(own_month(cancels=(D(2026, 10, 21), D(2026, 10, 26)), versions=versions, paid=0, today=D(2026, 10, 28)))
    assert [l.day for l in led.recalcs] == [D(2026, 10, 21)]
    assert bl.recalc_on(versions, D(2026, 10, 22)) and not bl.recalc_on(versions, D(2026, 10, 23))


# ---- 14. an incomplete first period of the «Общий месяц» (fixed billing day) -------------------------------------------------------------------


def fixed_month(*, cancels, price=660_000, joined=D(2026, 10, 14), versions=ON):
    """billing day 1; October has 22 weekday lessons, the student (joined 14.10) has 13 of them: the charge is price x 13 / 22."""
    inp = make(today=D(2026, 10, 25), stays=[bl.Stay(joined)], price_at=flat(price, D(2026, 1, 1)), start=D(2026, 9, 1),
               regimes=[bl.Regime(mode=bl.CALENDAR, anchor="fixed_day", billing_day=1)], lesson_days=MON_FRI)
    return replace(inp, centre_cancels=tuple(bl.CentreCancel(d, "l") for d in cancels), recalc_versions=tuple(versions))


def test_14_a_partial_first_period_is_price_x_not_cancelled_over_the_template():
    base = bl.run_ledger(fixed_month(cancels=()))
    (seg,) = [s for s in base.segments if s.begun]
    assert (seg.basis, seg.lessons_in_part, seg.lessons_in_period, seg.amount) == ("lessons", 13, 22, 390_000)        # 660 000 x 13 / 22
    led = bl.run_ledger(fixed_month(cancels=(D(2026, 10, 15), D(2026, 10, 20))))
    assert led.recalculated == 2 * 30_000
    assert led.debt == 330_000 == 660_000 * (13 - 2) // 22                    # price x (held lessons - cancelled) / the lessons of the month
    assert led.reconciles and led.charged == 390_000


def test_14_a_lesson_before_the_join_day_of_a_partial_period_costs_nothing():
    led = bl.run_ledger(fixed_month(cancels=(D(2026, 10, 12),)))        # the student came on 14.10: the 12th was not his
    assert led.recalcs == () and led.debt == 390_000


def test_14_the_deductions_of_a_period_never_exceed_its_charge():
    cancels = tuple(MON_FRI(D(2026, 10, 14), D(2026, 10, 31)))
    led = bl.run_ledger(fixed_month(cancels=cancels, price=500_000))
    (seg,) = [s for s in led.segments if s.begun]
    assert led.recalculated <= seg.amount and led.debt >= 0 and led.reconciles


# ---- 15. a package: the cancelled lesson is not charged anyway, the setting does not matter --------------------------------------------------------------


def test_15_the_lessons_mode_ignores_the_setting():
    inp = make(today=D(2026, 10, 25), stays=[bl.Stay(D(2026, 10, 6))], price_at=flat(600_000, D(2026, 1, 1)), start=D(2026, 9, 1),
               regimes=[bl.Regime(mode=bl.LESSONS, package_size=12)], credits=[credit("p", D(2026, 10, 6), 600_000)], lesson_days=MWF,
               counted=[bl.CountedLesson(d, "l") for d in MWF(D(2026, 10, 6), D(2026, 10, 14))])
    off = bl.run_ledger(inp)
    on = bl.run_ledger(replace(inp, centre_cancels=(bl.CentreCancel(D(2026, 10, 21), "l"),), recalc_versions=ON))
    assert on.recalcs == () and on.recalculated == 0
    assert (on.debt, on.advance, on.lessons, on.status) == (off.debt, off.advance, off.lessons, off.status) and on.reconciles


# ---- 16. a transfer in the middle of a period: each group's ledger counts its own lessons while the student is in it -----------------------------


def test_16_a_transfer_splits_the_lessons_by_the_group_they_belong_to():
    transfer = D(2026, 10, 20)
    old = replace(own_month(paid=0, stays=[bl.Stay(D(2026, 10, 6), transfer)]),
                  centre_cancels=(bl.CentreCancel(D(2026, 10, 16), "a"), bl.CentreCancel(D(2026, 10, 21), "a")))
    new = replace(own_month(paid=0, stays=[bl.Stay(D(2026, 10, 21))]),       # the day after the transfer
                  centre_cancels=(bl.CentreCancel(D(2026, 10, 16), "b"), bl.CentreCancel(D(2026, 10, 23), "b")))
    a, b = bl.run_ledger(old), bl.run_ledger(new)
    assert [l.day for l in a.recalcs] == [D(2026, 10, 16)]           # the 21st was after he left group A
    assert [l.day for l in b.recalcs] == [D(2026, 10, 23)]           # the 16th was before he came to group B
    assert a.reconciles and b.reconciles


# ---- the invariant on a sweep -------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("paid", [0, 100_000, 600_000, 1_300_000])
@pytest.mark.parametrize("today", [D(2026, 10, 12), D(2026, 10, 25), D(2026, 11, 20)])
def test_reconciles_everywhere(paid, today):
    cancels = tuple(MWF(D(2026, 10, 6), D(2026, 11, 30)))
    led = bl.run_ledger(own_month(cancels=cancels, paid=paid, today=today))
    assert led.reconciles and led.debt >= 0 and led.advance >= 0 and not (led.debt and led.advance)
    assert led.recalculated == sum(l.amount for l in led.recalcs) and all(l.amount > 0 for l in led.recalcs)
    # a deduction exists only for a period that has begun
    assert all(l.segment_id in {s.id for s in led.segments if s.begun} for l in led.recalcs)


# ---- random accounts: the invariants hold with cancellations, payments, pauses and stays of every shape ----------------------------------------------


def test_random_accounts_keep_the_invariants():
    import random

    rng = random.Random(20261025)
    days = MON_FRI(D(2026, 9, 1), D(2026, 12, 31))
    for case in range(400):
        mode = rng.choice(["payment_date", "fixed_day"])
        joined = D(2026, 9, 1) + timedelta(days=rng.randrange(0, 60))
        left = joined + timedelta(days=rng.randrange(10, 90)) if rng.random() < 0.3 else None
        pauses = [bl.Pause(joined + timedelta(days=a), joined + timedelta(days=a + rng.randrange(0, 12))) for a in (rng.randrange(3, 40),)] if rng.random() < 0.4 else []
        credits = [credit(f"p{i}", D(2026, 9, 1) + timedelta(days=rng.randrange(0, 100)), rng.choice([100_000, 250_000, 600_000, 1_200_000]), minutes=i) for i in range(rng.randrange(0, 4))]
        regimes = [bl.Regime(mode=bl.CALENDAR, anchor=mode, billing_day=rng.choice([1, 10, 28]))]
        inp = make(today=D(2026, 9, 1) + timedelta(days=rng.randrange(20, 110)), stays=[bl.Stay(joined, left)], price_at=flat(rng.choice([450_000, 600_000, 660_000, 999_000]), D(2026, 1, 1)),
                   credits=credits, regimes=regimes, lesson_days=MON_FRI, start=D(2026, 9, 1))
        cancels = tuple(bl.CentreCancel(d, "l") for d in days if rng.random() < 0.25)
        versions = (bl.RecalcVersion(D(2026, 9, 1) + timedelta(days=rng.randrange(0, 60)), True),)
        base = bl.run_ledger(replace(inp, pauses=tuple(pauses)))
        led = bl.run_ledger(replace(inp, pauses=tuple(pauses), centre_cancels=cancels, recalc_versions=versions))
        assert led.reconciles and base.reconciles, case
        assert led.debt >= 0 and led.advance >= 0 and not (led.debt and led.advance), case
        assert led.charged == base.charged and led.paid == base.paid, case                   # the deduction is a credit: charges and payments are the same facts
        assert led.recalculated == sum(l.amount for l in led.recalcs) and led.recalculated <= led.charged, case
        assert len({l.id for l in led.recalcs}) == len(led.recalcs), case
        # the account is never worse for the student: less debt or more advance, by exactly what was deducted (up to the part that fell beyond the charged periods)
        assert (led.advance - led.debt) - (base.advance - base.debt) == led.recalculated, case


# ---- fix round (R3): the horizon, the forgiven part, the state of a segment ------------------------------------------------------------------------


def test_r3_4_the_deduction_reaches_the_next_period_before_it_begins():
    """Paid in full, a lesson cancelled: the 46 000 settles on the NEXT period, which is generated for the money plus the deductions."""
    led = bl.run_ledger(own_month(cancels=(D(2026, 10, 21),), paid=600_000))
    nxt = [s for s in led.segments if not s.begun]
    assert len(nxt) == 1 and nxt[0].recalculated == 46_000 and nxt[0].unpaid == 554_000 and nxt[0].state == "future"
    assert led.debt == 0 and led.advance == 46_000 and led.reconciles
    from src.auth import payments_views as pv

    assert pv.suggested_amount(led, 600_000) == 554_000          # not 600 000


def test_r3_5_a_forgiven_period_gives_no_second_discount():
    forgive = replace(own_month(cancels=(D(2026, 10, 21),), paid=0), credits=(credit("w", D(2026, 10, 22), 600_000, kind=bl.WRITEOFF),))
    led = bl.run_ledger(forgive)
    assert led.recalcs == () and led.debt == 0 and led.advance == 0 and led.refundable == 0 and led.reconciles


def test_r3_5_half_paid_half_forgiven_gets_half_the_lesson_back():
    inp = replace(own_month(cancels=(D(2026, 10, 21),), paid=0),
                  credits=(credit("p", D(2026, 10, 6), 300_000), credit("w", D(2026, 10, 22), 300_000, kind=bl.WRITEOFF, minutes=5)))
    led = bl.run_ledger(inp)
    assert [l.amount for l in led.recalcs] == [bl.round_1000(300_000, 13)] == [23_000]       # the share the student really bore: (600 000 - 300 000) / 13
    assert led.debt == 0 and led.advance == 23_000 and led.refundable == 23_000 and led.reconciles
    # the forgiveness made AFTER the lesson was cancelled lands on the same segment: the same answer
    late = replace(inp, credits=(credit("p", D(2026, 10, 6), 300_000), credit("w", D(2026, 10, 26), 300_000, kind=bl.WRITEOFF, minutes=5)))
    assert bl.run_ledger(late).recalculated == 23_000


def test_r3_15_a_segment_covered_by_the_deduction_counts_as_covered():
    led = bl.run_ledger(own_month(cancels=(D(2026, 10, 21),), paid=600_000, today=D(2026, 11, 6)))
    first, second = [s for s in led.segments if s.kind == "period"][:2]
    assert first.state == "closed" and second.state == "partial" and second.covered_days == 2          # 46 000 of 600 000 on 31 days
    assert led.covered_through == second.days[1]
    records = bl.coverage_records(led)
    assert records and records[-1].valid_to == second.days[1]
