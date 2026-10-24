"""A2 / SV-01 + SV-27: the signing key and the invite-link base are checked at start-up by APP_ENV (the variable the whole project uses).

The strict values are ``production`` and ``staging`` (both servers run ``staging``); development / testing only warn.  The real import of the
service is run in a child process, because the guard fires while ``src.auth.service`` is imported.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.core import startup_guard as guard

ROOT = Path(__file__).resolve().parents[1]
GOOD = "k" * 64


def child(**env):
    """Import the service in a fresh interpreter with exactly this APP_ENV / key environment."""
    base = {k: v for k, v in os.environ.items() if k not in ("APP_ENV", "SECRET_KEY", "JWT_SECRET_KEY", "DIAGNOSTIC_BASE_URL", "REQUIRE_DIAGNOSTIC_BASE_URL")}
    base.update({k: v for k, v in env.items() if v is not None})
    return subprocess.run(
        [sys.executable, "-c", "import src.auth.service as s; print('KEYLEN', len(s.SECRET_KEY), len(s.JWT_SECRET), s.BASE_DIAGNOSTIC_URL)"],
        cwd=ROOT, env=base, capture_output=True, text=True, timeout=120,
    )


@pytest.mark.parametrize("app_env", ["staging", "production"])
@pytest.mark.parametrize("key", ["", "short-key-31-characters-long-xxx"[:31], "algo-v1-dev-secret-key-change-in-prod", "change-me-in-production-must-be-at-least-32-chars"])
def test_deployed_env_refuses_an_unfit_key_with_a_clear_message(app_env, key):
    r = child(APP_ENV=app_env, SECRET_KEY=key)
    assert r.returncode != 0
    assert "CRITICAL: SECRET_KEY" in r.stderr and f"APP_ENV={app_env}" in r.stderr


def test_deployed_env_refuses_a_missing_key_and_an_unfit_jwt_key():
    r = child(APP_ENV="staging")
    assert r.returncode != 0 and "CRITICAL: SECRET_KEY" in r.stderr
    r = child(APP_ENV="staging", SECRET_KEY=GOOD, JWT_SECRET_KEY="too-short")
    assert r.returncode != 0 and "CRITICAL: JWT_SECRET_KEY" in r.stderr


def test_deployed_env_starts_with_a_good_key_and_an_empty_jwt_key_falls_back_to_it():
    """The servers set only SECRET_KEY (64 chars); compose may pass JWT_SECRET_KEY as an empty string - that must not mean an empty HS256 key."""
    r = child(APP_ENV="staging", SECRET_KEY=GOOD, JWT_SECRET_KEY="")
    assert r.returncode == 0, r.stderr[-600:]
    assert "KEYLEN 64 64" in r.stdout


@pytest.mark.parametrize("app_env", ["development", "testing"])
def test_dev_and_test_only_warn(app_env):
    r = child(APP_ENV=app_env)                                   # no key at all
    assert r.returncode == 0, r.stderr[-600:]
    assert "SECRET_KEY: пустой" in r.stderr


def test_the_old_ENV_variable_no_longer_decides():
    r = child(ENV="dev", APP_ENV="production", SECRET_KEY="")
    assert r.returncode != 0


def test_unit_secret_problem_rules():
    assert guard.secret_problem("") and guard.secret_problem("   ") and guard.secret_problem("x" * 31)
    assert guard.secret_problem(guard.DEFAULT_SECRET) and guard.secret_problem("Change-Me-" + "x" * 40)
    assert guard.secret_problem("x" * 32) is None


def test_invite_link_base_sv27():
    """Production/staging without DIAGNOSTIC_BASE_URL logs an error (the infra compose files do not pass it yet); REQUIRE_DIAGNOSTIC_BASE_URL makes it fatal."""
    r = child(APP_ENV="staging", SECRET_KEY=GOOD)
    assert r.returncode == 0 and "DIAGNOSTIC_BASE_URL не задан" in r.stderr and "http://localhost:3000/d" in r.stdout
    r = child(APP_ENV="staging", SECRET_KEY=GOOD, REQUIRE_DIAGNOSTIC_BASE_URL="true")
    assert r.returncode != 0 and "DIAGNOSTIC_BASE_URL" in r.stderr
    r = child(APP_ENV="staging", SECRET_KEY=GOOD, REQUIRE_DIAGNOSTIC_BASE_URL="true", DIAGNOSTIC_BASE_URL="https://algo.example/d")
    assert r.returncode == 0 and "https://algo.example/d" in r.stdout
    r = child(APP_ENV="development")
    assert r.returncode == 0 and "DIAGNOSTIC_BASE_URL не задан" not in r.stderr
