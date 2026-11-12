"""Money-1 / Г: holes of the acceptance review MONEY1_G1_REVIEW (N1…N9) — each test carries the numbers of its scenario (PostgreSQL)."""
from datetime import date

import pytest

from src.models.auth import AttendanceExcuse

from tests.money1_helpers import hold_lesson, now_4_nov  # noqa: F401  (fixtures)
from tests.ops_helpers import grant_money_right
from tests.pg_world import make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money1_attendance_pg import post_save

pytestmark = needs_pg


# ---- Г-1 (N1): the text of a decided excuse does not move the decision --------------------------------------------------------------------------------

@pytest.mark.parametrize("decision", ["confirmed", "rejected"])
def test_g1_the_text_of_a_decided_excuse_keeps_the_decision_for_everybody(world, now_4_nov, decision):
    day, lid = hold_lesson(world, date(2026, 10, 30))
    s1 = world.students[0]
    grant_money_right(world)
    assert post_save(world, world.owner, day, lid, ["excused", "present", "present"], correctionReason="Болел", excuseReasons={str(s1.id): "Болел, написал"}).status_code == 200
    excuse = world.db.query(AttendanceExcuse).filter_by(student_id=s1.id).one()
    excuse.status, excuse.reviewed_by, excuse.review_note = decision, world.owner.id, "Решено"
    world.db.commit()
    grant_money_right(world, False)
    for who in (world.teacher, world.admin):                       # no right to the money: the text is saved, the decision (pack −1 lesson, teacher +20 000) stays
        r = post_save(world, who, day, lid, ["excused", "present", "present"], excuseReasons={str(s1.id): f"Другой текст {who.id}"})
        assert r.status_code == 200, r.text
        world.db.expire_all()
        row = world.db.query(AttendanceExcuse).filter_by(student_id=s1.id).one()
        assert (row.status, row.reviewed_by, row.review_note) == (decision, world.owner.id, "Решено") and row.reason == f"Другой текст {who.id}"
