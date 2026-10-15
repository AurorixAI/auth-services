"""The student card / list / payments headers show «Фамилия Имя» - StudentResponse.full_name follows Student.full_name.  No database."""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")


def test_student_response_full_name_is_last_then_first():
    from src.auth.service import AuthService
    from src.models.auth import Student

    student = Student(first_name="QA", last_name="Claude Тест", class_level=9)
    out = AuthService(None)._build_student_response(student)
    assert out.full_name == "Claude Тест QA" == student.full_name
    assert (out.first_name, out.last_name) == ("QA", "Claude Тест")
