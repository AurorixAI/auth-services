"""PAY-F item 17 (owner, 25.10): a PARTIAL period of the «Общий месяц» is counted by the REGULAR schedule only - numerator and denominator - from ``REGULAR_SCHEDULE_FROM``.

The extra one-off lessons are in neither; a lesson the centre cancelled stays in the numerator (LOGIC-A S-4 №3); a period that starts before the cut-over is computed the old
way.  «Свой месяц» (days) and a package are not touched."""
from dataclasses import replace
from datetime import date, datetime

import pytest

from src.auth import billing_ledger as bl
from tests.pay2_ledger_helpers import D, credit, flat, make

CUT = bl.REGULAR_SCHEDULE_FROM


def month_lessons(start: date):
    """The month of ``start`` (its 1st): 13 regular lessons - 6 before the 15th, 7 from it - and 2 extra one-off lessons before the 15th."""
    regular = [start.replace(day=d) for d in (2, 3, 6, 7, 9, 10)] + [start.replace(day=d) for d in (15, 16, 19, 20, 22, 23, 26)]
    extra = [start.replace(day=4), start.replace(day=5)]
    return regular, extra


def fixed_month(month_start: date, *, regular_from=CUT, joined_day=15):
    regular, extra = month_lessons(month_start)
    every = sorted(regular + extra)

    def within(days):
        return lambda a, b: [d for d in days if a <= d <= b]

    inp = make(today=month_start.replace(day=25), stays=[bl.Stay(month_start.replace(day=joined_day))], price_at=flat(600_000, D(2026, 1, 1)), start=D(2026, 9, 1),
               regimes=[bl.Regime(mode=bl.CALENDAR, anchor="fixed_day", billing_day=1)], lesson_days=within(every))
    return replace(inp, regular_lesson_days=within(regular), regular_pattern_days=within(regular), regular_only_from=regular_from)


def charge(inp):
    (seg,) = [s for s in bl.run_ledger(inp).segments if s.begun]
    return seg


def test_17_after_the_cut_over_a_partial_month_is_the_regular_lessons_in_both_places():
    seg = charge(fixed_month(D(2026, 11, 1)))
    assert (seg.basis, seg.lessons_in_part, seg.lessons_in_period, seg.amount) == ("lessons", 7, 13, 323_000)        # 600 000 x 7 / 13


def test_17_before_the_cut_over_the_month_is_computed_the_old_way():
    seg = charge(fixed_month(D(2026, 10, 1)))
    assert (seg.lessons_in_part, seg.lessons_in_period, seg.amount) == (7, 15, 280_000)                              # 600 000 x 7 / 15: the two extra lessons are in the denominator
    assert charge(fixed_month(D(2026, 10, 1), regular_from=None)).amount == 280_000                                  # and with no cut-over at all
    assert charge(fixed_month(D(2026, 11, 1), regular_from=None)).amount == 280_000


def test_17_the_cut_over_is_by_the_start_of_the_period():
    """A month that STARTS on the cut-over day is new; one that starts the day before is old, whatever the day he joined."""
    inp = fixed_month(D(2026, 11, 1))
    assert charge(replace(inp, regular_only_from=D(2026, 11, 1))).amount == 323_000
    assert charge(replace(inp, regular_only_from=D(2026, 11, 2))).amount == 280_000


def test_17_a_full_month_does_not_change():
    for month in (D(2026, 10, 1), D(2026, 11, 1), D(2026, 12, 1)):
        seg = charge(fixed_month(month, joined_day=1))
        assert (seg.basis, seg.amount) == ("full", 600_000)


