"""Money-2, the second round after the independent check (R3b, 17.11): В1-В4 and the small things (PostgreSQL).  The numbers of every money test are worked by hand in its comment."""
from datetime import date, timedelta

import pytest

from tests.money1b_helpers import add, set_schedule, slot
from tests.pg_world import ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.ops_helpers import exams  # noqa: F401  (fixture: exam-service stub)
from tests.test_money2_fix_r3_pg import leave, periods_billing_from, TUE_THU
from tests.test_money2_transfer_a_pg import groups3, snap
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block  # noqa: F401  (fixture)

pytestmark = needs_pg

FRIDAY = date(2026, 11, 27)
FIRST_LESSON = date(2026, 12, 1)          # the next Tuesday after Friday 27.11


# ============================================================================================================ В1: a cancelled leaving keeps the «Оплата с» that has not begun


@pytest.mark.parametrize("settings", [
    {},                                                                               # «Общий месяц»
    {"billing_anchor": "payment_date"},                                               # the personal period
    {"period_mode": "lessons", "lessons_per_payment": 8},                             # a package centre
], ids=["shared_month", "personal_period", "package"])
@pytest.mark.parametrize("explicit", [False, True], ids=["automatic_pay_from", "explicit_pay_from"])
def test_v1_leaving_and_coming_back_the_same_day_keeps_a_pay_from_that_has_not_begun(world, fixed_today, settings, explicit):
    # a group of Tuesdays and Thursdays (B), the student of A is written into B on Friday 27.11: no lesson that day, the first one is Tuesday 01.12 (automatic) or the owner chose Thursday 03.12 (explicit).
    # In the first month nothing is charged before «Оплата с»; he leaves B and comes back the same day: the stay is the SAME stay, so «Оплата с» stays 01.12 (03.12), not 27.11.
    # Hand count (package centre, 8 lessons for 300 000 -> 37 500 a lesson... the numbers of the ledger are compared before/after: they must be equal, and the period's own «Оплата с» is the exact check).
    a, b, c = groups3(world, fixed_today, FRIDAY)
    set_schedule(world, TUE_THU, b)
    if settings:
        settings_block(world, **settings)
    s0 = world.students[0]
    day = date(2026, 12, 3) if explicit else FIRST_LESSON
    out = add(world, s0, group=b, mode="add", **({"pay_from": day.isoformat()} if explicit else {}))
    assert out["billing"]["pay_from"] == day.isoformat() if out["billing"] and "pay_from" in out["billing"] else True
    before = snap(world, s0, b)
    assert [bf for _, bf in periods_billing_from(world, s0, b)] == [day]
    leave(world, b, s0)
    r = add(world, s0, group=b, mode="add")
    assert r["leave_undo"]["undone"] is True
    assert [bf for _, bf in periods_billing_from(world, s0, b)] == [day, day]          # the closed period keeps its own, the new one carries it on
    assert snap(world, s0, b) == before


def test_v1_a_pay_from_that_has_already_begun_is_not_carried(world, fixed_today):
    # «Оплата с» 02.11 is in the past on Friday 27.11: the return the same day is the same stay (nothing to carry), the new period has no own «Оплата с»
    a, b, c = groups3(world, fixed_today, FRIDAY)
    set_schedule(world, TUE_THU, b)
    s0 = world.students[0]
    add(world, s0, group=b, mode="add", pay_from="2026-11-02")
    leave(world, b, s0)
    assert add(world, s0, group=b, mode="add")["leave_undo"]["undone"] is True
    assert [bf for _, bf in periods_billing_from(world, s0, b)] == [date(2026, 11, 2), None]
