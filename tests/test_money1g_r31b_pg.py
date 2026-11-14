"""Money-1 G3M Р3-1б (PostgreSQL): the individual price given at the enrolment of a RETURNING student acts on every day of the new stay from
«Оплата с» (or from today), whatever rows his earlier stay left; the closed stay is never re-priced.

Root: a stay that is closed ends its own price rows (the marker of the last day, the scheduled rows after it are cancelled with an audit row), so a
return finds a clean book.  The centre: group 500 000; he studied 01.09-20.10 and left on 20.10; the clock then stands at 14.11."""
from datetime import date

import pytest

from src.auth import pricing
from src.models.auth import GroupAudit, MembershipPriceOverride

from tests.money1b_helpers import account, add, fresh_student, opening_world
from tests.pg_world import give_group_price, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money1b_pay_from_pg import STUDY, stay_of
from tests.test_pay2_api_pg import fixed_today  # noqa: F401  (fixture)

pytestmark = needs_pg
D = date


def _left(w, fixed_today, *, own=None, scheduled=None, plain=False):
    """He studied 01.09-20.10 (own price ``own`` from 01.09), maybe a scheduled own price (valid_from, amount); left on 20.10; now 14.11.
    ``plain``: the ordinary removal (``AuthService.remove_student_from_group``), else the leave of the route."""
    from tests.test_logicA_money_pg import leave

    opening_world(w, fixed_today, today=D(2026, 10, 20), schedule=STUDY)
    give_group_price(w.db, w.group, 500_000)
    s = fresh_student(w, "Вернувшийся")
    body = {"pay_from": "2026-09-01"}
    if own is not None:
        body["price_override"] = {"amount": own, "reason": "Скидка по договорённости"}
    add(w, s, **body)
    m, _ = stay_of(w, s)
    if scheduled is not None:
        w.db.add(MembershipPriceOverride(membership_id=m.id, amount=scheduled[1], valid_from=scheduled[0], reason="План", created_by=w.owner.id))
        w.db.commit()
    if plain:
        w.svc.remove_student_from_group(str(w.org.id), str(w.group.id), str(s.id), str(w.owner.id))
        w.db.commit()
    else:
        leave(w, w.group, s)
    fixed_today(D(2026, 11, 14))
    return s, m


def _price(w, m, day):
    w.db.expire_all()
    p = pricing.load_books(w.db, [w.group.id], [m.id]).price(m.id, w.group.id, day)
    return p.amount, p.source


def _old_cells(w, m):
    return [(x.period_start, x.period_end, x.amount) for x in account(w, m.id, D(2026, 11, 14)).segments if x.kind == "period" and x.period_end < D(2026, 10, 1)]   # the cells of the closed stay that no return re-cuts (the month he left in is re-cut by the ledger as before)


def _days_from(w, m, first, last, amount):
    day = first
    while day <= last:
        assert _price(w, m, day) == (amount, "override"), day
        day = D.fromordinal(day.toordinal() + 1)


@pytest.mark.parametrize("plain", [True, False])
def test_a_removed_with_an_own_price_returns_with_pay_from_the_day_after_and_a_new_price_every_day_is_the_new_price(world, fixed_today, plain):
    w = world
    s, m = _left(w, fixed_today, own=400_000, plain=plain)
    before = _old_cells(w, m)
    assert before and before[0][2] == 400_000
    add(w, s, pay_from="2026-10-21", price_override={"amount": 450_000, "reason": "Новая скидка при возврате"})
    _days_from(w, m, D(2026, 10, 21), D(2026, 11, 14), 450_000)
    assert _price(w, m, D(2026, 12, 20)) == (450_000, "override")                         # the next period too
    assert _price(w, m, D(2026, 10, 20)) == (400_000, "override")                         # the earlier stay keeps its price
    assert _old_cells(w, m) == before                                                      # not a sum of the earlier statement changed


def test_b_a_scheduled_own_price_of_the_earlier_stay_dated_after_the_leave_is_cancelled_with_the_leave_and_audited(world, fixed_today):
    w = world
    s, m = _left(w, fixed_today, own=400_000, scheduled=(D(2026, 11, 5), 380_000))
    assert w.db.query(MembershipPriceOverride).filter_by(membership_id=m.id, valid_from=D(2026, 11, 5)).count() == 0
    cancelled = [a for a in w.db.query(GroupAudit).filter_by(group_id=w.group.id, action=pricing.ACTION_MEMBERSHIP_PRICE_CANCELLED).all()]
    assert len(cancelled) == 1 and cancelled[0].snapshot["valid_from"] == "2026-11-05" and cancelled[0].snapshot["amount"] == 380_000
    before = _old_cells(w, m)
    add(w, s, pay_from="2026-10-21", price_override={"amount": 450_000, "reason": "Новая скидка при возврате"})
    _days_from(w, m, D(2026, 10, 21), D(2026, 11, 14), 450_000)
    assert _old_cells(w, m) == before


def test_c_the_same_scheduled_price_returned_without_a_price_means_the_group_price(world, fixed_today):
    w = world
    s, m = _left(w, fixed_today, scheduled=(D(2026, 11, 5), 380_000))
    add(w, s)
    for day in (D(2026, 10, 21), D(2026, 11, 4), D(2026, 11, 5), D(2026, 11, 14)):
        assert _price(w, m, day) == (500_000, "group"), day                                # the planned price of the earlier stay does not come alive


def test_d_leaving_and_returning_on_the_same_day_with_a_price_acts_from_today(world, fixed_today):
    from tests.test_logicA_money_pg import leave

    w = world
    opening_world(w, fixed_today, today=D(2026, 10, 20), schedule=STUDY)
    give_group_price(w.db, w.group, 500_000)
    s = fresh_student(w, "Вернувшийся")
    add(w, s, pay_from="2026-09-01", price_override={"amount": 400_000, "reason": "Скидка по договорённости"})
    m, _ = stay_of(w, s)
    leave(w, w.group, s)
    add(w, s, price_override={"amount": 450_000, "reason": "Новая скидка при возврате"})
    for day in (D(2026, 10, 20), D(2026, 10, 21), D(2026, 11, 14), D(2026, 12, 20)):
        assert _price(w, m, day) == (450_000, "override"), day                              # the marker of tomorrow is replaced by the new row of the same day


def test_e_a_student_without_own_prices_before_returns_with_a_price_and_a_past_pay_from_the_new_price_acts_from_pay_from(world, fixed_today):
    w = world
    s, m = _left(w, fixed_today)
    add(w, s, pay_from="2026-10-21", price_override={"amount": 300_000, "reason": "Скидка по договорённости"})
    _days_from(w, m, D(2026, 10, 21), D(2026, 11, 14), 300_000)
    assert _price(w, m, D(2026, 10, 20)) == (500_000, "group")


def test_f_left_with_an_own_price_returned_without_a_price_means_the_group_price(world, fixed_today):
    w = world
    s, m = _left(w, fixed_today, own=400_000, plain=True)
    add(w, s)
    assert _price(w, m, D(2026, 10, 20)) == (400_000, "override")
    for day in (D(2026, 10, 21), D(2026, 11, 14), D(2026, 12, 20)):
        assert _price(w, m, day) == (500_000, "group"), day


def test_g_a_pay_from_inside_the_earlier_stay_is_still_refused(world, fixed_today):
    w = world
    s, m = _left(w, fixed_today, own=400_000, plain=True)
    r = add(w, s, expect=422, pay_from="2026-10-10", price_override={"amount": 450_000, "reason": "Новая скидка при возврате"})
    assert r["detail"]["code"] == "PAY_FROM_RANGE"
