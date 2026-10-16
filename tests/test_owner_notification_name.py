"""Staff are written «Имя Фамилия» everywhere (owner decision 15.10): the platform admin's «new application» mail names
the owner that way too, not by the server's student-order ``User.full_name``.  No database."""
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")


def test_new_application_mail_names_the_owner_first_name_first():
    from src.auth.schemas import OrgApplicationRequest
    from src.auth.service import AuthService

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None  # the e-mail is free
    request = OrgApplicationRequest(
        org_name="Центр Тест", email="owner@example.com", password="secret12",
        first_name="Арслан", last_name="Джемилов",
    )
    with patch("src.auth.service.send_admin_new_application") as send:
        AuthService(db).apply_organization(request)

    assert send.call_count == 1
    assert send.call_args.kwargs["owner_name"] == "Арслан Джемилов"
    assert send.call_args.kwargs["owner_email"] == "owner@example.com"
