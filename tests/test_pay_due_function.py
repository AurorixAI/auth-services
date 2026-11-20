"""PAY-DUE П1: the pure function ``pay_due.classify`` over a READY ledger (hand-computed numbers).  Periods, packages, the opening balance, pauses, a student who left, a closed participation.
The function never changes the ledger: the same ledger gives ``debt == debt_amount + due_amount`` for every term (the money does not depend on the term)."""
from dataclasses import replace
from datetime import date

import pytest

from src.auth import billing_ledger as bl
from src.auth import pay_due as pd
from src.auth import payment_status as ps

from tests.pay2_ledger_helpers import D, credit, flat, make, weekly

PRICE = 500_000


def personal(today: date, *, pauses=(), credits=(), first: date = D(2026, 10, 15)):
    """«Личный период»: a student from ``first``, the period starts on his first day, 500 000 for the period."""
    inp = make(today=today, stays=[bl.Stay(first)], price_at=flat(PRICE), credits=credits, start=D(2026, 9, 1))
    return replace(inp, pauses=tuple(bl.Pause(a, b) for a, b in pauses))


def run(inp, days=5, lessons=2, in_centre=True, closed=False):
    ledger = bl.run_ledger(inp)
    return ledger, pd.classify(ledger, inp.today, days, lessons, in_centre, closed)


@pytest.mark.parametrize("today,days,name,until,overdue", [
    (D(2026, 10, 15), 5, pd.DUE, D(2026, 10, 19), 0),        # the first day
    (D(2026, 10, 19), 5, pd.DUE, D(2026, 10, 19), 0),        # the last day of the term (inclusive): «до сегодня»
    (D(2026, 10, 20), 5, pd.DEBT, None, 1),                  # D + 1: a debt, overdue 1 day
    (D(2026, 10, 25), 5, pd.DEBT, None, 6),
    (D(2026, 10, 15), 1, pd.DUE, D(2026, 10, 15), 0),        # 1 = the first day itself
    (D(2026, 10, 16), 1, pd.DEBT, None, 1),
    (D(2026, 10, 15), 0, pd.DEBT, None, 1),                  # 0 = a debt from the first day (D = the day before)
    (D(2026, 10, 28), 14, pd.DUE, D(2026, 10, 28), 0),       # the maximum
    (D(2026, 10, 29), 14, pd.DEBT, None, 1),
])
def test_period_term_boundaries(today, days, name, until, overdue):
    ledger, r = run(personal(today), days=days)
    assert ledger.debt == PRICE and r.unpaid == PRICE                      # the money is the same whatever the term
    assert r.name == name and r.due_until == until and r.overdue_days == overdue
    assert (r.debt_amount, r.due_amount) == ((PRICE, 0) if name == pd.DEBT else (0, PRICE))
    assert [p.name for p in r.parts] == [name]


def test_a_pause_inside_the_term_moves_it_and_a_late_pause_can_bring_the_debt_back():
    # days 15, 18, 19, 20, 21: the pause 16-17 moves the 5th day from 19.10 to 21.10
    _, r = run(personal(D(2026, 10, 21), pauses=[(D(2026, 10, 16), D(2026, 10, 17))]))
    assert r.name == pd.DUE and r.due_until == D(2026, 10, 21)
    _, r = run(personal(D(2026, 10, 22), pauses=[(D(2026, 10, 16), D(2026, 10, 17))]))
    assert r.name == pd.DEBT and r.overdue_days == 1
    # the decision (recorded): a pause entered BACK-DATED moves the term again, so a debt may become «к оплате» again (the term is derived from the pauses that exist today)
    assert run(personal(D(2026, 10, 21)))[1].name == pd.DEBT
    assert run(personal(D(2026, 10, 21), pauses=[(D(2026, 10, 16), D(2026, 10, 17))]))[1].name == pd.DUE


def test_overdue_days_do_not_count_the_days_of_a_pause_after_the_term():
    # D = 19.10; the pause 21-22; on 24.10 the days after D are 20, 23, 24 (21-22 are paused) -> 3
    _, r = run(personal(D(2026, 10, 24), pauses=[(D(2026, 10, 21), D(2026, 10, 22))]))
    assert r.name == pd.DEBT and r.overdue_days == 3


