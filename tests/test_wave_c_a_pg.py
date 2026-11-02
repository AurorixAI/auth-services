"""Wave C, package C-A: the hot read paths do not grow with the size of the centre (LESSONS 81), and give the SAME answer as before.

Every test builds a small centre and a big one (30-40 groups) and asserts that the number of SQL statements of the path is the same;
where a path was rewritten, the answer is compared with the answer of the untouched single-row path (the old behaviour).
"""
import pytest

from tests.pg_world import (  # noqa: F401
    capture_queries, count_queries, make_client, make_group, make_member, make_student, make_user, needs_pg, pg_session, world,
)

pytestmark = needs_pg

API = "/api/v1/auth"


def grow_centre(w, groups=40, per=3, teachers=5):
    """Add ``groups`` groups of ``per`` students; their teachers are spread over ``teachers`` people (the old ``world`` teacher among them)."""
    pool = [w.teacher, w.teacher2] + [make_user(w.db, "teacher", f"Учитель{i}") for i in range(max(0, teachers - 2))]
    for user in pool[2:]:
        make_member(w.db, w.org, user, "teacher")
    made = []
    for g in range(groups):
        group = make_group(w.db, w.org, pool[g % len(pool)], f"Группа {g:02d}")
        for s in range(per):
            make_student(w.db, w.org, group, f"Имя{g}-{s}", f"Фамилия{g}-{s}")
        made.append(group)
    w.db.commit()
    return made


def test_the_counter_helper_counts_and_captures(world):
    n, rows = count_queries(world, lambda: world.db.execute(__import__("sqlalchemy").text("select 1")).all())
    assert n == 1 and rows
    statements, _ = capture_queries(world.db, lambda: world.db.execute(__import__("sqlalchemy").text("select 2")))
    assert statements == ["select 2"]


# ---- SV-08: GET /org/groups -------------------------------------------------------------------------------------------------------------------


def groups_json(w, user=None):
    r = make_client(w, user=user or w.admin).get(f"{API}/org/groups")
    assert r.status_code == 200, r.text
    return r.json()


def test_sv08_org_groups_queries_do_not_grow_with_groups(world):
    small, _ = count_queries(world, lambda: groups_json(world))
    grow_centre(world, groups=40)
    big, body = count_queries(world, lambda: groups_json(world))
    print(f"[wca] SV-08 GET /org/groups: 1 group {small} SQL, 41 groups {big} SQL")
    assert len(body) == 41 and big == small <= 6


def test_sv08_org_groups_answer_equals_the_single_group_answer(world):
    made = grow_centre(world, groups=8)
    made[0].teacher_id = None                       # a group without a teacher
    world.students[0].archived_at = None
    from src.models.auth import StudentGroup

    world.db.query(StudentGroup).filter_by(student_id=world.students[1].id).update({"is_active": False})   # a left student is not counted
    world.db.commit()
    body = groups_json(world)
    expected = [world.svc._build_group_response(g).model_dump(mode="json") for g in
                sorted([world.group, *made], key=lambda g: g.name)]
    assert [dict(item) for item in body] == [dict(item) for item in expected]
    assert next(g for g in body if g["id"] == str(world.group.id))["student_count"] == 2
    assert next(g for g in body if g["id"] == str(made[0].id))["teacher_name"] is None


def test_sv08_my_groups_of_a_teacher_do_not_grow_and_keep_the_flag(world):
    grow_centre(world, groups=2, teachers=2)
    small, small_body = count_queries(world, lambda: groups_json(world, world.teacher))
    grow_centre(world, groups=40, teachers=2)
    big, body = count_queries(world, lambda: groups_json(world, world.teacher))
    print(f"[wca] SV-08 my groups: {len(small_body)} groups {small} SQL, {len(body)} groups {big} SQL")
    assert len(body) > len(small_body) + 15 and big == small
    assert all(item["substitution"] is False and item["teacher_name"] == body[0]["teacher_name"] for item in body)


# ---- SV-09: GET /org/schedule -------------------------------------------------------------------------------------------------------------------


def revise_schedules(w, groups):
    """Every group gets: a revision of s1 (applied) and one from the far future (not applied), a stopped s2, an s3 that stops in the far future."""
    from src.models.auth import GroupScheduleOverride

    for group in groups:
        def add(lesson, scope, day, payload):
            w.db.add(GroupScheduleOverride(org_id=w.org.id, group_id=group.id, lesson_id=lesson, scope=scope, effective_date=day,
                                           payload=payload, reason="тест", changed_by=w.admin.id))
        add("s1", "recurring", "2026-01-05", {"dayOfWeek": 1, "startTime": "15:00", "endTime": "16:00", "format": "online"})
        add("s1", "recurring", "2999-01-01", {"dayOfWeek": 1, "startTime": "18:00", "endTime": "19:00", "format": "offline"})
        add("s2", "recurring_end", "2026-01-05", {})
        add("s3", "recurring_end", "2999-01-01", {})
    w.db.commit()


