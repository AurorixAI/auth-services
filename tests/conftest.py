"""Test isolation for the counters behind the rate limits and the login throttle.

No test may reach a real Redis (a developer machine may run one on localhost:6379, and its counters would
leak from run to run), and no in-process counter may leak from one test into the next. A test that wants
Redis behaviour installs its own client by monkeypatching ``src.core.redis_client.get_redis``.
"""
import os

import pytest

os.environ.setdefault("PAYMENTS_SUMMARY_TTL", "0")  # no summary cache in tests (a test that wants it sets it)


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
    yield
