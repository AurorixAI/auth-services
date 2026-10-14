"""N13 P1.1: open lessons carry the teacher; the excuses summary counts them (PostgreSQL)."""
from src.auth.schemas import GroupUpdateRequest
from src.auth.service import AuthService

from tests.pg_world import (  # noqa: F401  (fixtures)
    business_today, excused_lesson, make_client, needs_pg, pg_session, reconcile, world,
)

pytestmark = needs_pg

OPEN = "/api/v1/auth/org/attendance/open-actions"
SUMMARY = "/api/v1/auth/org/attendance/excuses/summary"


def find(items, day, lesson_id, action_type):
    found = [
        i for i in items
        if i["lessonDate"] == day.isoformat() and i["lessonId"] == lesson_id and i["actionType"] == action_type
    ]
    assert len(found) == 1, (day, lesson_id, action_type, len(found))
    return found[0]


def change_group_teacher(w, teacher):
    w.svc.update_group(
        str(w.org.id), str(w.group.id), GroupUpdateRequest(teacher_id=str(teacher.id)),
        actor_id=str(w.admin.id), actor_role="admin",
    )


def test_teacher_name_present_for_both_action_types(world):
    day, lesson_id = reconcile(world, offset_days=1)
    items = make_client(world).get(OPEN).json()
    started = find(items, day, lesson_id, "in_progress")
    assert started["teacherId"] == str(world.teacher.id)
    assert started["teacherName"] == "Пётр Teacher"
    missed_day = day - (business_today() - day) * 2  # 3 days ago, never started
    missed = find(items, missed_day, f"s{missed_day.isoweekday()}", "missed")
    assert missed["teacherId"] == str(world.teacher.id)
    assert missed["teacherName"] == "Пётр Teacher"
    assert all(i["teacherName"] for i in items)


def test_in_progress_keeps_snapshot_teacher_missed_follows_group_teacher(world):
    day, lesson_id = reconcile(world, offset_days=1)
    change_group_teacher(world, world.teacher2)
    items = make_client(world).get(OPEN).json()
    started = find(items, day, lesson_id, "in_progress")
    assert started["teacherId"] == str(world.teacher.id)
    assert started["teacherName"] == "Пётр Teacher"
    missed_day = day - (business_today() - day) * 2  # 3 days ago, never started
    missed = find(items, missed_day, f"s{missed_day.isoweekday()}", "missed")
    assert missed["teacherId"] == str(world.teacher2.id)
    assert missed["teacherName"] == "Мария Teacher"


def test_in_progress_without_snapshot_falls_back_to_group_teacher(world):
    from src.models.auth import GroupAttendance

    day, lesson_id = reconcile(world, offset_days=1)
    world.db.query(GroupAttendance).filter_by(group_id=world.group.id).update({"teacher_id": None})
    world.db.commit()
    change_group_teacher(world, world.teacher2)
    started = find(make_client(world).get(OPEN).json(), day, lesson_id, "in_progress")
    assert started["teacherName"] == "Мария Teacher"


def test_names_are_resolved_in_one_batched_query(world):
    from sqlalchemy import event

    reconcile(world, offset_days=1)
    reconcile(world, offset_days=2)
    user_selects = []

    def count(conn, cursor, statement, *a):
        if statement.lstrip().upper().startswith("SELECT") and "FROM users" in statement and "IN (" in statement:
            user_selects.append(statement)

    engine = world.db.get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        items = world.svc.get_open_attendance_actions(str(world.org.id), actor_id=None, is_org_admin=True)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(items) > 10 and all(i.teacherName for i in items)
    assert len(user_selects) == 1


def test_summary_returns_open_lessons_total_and_keeps_pending_total(world):
    excused_lesson(world)  # one pending excuse, one started lesson
    client = make_client(world)
    body = client.get(SUMMARY).json()
    expected = len(client.get(OPEN).json())
    assert body["pending_total"] == 1
    assert body["open_lessons_total"] == expected and expected > 1


def test_summary_open_lessons_are_org_scoped(world):
    foreign = make_client(world, user=world.foreign_admin, org="other")
    items = foreign.get(OPEN).json()
    assert items and {i["groupId"] for i in items} == {str(world.foreign_group.id)}
    assert foreign.get(SUMMARY).json() == {"pending_total": 0, "open_lessons_total": len(items)}


def test_summary_survives_open_lessons_failure(world, monkeypatch):
    excused_lesson(world)

    def boom(self, *a, **kw):
        raise RuntimeError("schedule is broken")

    monkeypatch.setattr(AuthService, "get_open_attendance_actions", boom)
    r = make_client(world).get(SUMMARY)
    assert r.status_code == 200
    # Not counted is not zero: the client keeps its last value.
    assert r.json() == {"pending_total": 1, "open_lessons_total": None}


