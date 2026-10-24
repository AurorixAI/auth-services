"""Pack A2 - hygiene: LD-19 (html.escape in the mails, no header injection, no body in a deployed log), LD-30 (superadmin on a tenant route -> explicit
403), LD-09 / LD-10 (dev compose without a default internal token, loopback ports; .env.example names what the service reads).  No database."""
import logging
import os
import re
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import src.api.internal_router  # noqa: E402,F401  (import order: avoids a pre-existing router cycle)
from src.api import main_v2  # noqa: E402
from src.auth.dependencies import RoleChecker, get_current_active_user_payload, require_org_admin, require_superadmin  # noqa: E402
from src.core import email as mail  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
APP = main_v2.app


@pytest.fixture
def http():
    return TestClient(APP, raise_server_exceptions=False)


# ------------------------------------------------------------------------------------------------------ LD-19
EVIL = '<a href="http://evil.example/login">Подтвердите</a><script>alert(1)</script>'


def test_the_admin_mail_escapes_what_the_applicant_typed(monkeypatch):
    sent = []
    monkeypatch.setattr(mail, "ADMIN_NOTIFICATION_EMAIL", "admin@algo.test")
    monkeypatch.setattr(mail, "_send_raw", lambda to, subject, body: sent.append((to, subject, body)) or True)
    assert mail.send_admin_new_application(EVIL, "id-1", 'x"><b>@example.com', EVIL)
    _to, _subject, body = sent[0]
    assert "<a href" not in body and "<script" not in body and "<b>" not in body
    assert "&lt;a href=&quot;http://evil.example/login&quot;&gt;" in body and "&lt;script&gt;" in body


def test_the_activation_mail_escapes_names_and_the_link(monkeypatch):
    sent = []
    monkeypatch.setattr(mail, "_send_raw", lambda to, subject, body: sent.append(body) or True)
    mail.send_activation_email("o@example.com", EVIL, EVIL, 'tok"><img src=x onerror=alert(1)>')
    body = sent[0]
    assert "<script" not in body and "<img" not in body and 'href="http://evil' not in body
    assert "&lt;script&gt;" in body and "tok%22%3E%3Cimg" in body


def test_a_newline_in_the_org_name_cannot_start_a_mail_header(monkeypatch):
    captured = {}

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self): pass
        def login(self, *a): pass
        def sendmail(self, sender, rcpt, message): captured["rcpt"], captured["message"] = rcpt, message

    monkeypatch.setattr(mail, "SMTP_HOST", "smtp.test"); monkeypatch.setattr(mail, "SMTP_USER", "u"); monkeypatch.setattr(mail, "SMTP_PASSWORD", "p")
    monkeypatch.setattr(mail, "ADMIN_NOTIFICATION_EMAIL", "admin@algo.test")
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    assert mail.send_admin_new_application("Центр\r\nBcc: victim@example.com", "id-1", "o@example.com", "Имя")
    headers = captured["message"].split("\n\n", 1)[0]
    assert not re.search(r"(?mi)^Bcc:", headers) and captured["rcpt"] == ["admin@algo.test"]


def test_without_smtp_the_body_is_logged_only_in_development(monkeypatch, caplog):
    monkeypatch.setattr(mail, "SMTP_HOST", "")
    for env, expect_body in (("testing", True), ("development", True), ("staging", False), ("production", False)):
        monkeypatch.setenv("APP_ENV", env)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="src.core.email"):
            mail._send_raw("a@b.test", "Тема", "<p>activate?token=SECRETTOKEN</p>")
        assert ("SECRETTOKEN" in caplog.text) is expect_body, env
        assert "Тема" in caplog.text


# ------------------------------------------------------------------------------------------------------ LD-30
def test_a_superadmin_on_a_tenant_route_gets_an_explicit_403(http):
    superadmin = {"sub": str(uuid.uuid4()), "role": "superadmin", "type": "access"}      # what the live check returns: no org_id, no org_role
    APP.dependency_overrides[get_current_active_user_payload] = lambda: superadmin
    try:
        tenant = http.get("/api/v1/auth/org/import/template")
        assert tenant.status_code == 403 and "Нужна организация" in tenant.json()["detail"]
    finally:
        APP.dependency_overrides.pop(get_current_active_user_payload, None)


def test_a_superadmin_still_passes_the_platform_checkers_and_a_member_the_tenant_ones():
    superadmin = {"sub": "s", "role": "superadmin"}
    assert require_superadmin(superadmin)["role"] == "superadmin"
    assert RoleChecker(["superadmin"], check_org=False)(superadmin)["org_id"] is None
    with pytest.raises(HTTPException) as refused:
        require_org_admin(superadmin)
    assert refused.value.status_code == 403
    admin = {"sub": "a", "role": "admin", "org_id": "o1", "org_role": "admin"}
    assert require_org_admin(admin)["org_id"] == "o1"


# ------------------------------------------------------------------------------------------------------ LD-09 / LD-10
def test_dev_compose_has_no_default_internal_token_and_binds_its_ports_to_loopback():
    yaml = pytest.importorskip("yaml")
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    env = compose["services"]["auth"]["environment"]
    token = next(e for e in env if e.startswith("INTERNAL_SERVICE_TOKEN="))
    assert ":?" in token and "local-internal-service-token" not in token                     # fail-fast when it is not provided
    for name, service in compose["services"].items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (name, port)
    grafana = next(e for e in compose["services"]["grafana"]["environment"] if e.startswith("GF_SECURITY_ADMIN_PASSWORD="))
    assert "GRAFANA_PASSWORD" in grafana


def test_env_example_names_the_variables_the_service_reads():
    dotenv = pytest.importorskip("dotenv")
    values = dotenv.dotenv_values(ROOT / ".env.example")
    assert {"JWT_ACCESS_TTL_MINUTES", "JWT_REFRESH_TTL_DAYS", "JWT_SECRET_KEY", "DIAGNOSTIC_BASE_URL"} <= set(values)
    assert "ACCESS_TOKEN_EXPIRE_MINUTES" not in values and "REFRESH_TOKEN_EXPIRE_DAYS" not in values
    from src.core import startup_guard

    assert startup_guard.secret_problem(values["SECRET_KEY"])                      # the example key is a placeholder: staging/production would refuse it
