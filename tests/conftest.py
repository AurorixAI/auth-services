"""Test isolation for the counters behind the rate limits and the login throttle.

No test may reach a real Redis (a developer machine may run one on localhost:6379, and its counters would
leak from run to run), and no in-process counter may leak from one test into the next. A test that wants
Redis behaviour installs its own client by monkeypatching ``src.core.redis_client.get_redis``.
"""
import os
import re

import pytest

os.environ.setdefault("PAYMENTS_SUMMARY_TTL", "0")  # no summary cache in tests (a test that wants it sets it)


def _own_database_per_xdist_worker():
    """``pytest -n N`` (scripts/test_fast.sh): worker ``gwK`` gets its own copy of every test database, ``<name>_gwK``, created on first use.

    Runs while pytest loads this file - before any test module reads the URLs. CI and a plain run have no ``PYTEST_XDIST_WORKER`` and are not
    touched. Whole files go to one worker (``--dist loadfile``), so the databases the migration tests create by a fixed name never meet."""
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not worker:
        return
    import psycopg2
    from urllib.parse import urlsplit, urlunsplit

    for var in ("A2_AUTH_TEST_DATABASE_URL", "A5_AUTH_MIGRATION_TEST_DATABASE_URL", "DATABASE_URL"):
        url = os.environ.get(var)
        if not url or not re.search(r"/(a2_auth|a5_auth_migrations)$", url.split("?")[0]):
            continue                                  # an unusual URL: leave it, the guards of the tests refuse it
        parts = urlsplit(url)
        name = f"{parts.path.lstrip('/')}_{worker}"
        maintenance = urlunsplit(parts._replace(scheme="postgresql", path="/postgres"))
        conn = psycopg2.connect(maintenance)
        conn.autocommit = True
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
                if cursor.fetchone() is None:
                    cursor.execute(f'CREATE DATABASE "{name}"')
        finally:
            conn.close()
        os.environ[var] = urlunsplit(parts._replace(path=f"/{name}"))


_own_database_per_xdist_worker()


def pytest_collection_modifyitems(config, items):
    """Under pytest-xdist only: the longest test files first (measured seconds of ``.github/ci_test_durations.json``), so that a 270-second file does not start
    last and leave three workers waiting for it. Every worker sorts the same way (stable: the order inside a file and between equal weights is kept)."""
    if not os.environ.get("PYTEST_XDIST_WORKER"):
        return
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    try:
        known = json.loads((root / ".github" / "ci_test_durations.json").read_text())
    except (OSError, ValueError):
        return
    default = sorted(known.values())[len(known) // 2] if known else 0.0

    def weight(item):
        return known.get(Path(str(item.fspath)).resolve().relative_to(root).as_posix(), default)

    items.sort(key=lambda item: -weight(item))


@pytest.fixture(autouse=True)
def _no_soft_duplicate_guard(monkeypatch):
    """Tests enter equal payments back to back; the one-minute double-click guard has its own test."""
    try:
        from src.auth import payments_service
    except Exception:  # pragma: no cover - a unit test without the application stack
        yield
        return
    monkeypatch.setattr(payments_service, "SOFT_DUPLICATE_SECONDS", 0)
    yield


@pytest.fixture(autouse=True)
def _isolated_counters(monkeypatch):
    from src.api import rate_limit
    from src.auth import login_throttle
    from src.core import redis_client

    def clear():
        rate_limit._memory_store.clear()
        rate_limit._window_store.clear()
        login_throttle._memory.clear()
        login_throttle._known.clear()

    redis_client.reset_for_tests()
    monkeypatch.setattr(redis_client, "get_redis", lambda: None)
    clear()
    yield
    clear()
    redis_client.reset_for_tests()


@pytest.fixture(autouse=True)
def _legacy_slots_valid_from_the_beginning(request, monkeypatch):
    """LOGIC-B O1: a slot WITHOUT ``validFrom`` follows the day its group was created.  The fixtures of the older tests build groups
    "now" with slots that already had lessons in the past; for them the slot has always been valid.  A test that is about the legacy
    rule itself (``@pytest.mark.legacy_valid_from``) keeps the real one."""
    if request.node.get_closest_marker("legacy_valid_from"):
        yield
        return
    try:
        from src.auth import schedule_rules
    except Exception:  # pragma: no cover - a unit test without the application stack
        yield
        return
    monkeypatch.setattr(schedule_rules, "legacy_valid_from", lambda group: "2000-01-01")
    yield


def pytest_configure(config):
    config.addinivalue_line("markers", "legacy_valid_from: keep the real fallback of a slot without validFrom")
    config.addinivalue_line("markers", "real_bcrypt: the test is about the production cost of the password hash (12), keep it")


@pytest.fixture(autouse=True)
def _cheap_password_hashes(request, monkeypatch):
    """Two thirds of a database test is bcrypt: every sign-in hashes or verifies at cost 12 (0.3 s each). A test is about the behaviour, not about the
    cost, so passwords are hashed at cost 4 (the minimum) here; the production contexts are untouched, and a test that is about the production cost
    carries ``@pytest.mark.real_bcrypt``."""
    if request.node.get_closest_marker("real_bcrypt"):
        yield
        return
    try:
        from passlib.context import CryptContext

        from src.auth import service
        from src.core import security
    except Exception:  # pragma: no cover - a unit test without the application stack
        yield
        return
    cheap = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=4)
    monkeypatch.setattr(security, "_pwd_context", cheap)
    monkeypatch.setattr(service, "pwd_context", cheap)
    yield


@pytest.fixture(autouse=True)
def _exam_service_has_no_exams(monkeypatch):
    """LOGIC-B O4 / O8: ending a lesson and archiving a group ask exam-service.  No test may reach the network: by default exam-service
    answers "no exams here"; a test of the exam behaviour replaces ``exam_bridge._post`` (``tests/ops_helpers.ExamStub``)."""
    try:
        from src.auth import exam_bridge
    except Exception:  # pragma: no cover - a unit test without the application stack
        yield
        return

    def empty(path, payload):
        if path.endswith("/completion-check") or path.endswith("/archive-impact"):
            return {"ok": True, "exams": []}
        if path.endswith("/completed"):
            return {"ok": True, "released": [], "leftRunning": []}
        return {"ok": True, "releasedExamIds": []}

    monkeypatch.setattr(exam_bridge, "_post", empty)
    # W1 S8: a change of the day / time of a weekly lesson and the stop of a series ask exam-service for the exams of the future dates (``AuthService._fetch_exam_recurring_end_impact``,
    # plain httpx): by default "no exams"; a test of the exam behaviour replaces the two methods (``tests/test_w1_series_pg.py``)
    from src.auth.service import AuthService

    monkeypatch.setattr(AuthService, "_fetch_exam_recurring_end_impact", lambda self, **kw: {"can_end": True, "affected_exam_count": 0, "releasable_exam_count": 0,
                                                                                            "blocked_exam_count": 0, "exams": []})
    monkeypatch.setattr(AuthService, "_release_exam_recurring_end", lambda self, **kw: [])
    yield
