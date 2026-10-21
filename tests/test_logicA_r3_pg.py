"""LOGIC-A, fixes of the lead's R3 review (B1, M1-M3, m1-m13): one test per finding, on PostgreSQL."""
from datetime import date, timedelta

import pytest

from src.models.auth import Group, GroupAudit, GroupLessonTypeHistory

from tests.pg_world import give_group_price, make_client, pg_session, world  # noqa: F401  (fixtures)
from tests.test_payroll_pg import (  # noqa: F401
    API, calc, current_month, lesson, line_of, overview, owner, pay_calendar, preview, previous_month, save, set_rate,
    student_group,
)
from tests.test_logicA_payroll_pg import save_sheet

pytestmark = pytest.mark.skipif(not __import__("os").environ.get("A2_AUTH_TEST_DATABASE_URL"), reason="Requires disposable A2 auth PostgreSQL")


def today():
    from src.auth import payment_status as ps

    return ps.business_today()


# ---- B1: PUT /org/groups/{id} with a new lesson_type goes through the history ------------------------------------------------------------


def test_b1_a_group_update_with_a_new_lesson_type_writes_a_history_row_for_today_and_stales_the_current_sheets(world):
    s0, t = world.students[0], today()
    set_rate(world, world.teacher, "group", "50")
    set_rate(world, world.teacher, "pair", "60")
    cm = current_month()
    give_group_price(world.db, world.group, 600_000, valid_from=date(2026, 1, 1))
    lesson(world, t, {s0: "present"})
    saved = save_sheet(world, cm)
    assert {l["lesson_type"] for l in saved["lines"]} == {"group"}
    r = owner(world).put(f"{API}/org/groups/{world.group.id}", json={"lesson_type": "pair"})
    assert r.status_code == 200, r.text
    world.db.expire_all()
    rows = world.db.query(GroupLessonTypeHistory).filter_by(group_id=world.group.id).order_by(GroupLessonTypeHistory.valid_from).all()
    assert [(x.valid_from, x.lesson_type) for x in rows][-1] == (t, "pair") and rows[0].lesson_type == "group"
    assert world.db.get(Group, world.group.id).lesson_type == "pair"
    audit = world.db.query(GroupAudit).filter_by(group_id=world.group.id, action="group_lesson_type_set").one()
    assert audit.snapshot["valid_from"] == t.isoformat() and audit.snapshot["was"] == "group" and audit.snapshot["lesson_type"] == "pair"
    assert owner(world).get(f"{API}/org/groups/{world.group.id}/lesson-types").json()["current"] == "pair"
    assert {l["lesson_type"] for l in preview(world, cm)["lines"]} == {"pair"}               # payroll reads the history
    assert owner(world).get(f"{API}/org/payroll/sheets/{saved['id']}").json()["stale"] is True
    # the same type again writes nothing; a day of the past stays on PUT …/lesson-type
    assert owner(world).put(f"{API}/org/groups/{world.group.id}", json={"lesson_type": "pair", "name": "Новое имя"}).status_code == 200
    world.db.expire_all()
    assert world.db.query(GroupAudit).filter_by(group_id=world.group.id, action="group_lesson_type_set").count() == 1


# ---- M1: the divisor / mode of a lesson come from the ledger segment that covers it --------------------------------------------------------


from tests.test_pay2_api_pg import D, TODAY, fixed_today, membership, seed_payment, settings_block, stay  # noqa: E402,F401


def _coverage(w, m, first=D(2026, 9, 1)):
    from src.auth.payments_service import PaymentService

    w.db.expire_all()
    got = PaymentService(w.db).lesson_coverage(str(w.org.id), [m.id], first, TODAY)
    return {day: (c.mode, c.package_size, c.cover_source) for (_mid, day, _lid), c in sorted(got.items(), key=lambda kv: kv[0][1])}


def test_m1_deferred_lessons_to_calendar_the_running_package_keeps_its_divisor(world, fixed_today):
    """Package of 12 lessons begun 15.09; the centre switches to calendar on 10.10 (deferred): the package is finished first (its lessons
    on 10.10-14.10 keep the divisor 12, mode lessons), the lessons after it belong to the calendar period (no package size)."""
    s0 = world.students[0]
    settings_block(world, accounting_start_date="2026-09-01", period_mode="calendar", billing_history=[
        {"from": "min", "period_mode": "lessons", "lessons_per_payment": 12, "billing_anchor": "payment_date"},
        {"from": "2026-10-10", "defer": True, "period_mode": "calendar", "lessons_per_payment": 12, "billing_anchor": "payment_date"}])
    give_group_price(world.db, world.group, 600_000, valid_from=D(2026, 1, 1))
    m = membership(world, s0)
    stay(world, m, D(2026, 9, 15))
    days = [D(2026, 9, 15) + timedelta(days=2 * i) for i in range(8)] + [D(2026, 10, 11), D(2026, 10, 12), D(2026, 10, 13), D(2026, 10, 14), D(2026, 10, 16), D(2026, 10, 18)]
    for d in days:
        lesson(world, d, {s0: "present"})
    seed_payment(world, m, 600_000, D(2026, 9, 15))
    got = _coverage(world, m)
    pkg, tail = days[:12], days[12:]
    assert {got[d][:2] for d in pkg} == {("lessons", 12)}, got
    assert {got[d][:2] for d in tail} == {("calendar", None)}, got


def test_m1_deferred_calendar_to_lessons_the_running_period_keeps_calendar_and_the_first_package_follows(world, fixed_today):
    s0 = world.students[0]
    settings_block(world, accounting_start_date="2026-09-01", period_mode="lessons", lessons_per_payment=4, billing_history=[
        {"from": "min", "period_mode": "calendar", "billing_anchor": "payment_date"},
        {"from": "2026-10-10", "defer": True, "period_mode": "lessons", "lessons_per_payment": 4, "billing_anchor": "payment_date"}])
    give_group_price(world.db, world.group, 600_000, valid_from=D(2026, 1, 1))
    m = membership(world, s0)
    stay(world, m, D(2026, 9, 15))
    days = [D(2026, 9, 20), D(2026, 10, 12), D(2026, 10, 13), D(2026, 10, 16), D(2026, 10, 17), D(2026, 10, 18)]        # the period 15.09-14.10, then packages
    for d in days:
        lesson(world, d, {s0: "present"})
    seed_payment(world, m, 1_200_000, D(2026, 9, 15))
    got = _coverage(world, m)
    assert {got[d][:2] for d in days[:3]} == {("calendar", None)}, got                 # 12.10 and 13.10 lie in the period that was running when the regime changed
    assert {got[d][:2] for d in days[3:]} == {("lessons", 4)}, got
