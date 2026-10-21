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