def schedule_json(w):
    r = make_client(w).get(f"{API}/org/schedule")
    assert r.status_code == 200, r.text
    return r.json()


def test_sv09_schedule_queries_do_not_grow_with_groups(world):
    revise_schedules(world, [world.group])
    small, _ = count_queries(world, lambda: schedule_json(world))
    made = grow_centre(world, groups=40)
    revise_schedules(world, made)
    big, body = count_queries(world, lambda: schedule_json(world))
    print(f"[wca] SV-09 GET /org/schedule: 1 group {small} SQL, 41 groups {big} SQL")
    assert len({item["groupId"] for item in body}) == 41 and big == small


def test_sv09_schedule_answer_equals_the_per_group_query_answer(world, monkeypatch):
    made = grow_centre(world, groups=6)
    revise_schedules(world, [world.group, *made[:4]])           # the rest has no overrides at all
    body = schedule_json(world)
    assert any(item["startTime"] == "15:00" for item in body)                                  # the revision is applied ...
    assert not any(item["startTime"] == "18:00" for item in body)                              # ... the future one is not
    assert not any(item["id"] == "s2" for item in body if item["groupId"] == str(world.group.id))   # a stopped recurrence is omitted
    assert any(item["id"] == "s3" for item in body if item["groupId"] == str(world.group.id))       # one that stops later is still there
    original = type(world.svc)._schedule_overrides_for_group

    def per_group(self, group, *, until, extra_lesson_ids=None, preloaded_overrides=None):
        return original(self, group, until=until, extra_lesson_ids=extra_lesson_ids)       # the OLD path: one query per group

    monkeypatch.setattr(type(world.svc), "_schedule_overrides_for_group", per_group)
    assert schedule_json(world) == body


# ---- SV-10: a student's attendance reads only the groups he was in --------------------------------------------------------------------------------


def add_lessons(w, group, student_ids, day="2026-09-14", lessons=3, mark="present", snapshot=True):
    """``lessons`` completed lessons of the group on consecutive days; ``student_ids`` are in the frozen roster (and marked ``mark``)."""
    from datetime import date, timedelta

    from src.models.auth import GroupAttendance

    first = date.fromisoformat(day)
    for i in range(lessons):
        w.db.add(GroupAttendance(
            org_id=w.org.id, group_id=group.id, lesson_date=(first + timedelta(days=i)).isoformat(), lesson_id=f"L{i}", topic=f"Тема {i}", status="completed",
            records={str(s): mark for s in student_ids},
            roster_snapshot=[{"student_id": str(s), "name": "x"} for s in student_ids] if snapshot else None,
            marked_by=w.teacher.id, teacher_id=group.teacher_id,
        ))
    w.db.commit()


def student_attendance(w, student, month="2026-09"):
    r = make_client(w).get(f"{API}/org/students/{student.id}/attendance?month={month}")
    assert r.status_code == 200, r.text
    return r.json()


def test_sv10_student_attendance_reads_only_his_groups(world):
    me = world.students[0]
    others = grow_centre(world, groups=30)
    add_lessons(world, world.group, [me.id, world.students[1].id], lessons=4)
    for g in others:
        add_lessons(world, g, [], lessons=6)                                   # a big centre: 30 groups x 6 lessons of strangers
    statements, body = capture_queries(world, lambda: student_attendance(world, me), rows=True)
    journal = [(s, n) for s, n in statements if "FROM group_attendance JOIN groups" in s]       # the month's journal
    print(f"[wca] SV-10 journal rows read for a student of a 31-group centre: {[n for _, n in journal]}")
    assert len(journal) == 1 and journal[0][1] == 4                            # only the 4 lessons of HIS group, not 4 + 180
    assert len(body["lessons"]) == 4


def test_sv10_student_attendance_answer_keeps_closed_groups_and_other_rosters(world):
    from src.models.auth import StudentGroup

    me, other = world.students[0], world.students[1]
    old_group = make_group(world.db, world.org, world.teacher2, "Старая группа")
    world.db.add(StudentGroup(student_id=me.id, group_id=old_group.id, is_active=False, joined_at=__import__("tests.pg_world", fromlist=["x"]).ENROLLED_SINCE))
    world.db.commit()
    add_lessons(world, old_group, [me.id], day="2026-09-01", lessons=2, mark="absent")        # a group he has LEFT is still his history
    add_lessons(world, world.group, [me.id], day="2026-09-10", lessons=3, mark="late")
    add_lessons(world, world.group, [other.id], day="2026-09-20", lessons=2, mark="present")  # lessons without him in the roster are not his (other lesson ids)
    add_lessons(world, world.group, [me.id], day="2026-09-25", lessons=1, mark="present", snapshot=False)   # a legacy row: no frozen roster, he is in the records
    body = student_attendance(world, me)
    assert [item["date"] for item in body["lessons"]] == ["2026-09-01", "2026-09-02", "2026-09-10", "2026-09-11", "2026-09-12", "2026-09-25"]
    assert {g["group_name"] for g in body["groups"]} == {"Старая группа", world.group.name}
