"""The student's name in diagnostic invites follows the rest of the product: «Фамилия Имя» (Student.full_name),
the way the roster, the profile and the payments show it (staff names stay «Имя Фамилия»).

The first test needs no database; the other drives the real list / detail queries on the disposable ``a2_auth``
PostgreSQL (``needs_pg``).
"""
import os
from unittest.mock import MagicMock

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

import src.api.internal_router  # noqa: E402,F401  (import order: avoids a pre-existing router cycle)
from src.auth.schemas import DiagnosticCreateRequest  # noqa: E402
from src.auth.service import AuthService  # noqa: E402
from src.models.auth import Student  # noqa: E402

from tests.pg_world import needs_pg, pg_session, world  # noqa: E402,F401  (fixtures)


def test_created_invite_names_the_student_last_name_first_without_a_database():
    student = Student(first_name="Алия", last_name="Каримова", class_level=9)
    student.id = "00000000-0000-0000-0000-000000000001"
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = student
    out = AuthService(db).create_diagnostic_invite(
        "org-1", "creator-1", DiagnosticCreateRequest(student_id=str(student.id), class_level=9)
    )
    assert out.student_name == "Каримова Алия" == student.full_name


def test_a_missing_part_of_the_name_leaves_no_stray_space():
    student = Student(first_name="", last_name="Каримова", class_level=9)
    student.id = "00000000-0000-0000-0000-000000000002"
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = student
    out = AuthService(db).create_diagnostic_invite(
        "org-1", "creator-1", DiagnosticCreateRequest(student_id=str(student.id), class_level=9)
    )
    assert out.student_name == "Каримова"


@needs_pg
def test_create_list_and_detail_all_say_last_name_first(world):
    w = world
    student = w.students[0]  # Алия Каримова
    created = w.svc.create_diagnostic_invite(
        str(w.org.id), str(w.owner.id), DiagnosticCreateRequest(student_id=str(student.id), class_level=9)
    )
    listed, _ = w.svc.list_diagnostic_invites(str(w.org.id))
    listed_item = next(i for i in listed if i["id"] == created.id)
    detail = w.svc.get_diagnostic_invite(str(w.org.id), created.id, actor_id=None, is_org_admin=True)
    assert created.student_name == listed_item["student_name"] == detail["student_name"] == "Каримова Алия"
    assert created.student_name == student.full_name
