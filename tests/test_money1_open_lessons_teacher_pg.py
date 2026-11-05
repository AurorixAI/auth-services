"""Money-1 №18 (front need): every item of ``open_lesson_items`` - the list of the lessons without a mark that the owner is warned with before a month is closed - names the teacher: ``teacherId``.

The same item is in three answers (the preview of the month, the list of the month, the question of the save ``409 OPEN_LESSONS_CONFIRM_REQUIRED``): the front opens the journal for that teacher and the
question names the lessons of ONE teacher (PostgreSQL)."""
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_logicA_payroll_pg import API, LESSONS, fp, g_pay_world, owner
from tests.test_pay2_api_pg import fixed_today  # noqa: E402,F401  (fixture)
from tests.test_payroll_pg import lesson as lesson_row

pytestmark = needs_pg
ITEM_KEYS = {"lessonDate", "groupId", "groupName", "lessonId", "kind", "teacherId"}


def unresolved(world, m, number):
    return lesson_row(world, m.d(number), {world.students[0]: "present"}, status="in_progress")


def test_the_items_of_the_preview_the_list_and_the_question_of_the_save_name_the_teacher(world, fixed_today):
    m, s1, s2 = g_pay_world(world)
    day = [d for d in range(1, 29) if d not in LESSONS][0]
    row = unresolved(world, m, day)
    want = {"lessonDate": row.lesson_date, "groupId": str(world.group.id), "groupName": world.group.name, "lessonId": row.lesson_id, "kind": "in_progress", "teacherId": str(world.teacher.id)}
    preview = owner(world).post(f"{API}/org/payroll/months/{m.label}/calculate", json={"teacher_ids": [str(world.teacher.id)]}).json()["sheets"][0]["warnings"]["open_lesson_items"]
    assert preview == [want]
    listed = owner(world).get(f"{API}/org/payroll/months/{m.label}").json()["teachers"][0]["warnings"]["open_lesson_items"]
    assert listed == [want]
    asked = owner(world).post(f"{API}/org/payroll/sheets", json={"month": m.label, "teacher_id": str(world.teacher.id), "expected_fingerprint": fp(world, m), "confirm_open_lessons": False})
    assert asked.status_code == 409, asked.text
    detail = asked.json()["detail"]
    assert detail["code"] == "OPEN_LESSONS_CONFIRM_REQUIRED" and detail["count"] == 1
    assert detail["items"] == [want] and set(detail["items"][0]) == ITEM_KEYS
    assert detail["items"][0]["teacherId"] == str(world.teacher.id)        # the teacher of the question is the teacher of every item


def test_a_lesson_of_another_teacher_is_named_with_his_own_id(world, fixed_today):
    m, s1, s2 = g_pay_world(world)
    day = [d for d in range(1, 29) if d not in LESSONS][1]
    other = lesson_row(world, m.d(day), {world.students[0]: "present"}, teacher=world.teacher2, status="in_progress")
    overview = owner(world).get(f"{API}/org/payroll/months/{m.label}").json()["teachers"]
    items = {t["teacher_id"]: t["warnings"]["open_lesson_items"] for t in overview}
    assert [i["teacherId"] for i in items[str(world.teacher2.id)]] == [str(world.teacher2.id)] and items.get(str(world.teacher.id), []) == []
    assert other.lesson_date == items[str(world.teacher2.id)][0]["lessonDate"]
