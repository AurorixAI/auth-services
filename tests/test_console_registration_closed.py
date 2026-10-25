"""Self-registration is closed: ``POST /auth/apply`` is 410 with the Russian text, whatever the body; auto-approve is off by default."""
import os

import pytest
from fastapi.testclient import TestClient

import src.api.internal_router  # noqa: F401  (import order: avoids a pre-existing router cycle)
from src.api import main_v2
from src.auth.service import AuthService
from src.core.config import Settings, get_settings

APPLY = "/api/v1/auth/apply"
TEXT = "Регистрация по приглашению — оставьте заявку на демо"


@pytest.fixture
def http():
    return TestClient(main_v2.app, raise_server_exceptions=False)


@pytest.mark.parametrize("body", [{"org_name": "Центр", "email": "o@example.com", "password": "secret12", "first_name": "А", "last_name": "Б"}, {}, {"email": 1}])
def test_apply_is_410_whatever_the_body(http, monkeypatch, body):
    def must_not_run(self, request):
        raise AssertionError("the service was reached")

    monkeypatch.setattr(AuthService, "apply_organization", must_not_run)
    r = http.post(APPLY, json=body)
    assert r.status_code == 410 and r.json() == {"detail": TEXT, "code": "registration_closed"}


def test_apply_is_410_without_a_body_and_with_a_throttle_budget_untouched(http):
    assert http.post(APPLY).status_code == 410
    for _ in range(10):                                                                # the throttle does not even count: always 410
        assert http.post(APPLY, json={}, headers={"X-Real-IP": "203.0.113.200"}).status_code == 410


def test_the_old_behaviour_comes_back_with_the_setting(http, monkeypatch):
    monkeypatch.setattr(get_settings(), "self_registration_closed", False)
    assert http.post(APPLY, json={}).status_code == 422                                # the normal validation again


def test_the_defaults_are_closed_and_not_auto_approved(monkeypatch):
    for key in ("SELF_REGISTRATION_CLOSED", "AUTO_APPROVE_ORGANIZATIONS"):
        monkeypatch.delenv(key, raising=False)
    fresh = Settings(_env_file=None)
    assert fresh.self_registration_closed is True and fresh.auto_approve_organizations is False
    monkeypatch.setenv("SELF_REGISTRATION_CLOSED", "false")
    assert Settings(_env_file=None).self_registration_closed is False
