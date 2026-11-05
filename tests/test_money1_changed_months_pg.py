"""Money-1 (front need): ``GET /org/payroll/changed-months`` - the closed / reopened months (the last 12) whose saved pay has moved, ``{"items": [{"month": "2026-09", "teachers": 2}]}`` (PostgreSQL).

The number of teachers is the number of the saved sheets whose FRESH total (``PayrollService._fresh_total``: the function of the list of the month) is not the saved one - ``changed_by != 0`` of the payout
state.  Numbers: the G-PAY world of ``area-payroll-devil.md`` (50 %, S1 paid, S2 paid half of September: a late payment of S2 moves the saved September of the teacher by +125 000) plus a second teacher
who held three lessons of the second half of the month (+75 000)."""
from datetime import date

import pytest

from src.auth import closed_period

from tests.money1_helpers import now_4_nov, reopen_now, sheet, tashkent  # noqa: F401  (fixtures)
from tests.ops_helpers import grant_money_right
from tests.pg_world import make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_logicA_payroll_pg import API, g_pay_world, owner, save_sheet, student_group  # noqa: F401
from tests.test_pay2_api_pg import fixed_today  # noqa: E402,F401  (fixture)
from tests.test_payroll_pg import lesson, overview, set_rate

pytestmark = needs_pg
URL = f"{API}/org/payroll/changed-months"


def changed(who):
    r = who.get(URL)
    assert r.status_code == 200, r.text
    return r.json()


def two_teachers_in_a_closed_september(world, fixed_today):
    """September of the G-PAY world saved on 20.10 (after it ended: CLOSED) for two teachers; the second held the lessons of the 18th, 21st and 25th with S1 and S2."""
    fixed_today(date(2026, 10, 20))
    m, s1, s2 = g_pay_world(world)
    assert m.label == "2026-09"
    for n in (18, 21, 25):
        lesson(world, m.d(n), {s1: "present", s2: "present"}, teacher=world.teacher2)
    set_rate(world, world.teacher2, "group", "50")
    first = save_sheet(world, m)
    second = save_sheet(world, m, teacher=world.teacher2)
    assert (first["total_amount"], second["total_amount"]) == (475_000, 75_000)          # S1 only: S2 has paid the first half, the lessons of the second half of the second teacher are not paid yet
    return m, s1, s2, first, second


def pay_late(world, s2):
    mem2 = student_group(world, s2)
    r = owner(world).post(f"{API}/org/memberships/{mem2.id}/payments", json={"amount": 300_000, "paid_on": "2026-11-20", "method": "cash"})
    assert r.status_code == 201, r.text


# ================================================================================================ the contract
def test_a_late_payment_into_a_closed_september_names_the_month_with_both_teachers_and_the_new_sum_empties_the_list(world, fixed_today):
    m, s1, s2, first, second = two_teachers_in_a_closed_september(world, fixed_today)
    assert changed(owner(world)) == {"items": []}                                          # nothing moved yet: the empty answer is {"items": []}
    fixed_today(date(2026, 11, 20))
    pay_late(world, s2)
    assert changed(owner(world)) == {"items": [{"month": "2026-09", "teachers": 2}]}
    # the number is the one of the LIST of the month (LESSONS 104): the teachers whose payout says «изменилась»
    rows = overview(world, m)["teachers"]
    assert sorted(t["payout"]["changed_by"] for t in rows if t["payout"]) == [75_000, 125_000]
    # «Сохранить новую сумму»: both sheets are saved again - nothing differs any more
    for sheet_ in (first, second):
        look = owner(world).get(f"{API}/org/payroll/sheets/{sheet_['id']}/diff").json()["source_fingerprint"]
        r = owner(world).patch(f"{API}/org/payroll/sheets/{sheet_['id']}", json={"revision": 1, "recalculate": True, "expected_fingerprint": look})
        assert r.status_code == 200, r.text
    assert changed(owner(world)) == {"items": []}
    assert all(not t["payout"]["changed_by"] for t in overview(world, m)["teachers"] if t["payout"])


def test_the_first_teacher_alone_is_one_and_a_teacher_without_a_change_is_not_counted(world, fixed_today):
    fixed_today(date(2026, 10, 20))
    m, s1, s2 = g_pay_world(world)
    save_sheet(world, m)
    fixed_today(date(2026, 11, 20))
    pay_late(world, s2)
    assert changed(owner(world)) == {"items": [{"month": "2026-09", "teachers": 1}]}


