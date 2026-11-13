"""Money-1 Д-5 (R3B м4, rule 123): the right to the money has NO permissive default - ``add_student_to_group`` and ``bulk_add_students`` get it from the caller, the import and the restore
from the archive say «no money» explicitly; an import that meets a student who has just joined another group does not transfer him (a transfer carries his paid days)."""
import inspect

from src.auth import import_ops
from src.auth.service import AuthService
from src.models.auth import StudentGroup

from tests.pg_world import make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)

pytestmark = needs_pg


def test_d5_the_money_right_of_an_enrolment_has_no_default():
    for name in ("add_student_to_group", "bulk_add_students"):
        assert inspect.signature(getattr(AuthService, name)).parameters["money_right"].default is inspect.Parameter.empty, name


def test_d5_an_import_row_of_a_student_who_joined_another_group_meanwhile_is_skipped_not_transferred(world, monkeypatch):
    w = world
    other = make_group(w.db, w.org, w.teacher, "Группа B")
    student = w.students[0]
    item = {"existing": student, "group_name": other.name, "opening": None, "first": "", "last": "", "class_level": 9, "phone": None, "comment": ""}
    monkeypatch.setattr(import_ops, "_live_groups_of", lambda db, org_id, ids: {})      # the check before the enrolment saw nothing: the student joins A between it and the enrolment
    answer = import_ops._enrol(w.db, w.svc, w.org.id, w.owner.id, item, other.id)
    assert answer[:2] == ("skipped", "IN_OTHER_GROUP"), answer
    w.db.expire_all()
    assert [(m.group_id, m.is_active) for m in w.db.query(StudentGroup).filter_by(student_id=student.id)] == [(w.group.id, True)]      # before: he was TRANSFERRED to B with his paid days
