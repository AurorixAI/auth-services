"""LESSON-RULES FIX4 А1 (LESSONS 194 b): «История» reads the lines of the journal written BEFORE the migration ``mg12a1b2c3d4`` - they have no date and no id of the lesson.

The state of such a line cannot be made by the product any more (every writer of the journal copies the key of the lesson on insert), so the test makes it the only way it exists: the line the
product wrote is stripped of its key by an UPDATE (what the old database holds).  Everything else goes through the roads of the product.
World: ``lesson_rules_helpers.world`` (Mon / Wed 10:00-11:30, nine students, today Thu 26.11.2026 12:00).  Wed 25.11 is completed a day late (an entry ``backdated``, 1 day), Mon 23.11 is
completed late by an administrator (3 days) and then corrected by the owner (an entry ``corrected``)."""
from src.models.auth import GroupAttendanceAudit
from tests.lesson_rules_helpers import MON, WED, marks_of, reconcile, save, world, clock, pg_session, needs_pg, client, API  # noqa: F401

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"


def listing(w, who="owner", **params):
    r = client(w, who).get(SUB, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def audit_rows(items):
    return {(i["kind"], i["lesson"]["date"]): i for i in items if i.get("source") == "audit"}


def _world_with_entries(pg_session, clock, *, old: bool):
    """Two entries of the journal; with ``old`` the lines that carry them lose the key of the lesson (the state before the migration)."""
    w, g, people, members = world(pg_session, clock, money_right=True)
    reconcile(w, g, "2026-11-25")
    save(w, g, people, "2026-11-25", marks=marks_of(people, first_present=2))
    reconcile(w, g, "2026-11-23", MON)
    save(w, g, people, "2026-11-23", MON, who="admin", marks=marks_of(people, first_present=1))
    assert save(w, g, people, "2026-11-23", MON, who="owner", marks=marks_of(people, first_present=3), correctionReason="Ошибся").status_code == 200
    if old:
        lines = w.db.query(GroupAttendanceAudit).filter(GroupAttendanceAudit.group_id == g.id, GroupAttendanceAudit.action.in_(("updated", "corrected"))).all()
        assert len(lines) >= 3
        for line in lines:
            line.lesson_date, line.lesson_id = None, None
        w.db.commit()
        w.db.expire_all()
        assert w.db.query(GroupAttendanceAudit).filter(GroupAttendanceAudit.group_id == g.id, GroupAttendanceAudit.lesson_date.is_(None)).count() == len(lines)
    return w, g


def test_old_lines_without_the_key_are_read_with_the_date_the_time_and_can_return(pg_session, clock):
    """The history with the lines of the OLD form is the same list as with the new ones, ``limit`` 20 and 50, with the flag «Только задним числом» and without: 200, the same entries (backdated 25.11
    by 1 day, backdated 23.11 by 3 days, corrected 23.11), each with its date, the time of the lesson from the schedule (10:00-11:30) and ``canReturn`` true (the lesson is held now, the group is
    active, the month is open, the owner holds the money right)."""
    w, g = _world_with_entries(pg_session, clock, old=True)
    for limit in (20, 50):
        for flag in (False, True):
            out = listing(w, status="decided", limit=limit, **({"backdated_only": "true"} if flag else {}))
            rows = audit_rows(out["items"])
            assert set(rows) == {("backdated", "2026-11-25"), ("backdated", "2026-11-23"), ("corrected", "2026-11-23")}, (limit, flag)
            assert out["total"] == 3
            assert (rows[("backdated", "2026-11-25")]["lateDays"], rows[("backdated", "2026-11-23")]["lateDays"]) == (1, 3)
            for e in rows.values():
                assert (e["lesson"]["lessonId"] in (MON, WED), e["lesson"]["startTime"], e["lesson"]["endTime"], e["canReturn"]) == (True, "10:00", "11:30", True), e
            assert rows[("backdated", "2026-11-25")]["lesson"]["lessonId"] == WED and rows[("backdated", "2026-11-23")]["lesson"]["lessonId"] == MON


def test_old_lines_stand_on_one_page_with_the_ordinary_ones_and_under_the_filters(pg_session, clock):
    """One old line (the late completion of 25.11, key stripped) and the lines of the new form on the SAME page: 200 and the same three entries; the filters of the dates and of the group see the old line by
    the date of its lesson (taken from the row of the lesson); the group filter of another group does not."""
    w, g = _world_with_entries(pg_session, clock, old=False)
    old = (w.db.query(GroupAttendanceAudit).filter(GroupAttendanceAudit.group_id == g.id, GroupAttendanceAudit.action == "updated", GroupAttendanceAudit.after_status == "completed")
           .order_by(GroupAttendanceAudit.created_at).first())
    old.lesson_date, old.lesson_id = None, None
    w.db.commit()
    w.db.expire_all()
    out = listing(w, status="decided")
    rows = audit_rows(out["items"])
    assert set(rows) == {("backdated", "2026-11-25"), ("backdated", "2026-11-23"), ("corrected", "2026-11-23")} and out["total"] == 3
    only_25 = audit_rows(listing(w, status="decided", dateFrom="2026-11-25", dateTo="2026-11-25")["items"])
    only_23 = audit_rows(listing(w, status="decided", dateFrom="2026-11-23", dateTo="2026-11-24")["items"])
    assert set(only_25) | set(only_23) == set(rows) and len(only_25) + len(only_23) == 3
    assert listing(w, status="decided", groupId=str(g.id))["total"] == 3