def test_summary_survives_a_real_sql_error_in_the_open_lessons_count(world, monkeypatch):
    from sqlalchemy import text

    excused_lesson(world)

    def poisoned(self, *a, **kw):
        # A genuine database error: the PostgreSQL transaction is aborted.
        self.db.execute(text("SELECT * FROM table_that_does_not_exist_n13"))

    monkeypatch.setattr(AuthService, "get_open_attendance_actions", poisoned)
    client = make_client(world)
    r = client.get(SUMMARY)
    assert r.status_code == 200
    assert r.json() == {"pending_total": 1, "open_lessons_total": None}
    monkeypatch.undo()
    # The session was rolled back and is usable; the next summary is complete.
    again = client.get(SUMMARY).json()
    assert again["pending_total"] == 1 and again["open_lessons_total"] > 1
    assert world.db.execute(text("SELECT 1")).scalar() == 1


def _statements_for_open_actions(w):
    from sqlalchemy import event

    statements = []
    engine = w.db.get_bind()

    def record(conn, cursor, statement, *a):
        statements.append(statement)

    w.db.expire_all()
    event.listen(engine, "before_cursor_execute", record)
    try:
        items = w.svc.get_open_attendance_actions(str(w.org.id), actor_id=None, is_org_admin=True)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return items, statements


def test_open_actions_query_count_does_not_grow_with_group_count(world):
    from tests.pg_world import make_group

    def add_groups(count, start):
        for i in range(count):
            group = make_group(world.db, world.org, world.teacher, f"Доп {start + i}")
            if i % 3 == 0:
                reconcile(world, offset_days=1, group=group)  # a started lesson in every third group
        world.db.commit()

    add_groups(2, 0)  # 3 active groups
    small_items, small = _statements_for_open_actions(world)
    add_groups(27, 2)  # 30 active groups
    big_items, big = _statements_for_open_actions(world)
    assert len(big_items) > len(small_items) > 0
    assert len(big) == len(small), (len(small), len(big))
    assert len(big) <= 8


def test_preloaded_schedule_gives_the_same_occurrences(world):
    from datetime import timedelta
    from src.auth.service import AuthService

    today = business_today()
    kw = dict(date_from=today - timedelta(days=30), date_to=today)
    plain = world.svc._resolved_occurrences_for_group(world.group, **kw)
    pre = world.svc._preload_schedule_data([world.group.id], **kw)
    assert world.svc._resolved_occurrences_for_group(world.group, preloaded=pre, **kw) == plain


def test_teacher_still_gets_403_on_summary(world):
    assert make_client(world, user=world.teacher).get(SUMMARY).status_code == 403


def test_teacher_open_actions_only_own_groups(world):
    reconcile(world, offset_days=1)
    mine = make_client(world, user=world.teacher).get(OPEN).json()
    others = make_client(world, user=world.teacher2).get(OPEN).json()
    assert mine and all(i["teacherId"] == str(world.teacher.id) for i in mine)
    assert others == []


# ---- N13 P1.2: the missed-lesson window is "from the 1st of the previous month" ----


def test_missed_window_start_covers_the_previous_month():
    from datetime import date

    from src.auth.service import missed_lesson_window_start

    assert missed_lesson_window_start(date(2026, 10, 15)) == date(2026, 9, 1)
    assert missed_lesson_window_start(date(2026, 11, 1)) == date(2026, 10, 1)
    assert missed_lesson_window_start(date(2026, 10, 31)) == date(2026, 9, 1)
    assert missed_lesson_window_start(date(2027, 1, 5)) == date(2026, 12, 1)  # year boundary
    assert missed_lesson_window_start(date(2026, 3, 31)) == date(2026, 2, 1)  # short previous month


def test_open_actions_missed_lessons_start_on_the_first_of_the_previous_month(world):
    from datetime import datetime

    from src.auth.service import BUSINESS_TIMEZONE

    def missed_dates(now):
        items = world.svc.get_open_attendance_actions(
            str(world.org.id), actor_id=None, is_org_admin=True, now=now
        )
        return sorted(i.lessonDate for i in items if i.actionType == "missed")

    # 15.10 (a Thursday, after the lessons ended): the whole of September is covered.
    dates = missed_dates(datetime(2026, 10, 15, 23, 0, tzinfo=BUSINESS_TIMEZONE))
    assert dates[0] == "2026-09-01" and dates[-1] == "2026-10-15"
    assert "2026-08-31" not in dates
    # 01.11, early morning: October is covered from its 1st, the day itself has no past lesson yet.
    dates = missed_dates(datetime(2026, 11, 1, 5, 0, tzinfo=BUSINESS_TIMEZONE))
    assert dates[0] == "2026-10-01" and dates[-1] == "2026-10-31"
    assert not any(d.startswith("2026-09") for d in dates)
    # The preload stays O(1): a window of two months costs the same queries as before.
    _, statements = _statements_for_open_actions(world)
    assert len(statements) <= 8
