"""Test isolation for the counters behind the rate limits and the login throttle.

No test may reach a real Redis (a developer machine may run one on localhost:6379, and its counters would
leak from run to run), and no in-process counter may leak from one test into the next. A test that wants
Redis behaviour installs its own client by monkeypatching ``src.core.redis_client.get_redis``.
"""
import pytest


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
