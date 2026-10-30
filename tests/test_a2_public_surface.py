"""Pack A2 - the public surface of the auth service: what an anonymous caller (or an unlucky one) is told and how often they may ask.

Matrix: LD-18/DM-050 (500 in three different handlers -> fixed text + request id, details in the log only; /health without the DB error),
LD-22 (X-Request-ID only [A-Za-z0-9-]{1,64}), LD-01 (apply x6 from one address -> 6th 429; one e-mail from many addresses -> 4th 429),
SV-26 (IntegrityError of a double registration -> 409), Everything goes through the real app of
``src.api.main_v2`` (its handlers and middleware); no database is needed.
"""
import logging
import os
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

from fastapi import Depends, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.exc import IntegrityError, ProgrammingError  # noqa: E402

import src.api.internal_router  # noqa: E402,F401  (import order: avoids a pre-existing router cycle)
from src.api import main_v2  # noqa: E402
from src.auth.dependencies import RoleChecker, get_current_active_user_payload, require_org_admin, require_superadmin  # noqa: E402
from src.auth.service import AuthService  # noqa: E402
from src.core.database import get_db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
APP = main_v2.app
SECRET_TEXT = "password=hunter2 host=db.internal.example SELECT secret_column FROM users"


@pytest.fixture
def http(monkeypatch):
    # These tests are about the old self-registration route (throttle, 409, 500 text): it is closed by default since the console pack
    # (410, tests/test_console_registration_closed.py), so they reopen it with the setting.
    from src.core.config import get_settings

    monkeypatch.setattr(get_settings(), "self_registration_closed", False)
    return TestClient(APP, raise_server_exceptions=False)


@pytest.fixture
def boom_routes():
    """Three handlers of different kinds that fail with an unexpected exception carrying internals."""

    def sync_boom():
        raise RuntimeError(SECRET_TEXT)

    async def async_boom():
        raise ProgrammingError("SELECT secret_column FROM users WHERE token = %(t)s", {"t": "hunter2"}, Exception("host=db.internal.example"))

    def dep_boom():
        raise KeyError(SECRET_TEXT)

    def via_dependency(_x=Depends(dep_boom)):
        return {"ok": True}

    before = list(APP.router.routes)
    APP.add_api_route("/__a2/sync", sync_boom)
    APP.add_api_route("/__a2/async", async_boom)
    APP.add_api_route("/__a2/dep", via_dependency)
    yield ["/__a2/sync", "/__a2/async", "/__a2/dep"]
    APP.router.routes[:] = before


# ------------------------------------------------------------------------------------------------------ LD-18 / DM-050
def test_500_in_three_handlers_says_nothing_but_the_request_id(http, boom_routes, caplog):
    for path in boom_routes:
        with caplog.at_level(logging.ERROR):
            r = http.get(path)
        assert r.status_code == 500, path
        body = r.json()
        assert set(body) == {"detail", "request_id"} and body["detail"] == "Внутренняя ошибка сервера", path
        assert body["request_id"] == r.headers["X-Request-ID"] and uuid.UUID(body["request_id"])
        for forbidden in ("hunter2", "db.internal", "secret_column", "RuntimeError", "KeyError", "Traceback", "SELECT"):
            assert forbidden not in r.text, (path, forbidden)
    # the details exist - in the log, next to the same request id
    assert "hunter2" in caplog.text or "db.internal" in caplog.text
    assert body["request_id"] in caplog.text


def test_500_keeps_the_clients_well_formed_request_id_and_never_adds_a_traceback_even_in_debug(http, boom_routes, monkeypatch):
    monkeypatch.setattr(main_v2.settings, "DEBUG", True)
    r = http.get("/__a2/sync", headers={"X-Request-ID": "trace-me-123"})
    assert r.status_code == 500 and r.json() == {"detail": "Внутренняя ошибка сервера", "request_id": "trace-me-123"}
    assert "traceback" not in r.json() and r.headers["X-Request-ID"] == "trace-me-123"


def test_500_of_a_real_route_is_generic_too(http, monkeypatch):
    def broken(self, *a, **k):
        raise RuntimeError(SECRET_TEXT)

    monkeypatch.setattr(AuthService, "apply_organization", broken)
    body = {"org_name": "Центр", "email": "o@example.com", "password": "secret1", "first_name": "А", "last_name": "Б"}
    r = http.post("/api/v1/auth/apply", json=body)
    assert r.status_code == 500 and r.json()["detail"] == "Внутренняя ошибка сервера" and "hunter2" not in r.text


