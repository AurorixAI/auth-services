"""A password containing a NUL byte: refused when it is being SET (422), a plain wrong password when it is TRIED.

bcrypt cannot hash a NUL byte (it raises ValueError). Registration, the invitation form and the new password of
a password change therefore answer 422 «Пароль содержит недопустимый символ»; the sign-in of such a password is
the standard 401 (see test_login_throttle.py). No database.
"""
import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import src.api.internal_router  # noqa: E402,F401  (import order: avoids a pre-existing router cycle)
from src.auth import router as auth_router_module  # noqa: E402
from src.auth.dependencies import get_current_active_user_payload  # noqa: E402
from src.auth.schemas import (  # noqa: E402
    PASSWORD_NUL_MESSAGE,
    AcceptInviteRequest,
    ChangePasswordRequest,
    LoginRequest,
    OrgApplicationRequest,
)
from src.auth.service import AuthService  # noqa: E402
from src.core.database import get_db  # noqa: E402

API = "/api/v1/auth"
MESSAGE = "Пароль содержит недопустимый символ"
NUL_PASSWORD = "good\x00password"

APPLY = {"org_name": "Школа", "email": "o@x.kz", "first_name": "А", "last_name": "Б"}
INVITE = {"code": "ABCD1234", "email": "n@x.kz", "first_name": "А", "last_name": "Б"}


@pytest.fixture
def client(monkeypatch):
    refused = ValueError("reached the service")
    monkeypatch.setattr(AuthService, "apply_organization", lambda self, request: (_ for _ in ()).throw(refused))
    monkeypatch.setattr(AuthService, "accept_invitation", lambda self, request: (_ for _ in ()).throw(refused))
    monkeypatch.setattr(AuthService, "change_password", lambda self, uid, old, new: True)
    from src.core.config import get_settings

    monkeypatch.setattr(get_settings(), "self_registration_closed", False)  # the old route is closed by default (console pack)
    app = FastAPI()
    app.include_router(auth_router_module.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: MagicMock()
    app.dependency_overrides[get_current_active_user_payload] = lambda: {"sub": "user-1"}
    return TestClient(app)


def assert_nul_refused(r, field):
    assert r.status_code == 422, r.text
    (error,) = r.json()["detail"]
    assert error["msg"] == MESSAGE == PASSWORD_NUL_MESSAGE
    assert error["type"] == "password_nul" and error["loc"][-1] == field


def test_registration_refuses_a_nul_byte(client):
    r = client.post(f"{API}/apply", json={**APPLY, "password": NUL_PASSWORD})
    assert_nul_refused(r, "password")
    assert client.post(f"{API}/apply", json={**APPLY, "password": "good-password"}).status_code == 400  # reached the service


def test_the_invitation_form_refuses_a_nul_byte(client):
    r = client.post(f"{API}/invite/accept", json={**INVITE, "password": NUL_PASSWORD})
    assert_nul_refused(r, "password")
    assert client.post(f"{API}/invite/accept", json={**INVITE, "password": "good-password"}).status_code == 400


def test_change_password_refuses_a_nul_byte_in_the_new_password(client):
    r = client.post(f"{API}/change-password", json={"old_password": "old-password", "new_password": NUL_PASSWORD})
    assert_nul_refused(r, "new_password")
    ok = client.post(f"{API}/change-password", json={"old_password": "old-password", "new_password": "new-password"})
    assert ok.status_code == 200


@pytest.mark.parametrize("position", ["start", "middle", "end"])
def test_a_nul_byte_anywhere_in_the_password_is_refused(position):
    password = {"start": "\x00abcdefg", "middle": "abc\x00defg", "end": "abcdefg\x00"}[position]
    for schema, extra in ((OrgApplicationRequest, APPLY), (AcceptInviteRequest, INVITE)):
        with pytest.raises(ValidationError) as exc:
            schema(**extra, password=password)
        assert MESSAGE in str(exc.value)
    with pytest.raises(ValidationError):
        ChangePasswordRequest(old_password="x", new_password=password)


def test_other_control_characters_and_unicode_are_still_fine():
    assert OrgApplicationRequest(**APPLY, password="пароль\t123 ✓").password == "пароль\t123 ✓"
    assert ChangePasswordRequest(old_password="x", new_password="Σίσυφος-密码").new_password == "Σίσυφος-密码"


def test_a_login_attempt_with_a_nul_byte_is_not_a_validation_error():
    assert LoginRequest(email="a@x.kz", password=NUL_PASSWORD).password == NUL_PASSWORD  # the 401 path answers it

