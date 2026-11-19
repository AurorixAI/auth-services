"""TIME-TESTS helpers: a fake Redis for the stand clock and a fixture that switches the test mode on for one test (the mode is decided once, at import,
so a test flips the module attribute and removes the fake at the end)."""
from __future__ import annotations

import pytest

from src.auth import business_clock as bc


class FakeRedis:
    """The two calls ``business_clock`` makes; counts every call (``calls``) and can be "down"."""

    def __init__(self) -> None:
        self.store: dict = {}
        self.calls = 0
        self.down = False

    def get(self, key):
        self.calls += 1
        if self.down:
            raise ConnectionError("redis is down")
        return self.store.get(key)

    def set(self, key, value):
        self.calls += 1
        if self.down:
            raise ConnectionError("redis is down")
        self.store[key] = value


@pytest.fixture
def stand_clock(monkeypatch):
    """The test mode ON with a fake Redis and an empty cache; the real clock of ``business_clock`` (no ``set_clock``)."""
    fake = FakeRedis()
    bc.set_clock(None)
    monkeypatch.setattr(bc, "TEST_CLOCK_MODE", True)
    monkeypatch.setenv("APP_ENV", "development")                    # the second sign (the route checks both)
    monkeypatch.setattr(bc, "_redis_client", fake)
    monkeypatch.setattr(bc, "_offset_cache", (0, 0.0))
    yield fake
    bc.set_clock(None)


@pytest.fixture
def no_stand_clock(monkeypatch):
    """The mode OFF (what stage and prod run) with a fake Redis that must never be asked."""
    fake = FakeRedis()
    monkeypatch.setattr(bc, "TEST_CLOCK_MODE", False)
    monkeypatch.setattr(bc, "_redis_client", fake)
    monkeypatch.setattr(bc, "_offset_cache", (0, 0.0))
    yield fake
    bc.set_clock(None)
