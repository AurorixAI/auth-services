"""N13 P1.3: "My students" on the teacher dashboard counts people, not seats (PostgreSQL)."""
from datetime import datetime, timedelta

import pytest

from src.models.auth import Group, Student, StudentGroup

from tests.pg_world import (  # noqa: F401  (fixtures)
    make_client, make_group, make_student, needs_pg, pg_session, world,
)

pytestmark = needs_pg

SUMMARY = "/api/v1/auth/org/teacher/dashboard/summary"


def summary(w, teacher=None):
    teacher = teacher or w.teacher
    response = make_client(w, user=teacher, org_role="teacher").get(SUMMARY)
    assert response.status_code == 200, response.text
    return response.json()


def seat(w, student, group, **kw):
    w.db.add(StudentGroup(student_id=student.id, group_id=group.id, is_active=True,
                          joined_at=datetime.now() - timedelta(days=5), **kw))
    w.db.commit()


@pytest.fixture
def two_groups(world):
    """The world's group has three students; the same teacher also has a second group."""
    world.second = make_group(world.db, world.org, world.teacher, "Вторая")
    world.db.commit()
    return world


def test_a_student_of_two_groups_of_one_teacher_is_one_student(two_groups):
    w = two_groups
    make_student(w.db, w.org, w.second, "Только", "Вторая", "N13-010")
    w.db.commit()
    seat(w, w.students[0], w.second)  # Алия sits in both groups

    data = summary(w)

    per_group = {g["name"]: g["student_count"] for g in data["groups"]}
    assert per_group == {"Группа": 3, "Вторая": 2}  # rosters stay per group
    assert sum(per_group.values()) == 5  # what the old client added up
    assert data["studentCount"] == 4  # 3 + 1 only in the second group, Алия once


def test_one_student_in_every_group_counts_once(two_groups):
    w = two_groups
    for student in w.students:
        seat(w, student, w.second)

    data = summary(w)

    assert sorted(g["student_count"] for g in data["groups"]) == [3, 3]
    assert data["studentCount"] == 3


def test_without_overlap_it_is_the_sum_of_the_rosters(two_groups):
    w = two_groups
    make_student(w.db, w.org, w.second, "Один", "Второй", "N13-011")
    make_student(w.db, w.org, w.second, "Два", "Второй", "N13-012")
    w.db.commit()

    assert summary(w)["studentCount"] == 5


def test_a_left_membership_does_not_count(two_groups):
    w = two_groups
    seat(w, w.students[0], w.second)
    row = w.db.query(StudentGroup).filter_by(student_id=w.students[1].id, group_id=w.group.id).one()
    row.is_active = False
    row.left_at = datetime.now()
    w.db.commit()

    data = summary(w)

    assert {g["name"]: g["student_count"] for g in data["groups"]} == {"Группа": 2, "Вторая": 1}
    assert data["studentCount"] == 2  # Алия (both groups) and Дана


def test_an_archived_group_is_not_counted(two_groups):
    w = two_groups
    make_student(w.db, w.org, w.second, "Из", "Архива", "N13-013")
    w.db.commit()
    w.second.status = "archived"
    w.db.commit()

    data = summary(w)

    assert [g["name"] for g in data["groups"]] == ["Группа"]
    assert data["studentCount"] == 3


def test_another_teachers_students_are_not_mine_and_each_teacher_counts_them_once(two_groups):
    w = two_groups
    theirs = make_group(w.db, w.org, w.teacher2, "Чужая")
    w.db.commit()
    seat(w, w.students[0], theirs)  # Алия is also taught by the other teacher
    make_student(w.db, w.org, theirs, "Чужой", "Ученик", "N13-014")
    w.db.commit()

    assert summary(w)["studentCount"] == 3
    assert summary(w, w.teacher2)["studentCount"] == 2


def test_a_teacher_without_groups_has_no_students(world):
    data = summary(world, world.teacher2)

    assert data["groups"] == []
    assert data["studentCount"] == 0