# ================================================================================================ which months
def raw_sheet(world, teacher, month, saved_at):
    """A saved sheet that carries 100 000 while the data say 0 (no lessons of the teacher in the month): the total has moved by -100 000, the cheapest «изменилась»."""
    return sheet(world, teacher, month, saved_at, total=100_000)


def test_only_closed_and_reopened_months_are_named_an_open_one_never(world, now_4_nov):
    raw_sheet(world, world.teacher, date(2026, 8, 1), tashkent(2026, 9, 3))          # August saved after it ended: CLOSED
    raw_sheet(world, world.teacher, date(2026, 9, 1), tashkent(2026, 9, 15))         # September saved in its middle (an advance), never again: OPEN
    raw_sheet(world, world.teacher2, date(2026, 9, 1), tashkent(2026, 9, 16))
    raw_sheet(world, world.teacher, date(2026, 10, 1), tashkent(2026, 11, 2))        # October saved after it ended, then REOPENED
    reopen_now(world.db, world.org.id, world.teacher.id, "2026-10", world.owner.id, "Исправляем октябрь")
    raw_sheet(world, world.teacher, date(2026, 11, 1), tashkent(2026, 11, 3))        # the CURRENT month: its own list shows it
    states = {(m, closed_period.period_state(world.db, world.org.id, world.teacher.id, date.fromisoformat(m + "-01"))["state"]) for m in ("2026-08", "2026-09", "2026-10", "2026-11")}
    assert states == {("2026-08", "closed"), ("2026-09", "open"), ("2026-10", "reopened"), ("2026-11", "open")}
    assert changed(owner(world)) == {"items": [{"month": "2026-08", "teachers": 1}, {"month": "2026-10", "teachers": 1}]}


def test_the_window_is_the_last_twelve_months_not_the_whole_history(world, now_4_nov):
    raw_sheet(world, world.teacher, date(2025, 10, 1), tashkent(2025, 11, 3))        # 13 months back: out
    raw_sheet(world, world.teacher, date(2025, 11, 1), tashkent(2025, 12, 3))        # 12 months back: in
    assert changed(owner(world)) == {"items": [{"month": "2025-11", "teachers": 1}]}


def test_a_sheet_that_is_as_saved_is_not_a_change(world, now_4_nov):
    raw = raw_sheet(world, world.teacher, date(2026, 9, 1), tashkent(2026, 10, 3))
    assert changed(owner(world)) == {"items": [{"month": "2026-09", "teachers": 1}]}
    raw.total_amount = 0                                                              # the data say 0: the saved total is the fresh one
    world.db.commit()
    assert changed(owner(world)) == {"items": []}


# ================================================================================================ rights
def test_the_owner_and_an_administrator_with_the_money_right_read_it_an_administrator_without_it_is_refused(world, now_4_nov):
    raw_sheet(world, world.teacher, date(2026, 9, 1), tashkent(2026, 10, 3))
    want = {"items": [{"month": "2026-09", "teachers": 1}]}
    grant_money_right(world, False)
    refused = make_client(world, user=world.admin).get(URL)
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "MONEY_RIGHT_REQUIRED"
    assert make_client(world, user=world.teacher).get(URL).status_code == 403                  # a teacher
    assert changed(owner(world)) == want
    grant_money_right(world, True)
    assert changed(make_client(world, user=world.admin)) == want                               # the number of teachers, never a sum
    assert make_client(world, user=world.teacher).get(URL).status_code == 403


# ================================================================================================ the mutation: the filter of the closed months keeps the open ones out
def test_without_the_filter_of_the_closed_months_the_open_month_would_be_named(world, now_4_nov, monkeypatch):
    """The mutation of the task, run by the test itself: a rule that takes every month for closed puts the open September into the answer - so the test above that expects it NOT there is the one that
    fails on the code without the filter (the manual mutation - ``("closed", "reopened")`` -> ``("closed", "reopened", "open")`` - is in the report)."""
    raw_sheet(world, world.teacher, date(2026, 9, 1), tashkent(2026, 9, 15))
    assert changed(owner(world)) == {"items": []}
    monkeypatch.setattr(closed_period, "state_of_sheet", lambda sheet_, last: ("closed", date(2026, 9, 15)))
    assert changed(owner(world)) == {"items": [{"month": "2026-09", "teachers": 1}]}
