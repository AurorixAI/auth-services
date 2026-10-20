"""The payments summary (menu badge / «N учеников ждут оплаты» banner) also counts DISTINCT students (PostgreSQL).

``due`` / ``expired`` / ... count memberships, so a student in two groups counts twice there; ``students_due`` and
``students_expired`` are added next to them, with the same definitions, and the old fields keep their meaning.
"""
from sqlalchemy import event

from src.models.auth import StudentGroup

from tests.pg_world import (  # noqa: F401  (fixtures)
    make_client, make_group, make_student, needs_pg, pg_session, world,
)
from tests.test_payments_service_pg import API, day, pay, summary

pytestmark = needs_pg


def second_group(w, *students):
    group = make_group(w.db, w.org, w.teacher2, "Вторая")
    w.db.commit()
    for student in students:
        w.svc.add_student_to_group(str(w.org.id), str(group.id), str(student.id), mode="add")
    return group


def scenario(w):
    """Six memberships of four students: s0 expired in both groups, s1 ends today in A and is expired in B,
    s2 paid, s3 ends today in A."""
    s0, s1, s2 = w.students
    s3 = make_student(w.db, w.org, w.group, "Новый", "Ученик", "N13-004")
    w.db.commit()
    b = second_group(w, s0, s1)
    pay(w, s0, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))
    pay(w, s0, group=b, paid_on=day(-30), valid_from=day(-30), valid_to=day(-2))
    pay(w, s1, paid_on=day(-30), valid_from=day(-30), valid_to=day(0))
    pay(w, s1, group=b, paid_on=day(-30), valid_from=day(-30), valid_to=day(-3))
    pay(w, s2, paid_on=day(-5), valid_from=day(-5), valid_to=day(25))
    pay(w, s3, paid_on=day(-30), valid_from=day(-30), valid_to=day(0))
    return s3, b


def test_a_student_in_two_groups_counts_once_in_the_new_fields_and_twice_in_the_old_ones(world):
    scenario(world)
    s = summary(world)
    # the old fields: memberships, unchanged
    assert (s["due"], s["ends_today"], s["expired"], s["expiring"], s["none"], s["active"], s["free"], s["tracked_total"]) == (
        5, 2, 3, 0, 0, 1, 0, 6)
    # the new fields: students (s0 and s1 are in two groups each)
    assert s["students_due"] == 3          # s0, s1, s3 - a membership that needs payment each
    assert s["students_expired"] == 2      # s0 (twice expired) and s1 (expired in B only)


def test_one_student_with_two_expired_memberships_is_one_student(world):
    s0 = world.students[0]
    b = second_group(world, s0)
    pay(world, s0, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))
    pay(world, s0, group=b, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))
    s = summary(world)
    assert (s["due"], s["expired"], s["students_due"], s["students_expired"]) == (2, 2, 1, 1)


def test_a_student_who_only_ends_today_is_due_but_not_expired(world):
    pay(world, world.students[0], paid_on=day(-30), valid_from=day(-30), valid_to=day(0))
    s = summary(world)
    assert (s["due"], s["ends_today"], s["expired"]) == (1, 1, 0)
    assert (s["students_due"], s["students_expired"]) == (1, 0)


def test_nobody_due_gives_zeros(world):
    s = summary(world)
    assert (s["due"], s["expired"], s["students_due"], s["students_expired"]) == (0, 0, 0, 0)


def test_the_new_fields_follow_the_tracked_memberships_like_the_old_ones(world):
    scenario(world)
    s1 = world.students[1]
    # s1's only expired membership (group B) is closed: it is no longer tracked
    world.db.query(StudentGroup).filter(
        StudentGroup.student_id == s1.id, StudentGroup.group_id != world.group.id
    ).update({"is_active": False})
    world.db.commit()
    s = summary(world)
    assert (s["due"], s["expired"], s["tracked_total"]) == (4, 2, 5)
    assert (s["students_due"], s["students_expired"]) == (3, 1)      # s1 is still due (ends today in A), no longer expired


def test_the_summary_costs_the_same_number_of_queries_however_many_students_there_are(world):
    s3, b = scenario(world)

    def measured():
        world.db.expire_all()
        statements, engine = [], world.db.get_bind()

        def record(conn, cursor, statement, *a):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            body = summary(world)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return body, len(statements)

    small, small_count = measured()
    for n in range(5, 11):
        extra = make_student(world.db, world.org, world.group, f"Имя{n}", f"Фамилия{n}", f"N13-0{n:02d}")
        world.db.commit()
        world.svc.add_student_to_group(str(world.org.id), str(b.id), str(extra.id), mode="add")
        pay(world, extra, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))
        pay(world, extra, group=b, paid_on=day(-30), valid_from=day(-30), valid_to=day(-1))
    big, big_count = measured()
    assert (big["students_due"], big["students_expired"]) == (small["students_due"] + 6, small["students_expired"] + 6)
    assert big["due"] == small["due"] + 12                          # the same six students, twice each
    assert big_count == small_count, (small_count, big_count)


def test_the_permissions_of_the_summary_are_unchanged(world):
    """No new permission (the full matrix is test_n13_p2_permissions): the owner reads the new fields, a teacher and an
    admin without the payments switch are still refused."""
    scenario(world)
    url = f"{API}/org/payments/summary"
    owner = make_client(world, user=world.owner).get(url)
    assert owner.status_code == 200 and (owner.json()["students_due"], owner.json()["students_expired"]) == (3, 2)
    assert make_client(world, user=world.teacher).get(url).status_code == 403
    assert make_client(world, user=world.admin).get(url).status_code == 403
