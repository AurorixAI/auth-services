"""LESSON-RULES FIX4 А3: the mark «оформлен задним числом» of the journal of the group is ONE rule with the history of the owner: the lesson was formalised on a LATER business day than its date.
The same day (also late in the evening) is no mark and writes nothing (``details.backdated`` is not set).

World: ``lesson_rules_helpers.world`` (Mon / Wed 10:00-11:30, nine students, Thu 26.11.2026 12:00 until a test sets another time).  The lesson is Wed 25.11."""
from src.models.auth import GroupAttendance, GroupAttendanceAudit
from tests.lesson_rules_helpers import WED, reconcile, world, clock, pg_session, needs_pg, client, API  # noqa: F401

pytestmark = needs_pg
DAY = "2026-11-25"


def marks(w, g):
    """The marks «оформлен задним числом» of the journal of the group (the road of the screen) and what the journal of the audit was told."""
    r = client(w, "owner").get(f"{API}/org/groups/{g.id}/attendance", params={"month": "2026-11"})
    assert r.status_code == 200, r.text
    w.db.expire_all()
    row = w.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_date=DAY, lesson_id=WED).one()
    audit = w.db.query(GroupAttendanceAudit).filter_by(attendance_id=row.id, action="reconciliation_started").one()
    return [k for k in r.json()["backdated"] if DAY in k], audit.details.get("backdated")


def test_a_lesson_formalised_on_its_own_day_has_no_mark_and_writes_none(pg_session, clock):
    """Hand: Wed 25.11 12:00, the lesson (10:00-11:30) is over; the teacher A formalises it the same day: no mark in the journal, ``details`` has no ``backdated``."""
    w, g, people, members = world(pg_session, clock)
    clock.set("2026-11-25 12:00")
    reconcile(w, g, DAY)
    assert marks(w, g) == ([], None)


def test_a_lesson_formalised_late_in_the_evening_of_its_own_day_has_no_mark(pg_session, clock):
    """Hand: 25.11 23:58 (business time) is still 25.11: no mark."""
    w, g, people, members = world(pg_session, clock)
    clock.set("2026-11-25 23:58")
    reconcile(w, g, DAY)
    assert marks(w, g) == ([], None)


def test_a_lesson_formalised_the_next_day_has_the_mark_and_the_flag(pg_session, clock):
    """Hand: 26.11 00:02 is the next day: ONE mark of the lesson 25.11 (by A) and ``details.backdated`` true; the same for 26.11 12:00 (the next day, the old rule held for it too)."""
    w, g, people, members = world(pg_session, clock)
    clock.set("2026-11-26 00:02")
    reconcile(w, g, DAY)
    keys, flag = marks(w, g)
    assert len(keys) == 1 and flag is True
    r = client(w, "owner").get(f"{API}/org/groups/{g.id}/attendance", params={"month": "2026-11"})
    assert r.json()["backdated"][keys[0]]["by_name"] == "Иванов И."          # FIX5: the same short form as the history of the owner (LESSONS 48)
