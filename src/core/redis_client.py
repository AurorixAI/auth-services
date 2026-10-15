"""The one lazily created Redis client for short-lived counters (rate limits, login throttle).

The compose files give the service its own Redis (``REDIS_URL``); ``redis`` is a declared dependency.
A counter store must never take the service down with it, so:

* connect/read timeouts are short (a dead Redis costs a request at most ~0.5 s, once);
* after a failure the client is reported as absent (``None``) for ``RETRY_AFTER_FAILURE_SECONDS``, so
  callers run on their per-process in-memory fallback instead of paying the timeout on every request;
* nothing here raises: callers get a client or ``None``.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

RETRY_AFTER_FAILURE_SECONDS = 15.0
_SOCKET_TIMEOUT_SECONDS = 0.5

_lock = threading.Lock()
_client = None
_down_until = 0.0


def _now() -> float:
    return time.monotonic()


def get_redis():
    """The shared client, or None while Redis is unavailable (callers fall back to process memory)."""
    global _client
    if _now() < _down_until:
        return None
    if _client is not None:
        return _client
    with _lock:
        if _client is not None:
            return _client
        try:
            import redis  # imported here: a missing package must degrade to memory, not break startup

            from src.core.config import get_settings

            _client = redis.Redis.from_url(
                get_settings().redis_url,
                socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
                socket_timeout=_SOCKET_TIMEOUT_SECONDS,
                decode_responses=True,
            )
        except Exception as exc:  # noqa: BLE001 - any failure here means "no Redis"
            mark_down(exc)
            return None
    return _client


def mark_down(error: BaseException) -> None:
    """Report that a Redis call failed: callers use memory for the next RETRY_AFTER_FAILURE_SECONDS."""
    global _down_until
    first = _now() >= _down_until
    _down_until = _now() + RETRY_AFTER_FAILURE_SECONDS
    if first:  # one line per outage, not one per request
        logger.warning(
            "Redis unavailable (%s: %s): rate limits and login throttling fall back to per-process memory",
            type(error).__name__,
            error,
        )


def reset_for_tests() -> None:
    """Forget the client and any 'down' mark (tests swap the client or the clock)."""
    global _client, _down_until
    with _lock:
        _client = None
        _down_until = 0.0
