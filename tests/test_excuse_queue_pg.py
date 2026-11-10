"""The excuses queue: every pending excuse of every period, one definition for the badge and the list (PostgreSQL)."""
from datetime import timedelta

import pytest
from sqlalchemy import text

from src.auth.attendance_excuses import AttendanceExcuseService
from src.models.auth import AttendanceExcuse, GroupAttendance

from tests.pg_world import excused_lesson, make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)

pytestmark = needs_pg

BASE = "/api/v1/auth/org/attendance/excuses"
LONG_AGO = 40  # always before the first day of the current month (any month has <= 31 days)


def attendance_of(w, excuse_id):
    w.db.expire_all()
    excuse = w.db.get(AttendanceExcuse, excuse_id)
    return w.db.get(GroupAttendance, excuse.attendance_id)


def two_lessons(w):
    """Student 1 excused on a lesson 3 days ago, student 2 on one 5 days ago (two pending excuses)."""
    from tests.pg_world import reconcile, save

    out = []
    for offset, student in ((3, w.students[0]), (5, w.students[1])):
        day, lesson_id = reconcile(w, offset)
        others = [s for s in w.students if s.id != student.id]
        save(w, day, lesson_id, [(student, "excused")] + [(s, "present") for s in others],
             excuseReasons={student.id: "Болел"})
        out.append(day)
    return out


def test_pending_of_a_past_month_is_in_the_queue_without_dates(world):
    excused_lesson(world, offset_days=LONG_AGO)
    client = make_client(world)
    body = client.get(BASE, params={"status": "pending"}).json()
    assert body["total"] == 1 and len(body["items"]) == 1 and body["pending_total"] == 1
    assert client.get(f"{BASE}/summary").json()["pending_total"] == 1
    # decided statuses keep the window: the same excuse is not in the current month
    assert client.get(BASE, params={"status": "all"}).json()["total"] == 0


def test_badge_equals_rows_equals_list_counter_orphan_counts_nowhere_archived_counts(world):
    excused_lesson(world, offset_days=LONG_AGO)          # a past month
    two_lessons(world)                                    # two this month (or the previous one)
    client = make_client(world)
    assert client.get(f"{BASE}/summary").json()["pending_total"] == 3

    # an orphan: its attendance row is gone (the constraints are switched off for this one delete)
    victim = world.db.query(AttendanceExcuse).filter_by(status="pending", org_id=world.org.id).first()
    att = attendance_of(world, victim.id)
    world.db.execute(text("SET session_replication_role = replica"))
    world.db.execute(text("DELETE FROM group_attendance WHERE id = :i"), {"i": att.id})
    world.db.execute(text("SET session_replication_role = DEFAULT"))
    world.db.commit()

    world.group.status = "archived"
    world.db.commit()
    body = client.get(BASE, params={"status": "pending"}).json()
    summary = client.get(f"{BASE}/summary").json()["pending_total"]
    assert len(body["items"]) == body["total"] == body["pending_total"] == summary == 2
    assert all(i["group"]["archived"] is True for i in body["items"])
    # the counter of a decided tab is the same set
    assert client.get(BASE, params={"status": "confirmed"}).json()["pending_total"] == 2


def test_group_archived_flag_is_false_for_a_live_group(world):
    excused_lesson(world)
    items = make_client(world).get(BASE, params={"status": "pending"}).json()["items"]
    assert items and items[0]["group"]["archived"] is False


def test_equal_deadline_orders_by_lesson_date_ascending(world, monkeypatch):
    first, second = two_lessons(world)  # lesson dates: 3 days ago, then 5 days ago
    monkeypatch.setattr(AttendanceExcuseService, "_deadlines", lambda self, rows, ctx: {})
    items = make_client(world).get(BASE, params={"status": "pending"}).json()["items"]
    assert [i["lesson_date"] for i in items] == sorted([first.isoformat(), second.isoformat()])
    assert items[0]["lesson_date"] < items[1]["lesson_date"]


def test_sooner_deadline_comes_first_and_pages_keep_the_order(world, monkeypatch):
    first, second = two_lessons(world)

    def deadlines(self, rows, ctx):
        # the LATER lesson gets the SOONER deadline
        days = {a.lesson_date: (1 if a.lesson_date == first.isoformat() else 0) for _, a, _, _ in rows}
        return {e.id: {"kind": "date", "date": a.lesson_date, "overdue": False, "days_left": days[a.lesson_date]}
                for e, a, _, _ in rows}

    monkeypatch.setattr(AttendanceExcuseService, "_deadlines", deadlines)
    client = make_client(world)
    page1 = client.get(BASE, params={"status": "pending", "limit": 1}).json()
    page2 = client.get(BASE, params={"status": "pending", "limit": 1, "offset": 1}).json()
    assert page1["total"] == 2 and page1["items"][0]["lesson_date"] == second.isoformat()
    assert page2["items"][0]["lesson_date"] == first.isoformat()


def test_students_list_has_created_at(world):
    body = make_client(world).get("/api/v1/auth/org/students").json()
    assert body and all(isinstance(s["created_at"], str) and "T" in s["created_at"] for s in body)