def test_partial_payment_before_and_after_the_term():
    pay = credit("p", D(2026, 10, 16), 200_000)
    ledger, r = run(personal(D(2026, 10, 18), credits=[pay]))
    assert ledger.debt == 300_000 and (r.due_amount, r.debt_amount, r.due_until) == (300_000, 0, D(2026, 10, 19))
    ledger, r = run(personal(D(2026, 10, 20), credits=[pay]))
    assert ledger.debt == 300_000 and (r.due_amount, r.debt_amount, r.overdue_days) == (0, 300_000, 1)


def test_a_student_who_left_the_centre_owes_at_once_with_no_overdue_before_the_term():
    ledger, r = run(personal(D(2026, 10, 17)), in_centre=False)
    assert (r.name, r.debt_amount, r.due_amount, r.overdue_days) == (pd.DEBT, PRICE, 0, 0)      # D = 19.10 is ahead: «Долг N» without «просрочено»
    _, r = run(personal(D(2026, 10, 22)), in_centre=False)
    assert r.overdue_days == 3                                                                  # K = today - D


def test_the_opening_balance_is_always_a_debt():
    # the opening debt is settled first (FIFO): it stays a debt whatever the term, the new period beside it is «к оплате»
    inp = replace(personal(D(2026, 10, 16), first=D(2026, 10, 16)), opening=bl.Opening(debt_amount=120_000, debt_on=D(2026, 10, 1)))
    ledger, r = run(inp)
    assert ledger.debt == 120_000 + PRICE
    assert (r.debt_amount, r.due_amount, r.name, r.due_until) == (120_000, PRICE, pd.DEBT, D(2026, 10, 20))
    assert [p.kind for p in r.parts] == ["opening", "period"] and r.overdue_days == 15


def test_an_old_tail_and_a_new_period_in_one_account_are_two_names():
    # 15.10 - 14.11 unpaid, the next period from 15.11; today 15.11, days 5: the old one is a debt (D 19.10), the new one is due until 19.11
    ledger, r = run(personal(D(2026, 11, 15)))
    assert ledger.debt == 2 * PRICE
    assert (r.debt_amount, r.due_amount, r.due_until, r.name) == (PRICE, PRICE, D(2026, 11, 19), pd.DEBT)
    assert [p.name for p in r.parts] == [pd.DEBT, pd.DUE]


def newcomer(today, join):
    """«Общий месяц» (the 1st), lessons Mon / Wed / Fri, 600 000 a month."""
    reg = bl.Regime(anchor=ps.FIXED_DAY, billing_day=1)
    return make(today=today, stays=[bl.Stay(join)], price_at=flat(600_000), regimes=[reg], lesson_days=weekly({0, 2, 4}), start=D(2026, 10, 1))


def test_common_month_newcomer_counts_the_term_from_the_first_lesson_not_from_the_day_of_enrolment():
    assert D(2026, 10, 17).weekday() == 5 and D(2026, 10, 19).weekday() == 0        # Saturday, then Monday
    # enrolled on Saturday 17.10: the first lesson is Monday 19.10; five days -> 23.10 (not 21.10)
    _, r = run(newcomer(D(2026, 10, 22), D(2026, 10, 17)))
    assert (r.name, r.due_until) == (pd.DUE, D(2026, 10, 23))
    _, r = run(newcomer(D(2026, 10, 24), D(2026, 10, 17)))
    assert (r.name, r.overdue_days) == (pd.DEBT, 1)
    # enrolled on the day of a lesson (Monday 19.10): the same first day
    _, r = run(newcomer(D(2026, 10, 23), D(2026, 10, 19)))
    assert (r.name, r.due_until) == (pd.DUE, D(2026, 10, 23))


LESSONS = [D(2026, 9, 25), D(2026, 9, 28), D(2026, 9, 30), D(2026, 10, 2), D(2026, 10, 5), D(2026, 10, 7), D(2026, 10, 9), D(2026, 10, 12), D(2026, 10, 14), D(2026, 10, 16)]


