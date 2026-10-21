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