def test_17_a_lesson_the_centre_cancelled_stays_in_the_numerator():
    inp = fixed_month(D(2026, 11, 1))
    cancelled = replace(inp, centre_cancels=(bl.CentreCancel(D(2026, 11, 16), "l"),), recalc_versions=(bl.RecalcVersion(date.min, False),))
    assert charge(cancelled).amount == 323_000                              # the numerator is the held AND the cancelled lessons: the month costs the same
    on = replace(cancelled, recalc_versions=(bl.RecalcVersion(date.min, True),))
    led = bl.run_ledger(on)
    assert [l.amount for l in led.recalcs] == [bl.round_1000(600_000, 13)] == [46_000] and led.reconciles      # the price of a lesson is price / 13 as well


def test_17_own_month_is_not_touched():
    """«Свой месяц» (days): the same charge with or without the regular schedule and the cut-over."""
    def own(**extra):
        inp = make(today=D(2026, 11, 25), stays=[bl.Stay(D(2026, 11, 5), D(2026, 11, 20))], price_at=flat(600_000, D(2026, 1, 1)), start=D(2026, 9, 1),
                   regimes=[bl.Regime(mode=bl.CALENDAR, anchor=bl.ps.PAYMENT_DATE)], lesson_days=lambda a, b: [])
        return replace(inp, **extra)
    plain = bl.run_ledger(own())
    marked = bl.run_ledger(own(regular_lesson_days=lambda a, b: [], regular_pattern_days=lambda a, b: [], regular_only_from=CUT))
    assert [(s.basis, s.amount) for s in marked.segments] == [(s.basis, s.amount) for s in plain.segments] and plain.segments[0].basis == "days"
    assert all(not s.by_cell for s in marked.segments)


# ---- on PostgreSQL: the real schedule with real extra lessons -------------------------------------------------------------------------------------------------

from tests.ops_helpers import clock, history_row, needs_pg, pg_session  # noqa: E402,F401  (fixtures)
from tests.payf_helpers import API, build_org, cancel, centre, client, pay, rows  # noqa: E402


@needs_pg
def test_17_the_real_month_with_two_extra_lessons(pg_session, clock):
    """November 2026: 13 Mon / Wed / Fri lessons; two extra ones (03.11, 05.11) before the student came on 15.11; 7 regular lessons after: 600 000 x 7 / 13."""
    w = build_org(pg_session)
    clock.set("2026-11-01 09:00")
    (g,), (s,), (m,) = centre(w, joined=datetime(2026, 11, 15, 6, 0), billing_anchor="fixed_day", billing_day=1, accounting_start_date="2026-11-01")
    for day in ("2026-11-03", "2026-11-05"):
        r = client(w, "owner").post(f"{API}/org/schedule/occurrences", json={"groupId": str(g.id), "lessonDate": day, "startTime": "15:00", "endTime": "16:00", "purpose": "extra", "money": "paid"})
        assert r.status_code == 201, r.text
    clock.set("2026-11-20 12:00")
    (row,) = rows(w, s)
    assert row["debt"] == 323_000
    cancel(w, g, "lesson-mon", "2026-11-23")                                              # a cancelled regular lesson stays in the numerator
    (row,) = rows(w, s)
    assert row["debt"] == 323_000


@needs_pg
def test_17_a_package_does_not_count_an_additional_lesson(pg_session, clock):
    w = build_org(pg_session)
    clock.set("2026-11-03 09:00")
    (g,), (s,), (m,) = centre(w, joined=datetime(2026, 9, 1, 6, 0), period_mode="lessons", lessons_per_payment=12)
    r = client(w, "owner").post(f"{API}/org/schedule/occurrences", json={"groupId": str(g.id), "lessonDate": "2026-11-05", "startTime": "15:00", "endTime": "16:00", "purpose": "extra", "money": "paid"})
    one_off = r.json()["id"]
    pay(w, m, 600_000, date(2026, 11, 3))
    clock.set("2026-11-05 18:00")
    history_row(w.db, w.org, g, one_off, "2026-11-05", w.A, [s], start="15:00", end="15:55")          # the extra lesson was held
    w.db.commit()
    (row,) = rows(w, s)
    assert (row["lessons_used"], row["lessons_left"]) == (0, 12)                                       # #154: an additional lesson is free for the student - it spends nothing
