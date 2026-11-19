"""TIME-TESTS (server): the switch of the stand clock is guarded twice - APP_ENV=development AND ALGO_TEST_CLOCK=1 - and the service refuses to start with the variable anywhere else.

``TEST_CLOCK_MODE`` is decided at import, so every case here runs the real import in a child process (like tests/test_a2_startup_guard.py).
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.core import startup_guard as guard

ROOT = Path(__file__).resolve().parents[1]
GOOD = "9f2c7ab41de05b68" * 4
CLEAN = ("APP_ENV", "ALGO_TEST_CLOCK", "ALGO_TEST_CLOCK_REDIS_URL", "SECRET_KEY", "JWT_SECRET_KEY")


def child(code, **env):
    base = {k: v for k, v in os.environ.items() if k not in CLEAN}
    base.update({k: v for k, v in env.items() if v is not None})
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=base, capture_output=True, text=True, timeout=180)


MODE = "import src.auth.business_clock as bc; print('MODE', bc.TEST_CLOCK_MODE, bc.current_offset_seconds())"
APP = ("from fastapi.testclient import TestClient\nfrom src.api.main_v2 import app\n"
       "print('CLOCK_GET', TestClient(app).get('/api/v1/auth/dev/clock').status_code); print('DOCS', app.docs_url)")


@pytest.mark.parametrize("app_env", ["staging", "production", "preview", "testing", "Staging", ""])
@pytest.mark.parametrize("value", ["1", "0", "true", "x"])
def test_any_non_empty_variable_outside_development_stops_the_start_and_names_both_variables(app_env, value):
    r = child(MODE, APP_ENV=app_env, ALGO_TEST_CLOCK=value, SECRET_KEY=GOOD)
    assert r.returncode != 0
    assert "ALGO_TEST_CLOCK" in r.stderr and "ALGO_TEST_CLOCK_REDIS_URL" in r.stderr and "APP_ENV=development" in r.stderr


def test_an_empty_or_missing_variable_changes_nothing_on_a_server():
    for value in (None, "", "  "):
        r = child(MODE, APP_ENV="staging", ALGO_TEST_CLOCK=value, SECRET_KEY=GOOD)
        assert r.returncode == 0, r.stderr[-400:]
        assert "MODE False 0" in r.stdout


def test_development_alone_does_not_switch_the_clock_on():
    for value in (None, "0", ""):
        r = child(MODE, APP_ENV="development", ALGO_TEST_CLOCK=value)
        assert r.returncode == 0, r.stderr[-400:]
        assert "MODE False 0" in r.stdout


def test_development_with_the_variable_switches_it_on_and_a_dead_redis_is_a_loud_error():
    r = child("import src.auth.business_clock as bc\nprint('MODE', bc.TEST_CLOCK_MODE)\n"
              "try:\n    bc.business_now()\nexcept bc.TestClockError as e:\n    print('ERR', e)",
              APP_ENV=" Development ", ALGO_TEST_CLOCK="1", ALGO_TEST_CLOCK_REDIS_URL="redis://127.0.0.1:1/0")
    assert r.returncode == 0, r.stderr[-400:]
    assert "MODE True" in r.stdout and "ERR управляемые часы: Redis недоступен" in r.stdout


def test_development_with_the_variable_and_no_redis_address_is_a_loud_error():
    r = child("import src.auth.business_clock as bc\ntry:\n    bc.business_now()\nexcept bc.TestClockError as e:\n    print('ERR', e)", APP_ENV="development", ALGO_TEST_CLOCK="1")
    assert "ERR" in r.stdout and "ALGO_TEST_CLOCK_REDIS_URL" in r.stdout


def test_the_app_of_a_server_has_no_dev_clock_route_and_no_docs_and_the_dev_stand_has_it():
    server = child(APP, APP_ENV="staging", SECRET_KEY=GOOD)
    assert server.returncode == 0, server.stderr[-400:]
    assert "CLOCK_GET 404" in server.stdout and "DOCS None" in server.stdout
    stand = child(APP, APP_ENV="development", ALGO_TEST_CLOCK="1", ALGO_TEST_CLOCK_REDIS_URL="redis://127.0.0.1:1/0")
    assert stand.returncode == 0, stand.stderr[-400:]
    assert "CLOCK_GET 503" in stand.stdout          # the route exists and says "Redis is not reachable" (the address above is a dead port)


def test_a_development_machine_without_the_variable_has_the_route_but_it_answers_404():
    """The dev router is mounted in development (as before); the clock route then still checks the second sign and answers 404."""
    r = child("from fastapi.testclient import TestClient\nfrom src.api.main_v2 import app\nc = TestClient(app)\n"
              "print('CODES', c.get('/api/v1/auth/dev/clock').status_code, c.put('/api/v1/auth/dev/clock', json={'offset_seconds': 5}).status_code)", APP_ENV="development")
    assert r.returncode == 0, r.stderr[-400:]
    assert "CODES 404 404" in r.stdout


def test_health_says_whether_the_clock_is_on():
    code = ("from fastapi.testclient import TestClient\nfrom src.api.main_v2 import app\nprint('HEALTH', TestClient(app).get('/health').json().get('test_clock'))")
    assert "HEALTH False" in child(code, APP_ENV="staging", SECRET_KEY=GOOD).stdout
    assert "HEALTH True" in child(code, APP_ENV="development", ALGO_TEST_CLOCK="1", ALGO_TEST_CLOCK_REDIS_URL="redis://127.0.0.1:1/0").stdout


def test_one_definition_of_the_development_environment():
    for raw, expected in (("development", True), (" Development ", True), ("DEVELOPMENT", True), ("dev", False), ("staging", False), ("", False), ("production", False)):
        assert guard.is_dev_env({"APP_ENV": raw}) is expected
    assert guard.is_dev_env({}) is False


def test_the_compose_files_of_this_repository_never_carry_the_test_clock():
    """The compose files of stage / prod live in the infrastructure repository (checked by its CI, part П3); here: none of the compose files of the service sets the switch."""
    files = sorted(ROOT.glob("docker-compose*.yml")) + sorted(ROOT.glob("docker-compose*.yaml"))
    assert files
    for path in files:
        assert "ALGO_TEST_CLOCK" not in path.read_text(encoding="utf-8"), path.name
