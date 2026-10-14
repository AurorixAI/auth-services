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
    assert r.json() == {"pending_total": 1, "open_lessons_total": 0}


def test_teacher_still_gets_403_on_summary(world):
    assert make_client(world, user=world.teacher).get(SUMMARY).status_code == 403


def test_teacher_open_actions_only_own_groups(world):
    reconcile(world, offset_days=1)
    mine = make_client(world, user=world.teacher).get(OPEN).json()
    others = make_client(world, user=world.teacher2).get(OPEN).json()
    assert mine and all(i["teacherId"] == str(world.teacher.id) for i in mine)
    assert others == []