def test_health_does_not_print_the_database_error(http):
    class DeadSession:
        def execute(self, *a, **k):
            raise RuntimeError("connection to server at db.internal.example (10.0.0.5) failed: password authentication failed for user algo")

    APP.dependency_overrides[get_db] = lambda: DeadSession()
    try:
        r = http.get("/health")
    finally:
        APP.dependency_overrides.pop(get_db, None)
    assert r.status_code == 200 and r.json()["status"] == "degraded" and r.json()["database"] == "error"
    assert "db.internal" not in r.text and "10.0.0.5" not in r.text and "password" not in r.text


# ------------------------------------------------------------------------------------------------------ LD-22
@pytest.mark.parametrize("bad", ["x" * 65, "with space", "semi;colon", "a/b", "drop table;--"])
def test_a_malformed_request_id_is_replaced(http, bad):
    r = http.get("/health/live", headers={"X-Request-ID": bad})
    got = r.headers["X-Request-ID"]
    assert got != bad and uuid.UUID(got)


@pytest.mark.parametrize("good", ["abc-123", "A" * 64, "7f0c1e2a-9b1d-4c33-8a55-0d6f3b2a1c9e", "1"])
def test_a_well_formed_request_id_is_kept(http, good):
    assert http.get("/health/live", headers={"X-Request-ID": good}).headers["X-Request-ID"] == good


def test_a_request_id_with_a_newline_is_refused_by_the_filter_not_logged():
    from src.core.middleware import safe_request_id

    assert uuid.UUID(safe_request_id("abc\nINJECTED"))
    assert uuid.UUID(safe_request_id("")) and uuid.UUID(safe_request_id(None))
    assert safe_request_id("ok-1") == "ok-1"


# ------------------------------------------------------------------------------------------------------ LD-01 / SV-26
APPLY = "/api/v1/auth/apply"


def apply_body(email):
    return {"org_name": "Центр", "email": email, "password": "secret1", "first_name": "А", "last_name": "Б"}


@pytest.fixture
def taken_email(monkeypatch):
    """apply_organization answers 'e-mail taken' (400) without a database: only the throttle is under test."""
    calls = []

    def fake(self, request):
        calls.append(request.email)
        raise ValueError("Пользователь с таким email уже существует")

    monkeypatch.setattr(AuthService, "apply_organization", fake)
    return calls


def test_apply_x6_from_one_address_the_sixth_is_429(http, taken_email):
    codes = [http.post(APPLY, json=apply_body(f"u{i}@example.com"), headers={"X-Real-IP": "203.0.113.7"}) for i in range(6)]
    assert [r.status_code for r in codes[:5]] == [400] * 5
    last = codes[5]
    assert last.status_code == 429 and "Слишком много заявок" in last.json()["detail"] and int(last.headers["Retry-After"]) > 0
    assert len(taken_email) == 5                                               # the sixth never reached the service
    other = http.post(APPLY, json=apply_body("v@example.com"), headers={"X-Real-IP": "203.0.113.8"})
    assert other.status_code == 400                                            # another address has its own budget


def test_apply_one_email_from_many_addresses_the_fourth_is_429(http, taken_email):
    codes = [http.post(APPLY, json=apply_body("same@example.com"), headers={"X-Real-IP": f"198.51.100.{i}"}).status_code for i in range(1, 5)]
    assert codes == [400, 400, 400, 429]
    assert http.post(APPLY, json=apply_body("SAME@Example.com"), headers={"X-Real-IP": "198.51.100.77"}).status_code == 429   # the case does not buy a new budget


def test_apply_double_registration_race_is_409_not_500(http, monkeypatch):
    def lost_the_race(self, request):
        raise IntegrityError("INSERT INTO users ...", {}, Exception("duplicate key value violates unique constraint ix_users_email"))

    rollbacks = []
    monkeypatch.setattr(AuthService, "apply_organization", lost_the_race)
    fake_db = MagicMock()
    fake_db.rollback.side_effect = lambda: rollbacks.append(1)
    APP.dependency_overrides[get_db] = lambda: fake_db
    try:
        r = http.post(APPLY, json=apply_body("race@example.com"))
    finally:
        APP.dependency_overrides.pop(get_db, None)
    assert r.status_code == 409 and r.json()["detail"] == "Пользователь с таким email уже существует" and rollbacks
    assert "duplicate key" not in r.text
