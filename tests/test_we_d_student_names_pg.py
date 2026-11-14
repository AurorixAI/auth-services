"""Wave E, E-D (SV-13): one student name in every list the group pages and the attendance journal build - «Фамилия Имя», trimmed, «Ученик» when
both parts are empty (``roster_names.student_full_name``).  Before E-D ``service.py`` built it five ways: the group list kept a trailing space and
printed a blank for an empty name, the student check trimmed but had no fallback, the three journal builders had the fallback."""
from datetime import timedelta

import pytest

from src.auth.business_clock import utc_now
from src.models.auth import Student, StudentGroup
from tests.pg_world import (  # noqa: F401  (fixtures)
    business_today, make_group, make_student, needs_pg, pg_session, reconcile, save, world,
)

pytestmark = needs_pg

NAMES = [("Иванов", "Иван", "Иванов Иван"), ("Иванов", "", "Иванов"), ("", "Иван", "Иван"), ("", "", "Ученик"), (" ", " ", "Ученик")]


def _rename(w, student, last, first):
    student.last_name, student.first_name = last, first
    w.db.commit()


@pytest.mark.parametrize("last,first,expected", NAMES)
def test_every_builder_prints_the_same_name(world, last, first, expected):
    w = world
    s = w.students[0]
    _rename(w, s, last, first)
    detail = w.svc.get_group_detail(str(w.org.id), str(w.group.id), actor_id=str(w.admin.id), is_org_admin=True)
    in_group = next(x for x in detail["students"] if x["id"] == str(s.id))["full_name"]
    checked = w.svc.verify_group_student(str(w.group.id), student_id=s.display_id)["student"]["full_name"]
    now_roster = next(x for x in w.svc._active_attendance_roster(w.group.id) if x["student_id"] == str(s.id))["full_name"]
    then_roster = next(x for x in w.svc._attendance_roster_at(w.group.id, utc_now() + timedelta(minutes=1)) if x["student_id"] == str(s.id))["full_name"]
    assert in_group == checked == now_roster == then_roster == expected


def test_a_former_student_of_the_journal_has_the_same_name(world):
    w = world
    s1 = w.students[0]
    day, lesson_id = reconcile(w)
    save(w, day, lesson_id, [(s, "present") for s in w.students], status="completed")
    _rename(w, s1, "", "")
    w.svc.remove_student_from_group(str(w.org.id), str(w.group.id), str(s1.id), str(w.owner.id))
    w.db.commit()
    today = business_today()
    journal = w.svc.get_group_attendance(str(w.org.id), str(w.group.id), date_from=(today - timedelta(days=30)).isoformat(), date_to=today.isoformat(),
                                         actor_id=str(w.admin.id), is_org_admin=True)
    former = [x for x in journal.historicalStudents if x["id"] == str(s1.id)]
    assert former and former[0]["full_name"] == "Ученик"


@pytest.mark.parametrize("typed,found", [("%", 0), ("_", 0), ("\\", 0), ("кари", 1), ("КАРИМ%", 0), ("N13-001", 1), ("N13_001", 0)])
def test_the_search_box_treats_masks_as_plain_characters(world, typed, found):
    """The student search builds its pattern with ``sql_text.contains`` (E-D): «%» and «_» are characters, not masks."""
    w = world
    page, total = w.svc.list_students_page(str(w.org.id), search=typed, actor_id=str(w.admin.id), is_org_admin=True)
    assert total == found
