"""Wave E, E-D (DM-043): the refusals of ``add_student_to_group`` are typed; the import of a file and the return from the archive read the TYPE, never a word
of the message (PostgreSQL)."""
import uuid

import pytest

from src.auth import import_ops, ops_rules as R
from src.auth.student_archive import skip_code
from src.models.auth import Student, StudentGroup
from tests.pg_world import make_group, make_student, needs_pg, pg_session, world  # noqa: F401  (fixtures)

pytestmark = needs_pg


class Refusing:
    def __init__(self, error):
        self.error = error

    def add_student_to_group(self, *args, **kwargs):
        raise self.error


def new_loose_student(w, name="Новый"):
    student = Student(org_id=w.org.id, first_name=name, last_name="Вольный", class_level=9, status="new")
    w.db.add(student)
    w.db.commit()
    return student


def enrol_with(w, error):
    student = new_loose_student(w)
    item = {"existing": student, "group_name": w.group.name, "row": 2}
    return import_ops._enrol(w.db, Refusing(error), w.org.id, w.admin.id, item, w.group.id)


def test_the_import_knows_a_full_group_by_its_type(world):
    out = enrol_with(world, R.GroupFull(5, 7))
    assert out[:2] == ("skipped", "GROUP_FULL") and "лимит 5, занято 7" in out[2]


def test_the_same_words_from_a_plain_value_error_are_a_row_error(world):
    """On the old code any ValueError with «лимит» in it was GROUP_FULL (the substring rule)."""
    for words in ("В группе достигнут лимит учеников", "лимит чего-то другого", "Нельзя зачислить в архивную группу", "Архивного ученика нельзя перевести"):
        out = enrol_with(world, ValueError(words))
        assert out[:2] == ("skipped", "ROW_ERROR"), words


def test_the_import_knows_an_archived_group_and_student_by_their_types(world):
    assert enrol_with(world, R.GroupArchivedForEnrolment("x"))[:2] == ("skipped", "GROUP_ARCHIVED")
    assert enrol_with(world, R.StudentArchivedForEnrolment("x"))[:2] == ("skipped", "STUDENT_ARCHIVED")


def test_the_real_refusals_are_typed_and_keep_their_words(world):
    w = world
    org, group = str(w.org.id), w.group
    group.max_students = 3                                    # three students sit in it
    w.db.commit()
    loose = new_loose_student(w)
    with pytest.raises(R.GroupFull) as full:
        w.svc.add_student_to_group(org, str(group.id), str(loose.id), mode="add", money_right=True)
    assert str(full.value) == "В группе достигнут лимит учеников" and (full.value.limit, full.value.taken) == (3, 3)
    assert isinstance(full.value, ValueError) and skip_code(full.value) == "group_full"
    with pytest.raises(R.GroupNotFoundForEnrolment) as missing:
        w.svc.add_student_to_group(org, str(uuid.uuid4()), str(loose.id), mode="add", money_right=True)
    assert str(missing.value) == "Группа не найдена" and skip_code(missing.value) == "not_found"
    loose.status = "archived"
    w.db.commit()
    with pytest.raises(R.StudentArchivedForEnrolment) as archived:
        w.svc.add_student_to_group(org, str(group.id), str(loose.id), mode="add", money_right=True)
    assert str(archived.value) == "Архивного ученика нельзя перевести в группу"