def package(today, held=10, credits=()):
    """A package centre: 12 lessons for 600 000 -> 50 000 a lesson; ``held`` lessons of ``LESSONS`` are held."""
    return make(today=today, stays=[bl.Stay(D(2026, 9, 25))], price_at=flat(600_000), regimes=[bl.Regime(mode=bl.LESSONS, package_size=12)], credits=credits,
                counted=[bl.CountedLesson(d, f"l{i}") for i, d in enumerate(LESSONS[:held])])


def test_packages_the_newest_m_unpaid_lessons_are_due_the_rest_are_a_debt():
    ledger, r = run(package(D(2026, 10, 21)))
    assert ledger.debt == 500_000
    assert (r.due_amount, r.debt_amount) == (100_000, 400_000)                    # 14.10 and 16.10 are due; 8 lessons are a debt
    assert r.due_lessons == {"used": 2, "limit": 2} and r.debt_lessons == 8
    assert r.due_until == D(2026, 10, 28)                                         # the oldest due lesson 14.10 + 14
    # K: the oldest lesson 25.09 became a debt when the 30.09 lesson pushed it out of the two newest (29.09 is the last day) -> 21.10 - 29.09 = 22
    assert r.overdue_days == 22 and r.name == pd.DEBT


def test_packages_a_payment_settles_the_oldest_lesson_first_and_the_sums_move_by_one_lesson():
    ledger, r = run(package(D(2026, 10, 21), credits=[credit("p", D(2026, 10, 20), 50_000)]))
    assert ledger.debt == 450_000 and (r.due_amount, r.debt_amount) == (100_000, 350_000)
    ledger, r = run(package(D(2026, 10, 21), credits=[credit("p", D(2026, 10, 20), 460_000)]))
    assert ledger.debt == 40_000 and (r.due_amount, r.debt_amount) == (40_000, 0) and r.name == pd.DUE   # the nine oldest are paid; part of the lesson of 14.10 is still due


def test_packages_the_fifteenth_day_after_the_lesson_a_lesson_is_a_debt_whatever_m_is():
    _, r = run(package(D(2026, 10, 30)))                                # 16.10 + 14 = 30.10: still due, 14.10 + 14 = 28.10: a debt
    assert r.due_lessons["used"] == 1 and r.due_amount == 50_000 and r.due_until == D(2026, 10, 30)
    _, r = run(package(D(2026, 10, 31)))                                # 15th day after 16.10
    assert r.due_lessons["used"] == 0 and r.due_amount == 0 and r.name == pd.DEBT and r.debt_amount == 500_000
    assert r.overdue_days == 22 + 10                                    # the oldest lesson is still the oldest debt: 31.10 - 29.09


def test_packages_m_zero_and_a_closed_participation_owe_at_once():
    _, r = run(package(D(2026, 10, 21)), lessons=0)
    assert (r.due_amount, r.debt_amount) == (0, 500_000)
    _, r = run(package(D(2026, 10, 21)), closed=True)
    assert (r.due_amount, r.debt_amount) == (0, 500_000)
    _, r = run(package(D(2026, 10, 21)), in_centre=False)
    assert (r.due_amount, r.debt_amount) == (0, 500_000)
    _, r = run(package(D(2026, 10, 21)), lessons=4)
    assert (r.due_amount, r.debt_amount, r.due_lessons) == (200_000, 300_000, {"used": 4, "limit": 4})


def test_nothing_owed_is_nothing():
    _, r = run(personal(D(2026, 10, 16), credits=[credit("p", D(2026, 10, 15), PRICE)]))
    assert r == pd.NOTHING and r.name == pd.NONE


def test_the_function_does_not_touch_the_ledger_and_the_sum_is_the_ledger_debt_for_every_term():
    for make_inp in (lambda: personal(D(2026, 10, 24), credits=[credit("p", D(2026, 10, 16), 200_000)]), lambda: package(D(2026, 10, 21))):
        ledger = bl.run_ledger(make_inp())
        snapshot = (ledger.debt, ledger.charged, ledger.advance, ledger.covered_through, ledger.status, ledger.segments)
        for days in range(0, 15):
            for lessons in range(0, 5):
                for centre in (True, False):
                    r = pd.classify(ledger, make_inp().today, days, lessons, centre)
                    assert r.debt_amount + r.due_amount == ledger.debt
        assert (ledger.debt, ledger.charged, ledger.advance, ledger.covered_through, ledger.status, ledger.segments) == snapshot
