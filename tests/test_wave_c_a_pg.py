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
