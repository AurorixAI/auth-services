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
