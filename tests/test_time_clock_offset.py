"""TIME-TESTS (server): the stand clock - an offset in Redis moves the business time forward; outside the test mode nothing is read."""
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from src.auth import business_clock as bc
from src.auth import payment_status as ps
from tests.time_clock_helpers import FakeRedis, no_stand_clock, stand_clock  # noqa: F401  (fixtures)


def _real_business_now():
    return bc.real_utc_now().astimezone(bc.BUSINESS_TIMEZONE)


def test_outside_the_test_mode_the_offset_is_zero_and_redis_is_never_asked(no_stand_clock):
    no_stand_clock.store[bc.OFFSET_KEY] = str(5 * 86400)               # the key is there, the sign is not
    before = bc.real_utc_now()
    for _ in range(50):
        moment = bc.business_now()
        bc.business_today(), bc.utc_now(), ps.business_today()
    assert no_stand_clock.calls == 0
    assert bc.current_offset_seconds() == 0
    assert abs(moment.astimezone(timezone.utc) - before) < timedelta(seconds=5)
    with pytest.raises(bc.TestClockError):                             # and nobody can move it
        bc.set_offset(60)
    assert no_stand_clock.calls == 0


def test_a_forward_offset_moves_today_the_period_and_the_utc_instant(stand_clock):
    real_day = _real_business_now().date()
    bc.set_offset(3 * 86400)
    assert bc.business_today() == real_day + timedelta(days=3)
    assert ps.business_today() == real_day + timedelta(days=3)
    assert abs(bc.utc_now() - (bc.real_utc_now().replace(tzinfo=None) + timedelta(days=3))) < timedelta(seconds=5)
    assert bc.month_window_start(bc.business_today()).day == 1
    bc.set_offset(70 * 86400)                                           # forward again: a month later
    assert bc.business_today() == real_day + timedelta(days=70)
    assert stand_clock.store[bc.OFFSET_KEY] == str(70 * 86400)


def test_the_clock_never_moves_back_and_the_same_offset_is_fine(stand_clock):
    bc.set_offset(86400)
    bc.set_offset(86400)
    with pytest.raises(bc.ClockMovedBackError):
        bc.set_offset(86400 - 1)
    assert bc.current_offset_seconds() == 86400 and stand_clock.store[bc.OFFSET_KEY] == "86400"


def test_a_negative_offset_is_read_when_the_key_holds_one(stand_clock):
    """A negative integer in the key is read as it is (nothing here writes one; the stand only moves forward from zero)."""
    stand_clock.store[bc.OFFSET_KEY] = "-3600"
    assert bc.current_offset_seconds() == -3600
    assert bc.business_now() < _real_business_now() - timedelta(minutes=59)


def test_business_now_target_becomes_the_offset(stand_clock):
    target = (_real_business_now() + timedelta(days=200)).replace(tzinfo=None, hour=10, minute=0, second=0, microsecond=0)
    bc.set_offset(bc.offset_for_business_now(target))
    now = bc.business_now()
    assert abs(now.replace(tzinfo=None) - target) < timedelta(seconds=5)


def test_the_cache_lives_a_fraction_of_a_second(stand_clock, monkeypatch):
    stand_clock.store[bc.OFFSET_KEY] = "100"
    assert bc.current_offset_seconds() == 100
    stand_clock.store[bc.OFFSET_KEY] = "200"                           # changed by the other service
    calls = stand_clock.calls
    assert bc.current_offset_seconds() == 100 and stand_clock.calls == calls     # still cached
    monkeypatch.setattr(bc, "_offset_cache", (100, time.monotonic() - 1))         # expired
    assert bc.current_offset_seconds() == 200


def test_put_updates_the_local_cache_at_once(stand_clock):
    assert bc.current_offset_seconds() == 0
    bc.set_offset(600)
    calls = stand_clock.calls
    assert bc.current_offset_seconds() == 600 and stand_clock.calls == calls      # no wait for the cache to expire


def test_redis_down_in_the_test_mode_is_a_clear_error_not_a_silent_zero(stand_clock):
    stand_clock.down = True
    with pytest.raises(bc.TestClockError, match="Redis недоступен"):
        bc.business_now()
    with pytest.raises(bc.TestClockError, match="Redis недоступен"):
        bc.set_offset(10)


def test_a_missing_redis_address_or_a_junk_value_is_a_clear_error(monkeypatch, stand_clock):
    stand_clock.store[bc.OFFSET_KEY] = "tomorrow"
    with pytest.raises(bc.TestClockError, match="не целое"):
        bc.current_offset_seconds()
    monkeypatch.setattr(bc, "_redis_client", None)
    monkeypatch.delenv("ALGO_TEST_CLOCK_REDIS_URL", raising=False)
    monkeypatch.setattr(bc, "_offset_cache", (0, 0.0))
    with pytest.raises(bc.TestClockError, match="ALGO_TEST_CLOCK_REDIS_URL"):
        bc.current_offset_seconds()


def test_set_clock_of_the_tests_wins_over_the_offset(stand_clock):
    bc.set_offset(10 * 86400)
    frozen = datetime(2026, 10, 21, 14, 0, tzinfo=bc.BUSINESS_TIMEZONE)
    bc.set_clock(lambda: frozen)
    assert bc.business_now() == frozen


def test_parallel_readers_and_writers_do_not_break_the_cache(stand_clock):
    seen, errors = [], []

    def reader():
        try:
            for _ in range(200):
                seen.append(bc.current_offset_seconds())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(6)]
    for t in threads:
        t.start()
    for step in range(1, 30):
        bc.set_offset(step * 60)
    for t in threads:
        t.join()
    assert not errors and bc.current_offset_seconds() == 29 * 60
    assert all(0 <= v <= 29 * 60 for v in seen)
