"""Money-2, the round after the independent review of the server (R3, 16.11): П1-П4 and the small things (PostgreSQL).  The numbers of every money test are worked by hand in its comment."""
from datetime import date, timedelta

import pytest

from src.models.auth import GroupAudit, StudentGroup
from tests.money1b_helpers import add, set_schedule, slot
from tests.pg_world import ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money2_transfer_a_pg import groups3, joined, nov_charge, snap
from tests.test_payments_service_pg import mid, pay
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block, stay  # noqa: F401  (fixture)

pytestmark = needs_pg

FRIDAY = date(2026, 11, 27)
TUE_THU = [slot("tu", 2, "2026-01-01"), slot("th", 4, "2026-01-01")]          # no lesson on Friday: the next one is Tuesday 01.12


# ============================================================================================================ П1: the return on the day of leaving does not move «Оплата с»


def leave(w, group, student):
    r = owner(w).post(f"{API}/org/groups/{group.id}/students/{student.id}/leave", json={})
    assert r.status_code == 200, r.text


def periods_billing_from(w, student, group):
    w.db.expire_all()
    return sorted((p.joined_at, p.billing_from) for p in membership(w, student, group).periods)


@pytest.mark.parametrize("settings", [
    {},                                                                               # «Общий месяц»: calendar months from the 1st
    {"billing_anchor": "payment_date"},                                               # the personal period
    {"period_mode": "lessons", "lessons_per_payment": 8},                             # a package centre
], ids=["shared_month", "personal_period", "package"])
def test_p1_leaving_and_coming_back_on_friday_in_a_tuesday_thursday_group_gives_every_number_back(world, fixed_today, settings):
    # a group of Tuesdays and Thursdays, price 300 000 a month, the student since Mon 02.11, paid 300 000; he leaves and comes back on Friday 27.11 (no lesson that day: the next one is Tue 01.12).
    # Before the fix the return opened a NEW period with «Оплата с» = 01.12: 27-30.11 became a pause and the «paid until» moved.  Now the stay goes on as if he had not left: every number as before.
    a, b, c = groups3(world, fixed_today, FRIDAY)
    set_schedule(world, TUE_THU, a)
    if settings:
        settings_block(world, **settings)
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    pay(world, s0, a, amount=300_000, paid_on="2026-11-05")
    before = snap(world, s0, a)
    leave(world, a, s0)
    out = add(world, s0, group=a, mode="add")
    assert out["leave_undo"]["undone"] is True and out["billing"] is None            # no new «Оплата с»: nothing was started
    assert snap(world, s0, a) == before
    assert all(billing_from is None for _, billing_from in periods_billing_from(world, s0, a))
    if not settings:
        assert nov_charge(world, s0, a) == 300_000                                   # a partial month goes by lessons: Tue 3, 10, 17, 24 and Thu 5, 12, 19, 26 = 8 of 8 lessons of November are in the stay -> the whole 300 000


def test_p1_an_explicit_pay_from_or_opening_balance_with_a_cancelled_leaving_is_refused_and_nothing_is_written(world, fixed_today):
    a, b, c = groups3(world, fixed_today, FRIDAY)
    set_schedule(world, TUE_THU, a)
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    pay(world, s0, a, amount=300_000)
    leave(world, a, s0)
    gone = snap(world, s0, a)
    for body in ({"pay_from": "2026-12-01"}, {"opening_balance": {"debt_amount": 100_000, "debt_on": "2026-11-10"}}):
        r = add(world, s0, group=a, mode="add", expect=422, **body)
        assert r["detail"]["code"] == "LEAVE_UNDO_NO_START" and "уход отменяется" in r["detail"]["message"]
        assert snap(world, s0, a) == gone and membership(world, s0, a).is_active is False


def test_p1_the_return_after_the_end_of_the_paid_period_and_the_next_day_stay_ordinary(world, fixed_today):
    # the paid period ended earlier (paid 150 000 on 05.11 = 15 days of 300 000/30 -> covered through 19.11); on Friday 27.11 he leaves and comes back the same day: the leaving is cancelled,
    # the stay is not touched, the debt of 20.11-26.11 stays what it was
    a, b, c = groups3(world, fixed_today, FRIDAY)
    set_schedule(world, TUE_THU, a)
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    pay(world, s0, a, amount=150_000, paid_on="2026-11-05")
    before = snap(world, s0, a)
    leave(world, a, s0)
    assert add(world, s0, group=a, mode="add")["leave_undo"]["undone"] is True
    assert snap(world, s0, a) == before


def test_p1_restoring_a_student_archived_today_into_the_same_group_is_the_same_cancel(world, fixed_today):
    a, b, c = groups3(world, fixed_today, FRIDAY)
    set_schedule(world, TUE_THU, a)
    s0 = world.students[0]
    joined(world, s0, a, date(2026, 11, 2))
    pay(world, s0, a, amount=300_000, paid_on="2026-11-05")
    before = snap(world, s0, a)
    assert owner(world).post(f"{API}/org/students/{s0.id}/archive", json={}).status_code == 200
    r = owner(world).post(f"{API}/org/students/{s0.id}/restore", json={"group_ids": [str(a.id)]})
    assert r.status_code == 200 and not r.json()["skipped"], r.text
    assert snap(world, s0, a) == before
    assert all(billing_from is None for _, billing_from in periods_billing_from(world, s0, a))
