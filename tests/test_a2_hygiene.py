"""Pack A2 - hygiene: LD-19 (html.escape in the mails, no header injection, no body in a deployed log), LD-30 (a token without an organisation on a tenant route -> explicit
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
from src.auth.dependencies import RoleChecker, get_current_active_user_payload, require_org_admin  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
APP = main_v2.app


@pytest.fixture
def http():
    return TestClient(APP, raise_server_exceptions=False)


# ------------------------------------------------------------------------------------------------------ LD-19
# K2-1: the old mail module (src/core/email.py: admin / activation letters) is gone; the escaping, the header-injection guard and the "no body in a
# deployed log" rules of the letters that remain are tested in tests/test_console_mail.py (src/mail).


# ------------------------------------------------------------------------------------------------------ LD-30
def test_a_token_without_an_organisation_on_a_tenant_route_gets_a_403(http):
    loner = {"sub": str(uuid.uuid4()), "role": "teacher", "type": "access"}      # a payload without org_id and org_role (K2-2: there is no platform role in a cabinet)
    APP.dependency_overrides[get_current_active_user_payload] = lambda: loner
    try:
        tenant = http.get("/api/v1/auth/org/import/template")
        assert tenant.status_code == 403
    finally:
        APP.dependency_overrides.pop(get_current_active_user_payload, None)


def test_a_member_passes_the_tenant_checkers_and_an_account_without_a_place_does_not():
    loner = {"sub": "s", "role": "teacher"}
    with pytest.raises(HTTPException) as refused:
        require_org_admin(loner)
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
