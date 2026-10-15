"""
ALGO V1 — Rate Limiting Middleware

Redis-backed rate limiter with in-memory fallback.
The Redis client is src/core/redis_client.py (REDIS_URL); while Redis is unreachable every limit runs on
per-process memory instead (fail open: availability first, the limits just stop being shared).

Usage in router:
    from src.api.rate_limit import rate_limit

    @router.post("/login")
    def login(request: Request, _rl=Depends(rate_limit("5/minute")), ...):
        ...
"""

from __future__ import annotations

import ipaddress
import logging
import math
import threading
import time
from collections import defaultdict
from typing import Optional, Tuple

from fastapi import HTTPException, Request

from src.core import redis_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory fallback store  {key: [(timestamp, ...),]}
# ---------------------------------------------------------------------------
_memory_store: dict[str, list[float]] = defaultdict(list)

# Fixed-window counters for hit_window(): {key: (count, window_ends_at)}.
_window_store: dict[str, Tuple[int, float]] = {}
_window_lock = threading.Lock()
_WINDOW_STORE_SOFT_LIMIT = 20_000


def _parse_rate(rate_string: str) -> Tuple[int, int]:
    """Parse '5/minute' → (5, 60), '3/hour' → (3, 3600)."""
    count_s, period_s = rate_string.split("/")
    count = int(count_s)
    period_map = {
        "second": 1,
        "sec": 1,
        "s": 1,
        "minute": 60,
        "min": 60,
        "m": 60,
        "hour": 3600,
        "hr": 3600,
        "h": 3600,
        "day": 86400,
        "d": 86400,
    }
    window = period_map.get(period_s.strip().lower())
    if window is None:
        raise ValueError(f"Unknown rate period: {period_s}")
    return count, window


def _valid_ip(value: str | None) -> str | None:
    """*value* as a normalised IP address, or None (a header is client input: never trust its shape)."""
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def client_ip(request: Request) -> str:
    """The caller's address, as far as it can be trusted.

    The service sits behind our nginx, which is directly on the internet and sets
    X-Real-IP $remote_addr in every proxied location (it overwrites whatever the client sent) and
    APPENDS the real peer to X-Forwarded-For (the client controls everything before it). So:
    X-Real-IP, else the RIGHTMOST X-Forwarded-For element (the one our proxy added), else the
    socket peer. The LEFTMOST element is whatever the client typed: using it let anybody pick a fresh
    rate-limit bucket per request. A value that is not an IP address is ignored.
    """
    real_ip = _valid_ip(request.headers.get("x-real-ip"))
    if real_ip:
        return real_ip
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        last = _valid_ip(forwarded.split(",")[-1])
        if last:
            return last
    return request.client.host if request.client else "unknown"


IPV6_BUCKET_PREFIX_LENGTH = 64


def limit_bucket(address: str) -> str:
    """The rate-limit bucket of a client address: an IPv4 address as it is, an IPv6 address by its /64 prefix.

    A single IPv6 subscriber is handed a whole /64 (2**64 addresses) and can pick a new source address for
    every request: counting the full address gives such a client an unlimited number of buckets. An IPv4
    address that arrives IPv6-mapped (``::ffff:203.0.113.7``, how a dual-stack socket shows it) is the IPv4
    client and keeps its own bucket - otherwise every IPv4 client would share the single bucket ``::/64``.
    Anything that is not an address (``unknown``) is returned unchanged.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if ip.version == 4:
        return str(ip)
    if ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ipaddress.ip_network(f"{ip}/{IPV6_BUCKET_PREFIX_LENGTH}", strict=False))


def client_bucket(request: Request) -> str:
    """What every limiter counts for this caller: the trusted client address (``client_ip``) as a bucket."""
    return limit_bucket(client_ip(request))


def _client_key(request: Request, endpoint: str) -> str:
    """Build a unique key from client bucket + endpoint."""
    return f"rl:{endpoint}:{client_bucket(request)}"


# ---------------------------------------------------------------------------
# Core check functions
# ---------------------------------------------------------------------------


# INCR + EXPIRE as ONE atomic step. EXPIRE is set when the window opens (count == 1) and, defensively,
# whenever a key somehow has no TTL (a crash between the two calls of the old non-atomic version left a
# counter that never expired: a permanent block). Later hits do NOT push the TTL out, so a client that
# keeps retrying inside the window still gets un-blocked when the window ends.
_WINDOW_HIT_LUA = """
local c = redis.call('INCR', KEYS[1])
local ttl = redis.call('TTL', KEYS[1])
if c == 1 or ttl < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
  ttl = tonumber(ARGV[1])
end
return {c, ttl}
"""


def _redis_hit(key: str, window: int) -> Optional[Tuple[int, int]]:
    """(count, seconds until the window ends) from Redis, or None when Redis is unavailable."""
    r = redis_client.get_redis()
    if r is None:
        return None
    try:
        count, ttl = r.eval(_WINDOW_HIT_LUA, 1, key, window)
        return int(count), max(int(ttl), 1)
    except Exception as e:  # noqa: BLE001 - a counter store must never break a request
        redis_client.mark_down(e)
        return None


def _check_redis(key: str, max_requests: int, window: int) -> bool:
    """Return True if request is allowed. Redis (shared between workers) first, process memory otherwise."""
    hit = _redis_hit(key, window)
    if hit is None:
        return _check_memory(key, max_requests, window)
    return hit[0] <= max_requests


def _monotonic() -> float:
    return time.monotonic()


def hit_window(key: str, window: int) -> Tuple[int, int]:
    """Count one hit in a fixed window of *window* seconds: (hits so far including this one, seconds left).

    Redis when available, a per-process fixed window otherwise. Unlike ``rate_limit`` the caller gets the
    time left, which is what a ``Retry-After`` header needs.
    """
    hit = _redis_hit(key, window)
    if hit is not None:
        return hit
    now = _monotonic()
    with _window_lock:
        count, ends_at = _window_store.get(key, (0, 0.0))
        if ends_at <= now:
            count, ends_at = 0, now + window
        count += 1
        _window_store[key] = (count, ends_at)
        if len(_window_store) > _WINDOW_STORE_SOFT_LIMIT:  # keep unknown-key floods from growing memory
            for stale in [k for k, (_, end) in _window_store.items() if end <= now]:
                del _window_store[stale]
    return count, max(math.ceil(ends_at - now), 1)


def _check_memory(key: str, max_requests: int, window: int) -> bool:
    """In-memory sliding window fallback."""
    now = time.time()
    cutoff = now - window
    # Prune old entries
    _memory_store[key] = [t for t in _memory_store[key] if t > cutoff]
    if len(_memory_store[key]) >= max_requests:
        return False
    _memory_store[key].append(now)
    return True


# ---------------------------------------------------------------------------
# FastAPI Dependency
# ---------------------------------------------------------------------------


def rate_limit(rate_string: str, endpoint: str | None = None):
    """
    Returns a FastAPI dependency that enforces rate limiting.

    Args:
        rate_string: e.g. "5/minute", "3/hour", "10/second"
        endpoint: optional override for the key (default: request path)

    Example:
        @router.post("/login")
        def login(_rl=Depends(rate_limit("5/minute")), ...):
    """
    max_requests, window = _parse_rate(rate_string)

    def _dependency(request: Request):
        ep = endpoint or request.url.path
        key = _client_key(request, ep)
        if not _check_redis(key, max_requests, window):
            logger.warning("Rate limit exceeded: %s", key)
            raise HTTPException(
                status_code=429,
                detail="Слишком много запросов. Попробуйте позже.",
            )

    return _dependency


def enforce_rate_limit(subject: str, rate_string: str, endpoint: str) -> None:
    """Count one request of *subject* (anything but a client address, e.g. the hash of a token) and raise 429 over the limit.

    For limits that are not about the caller's address: the key is built from *subject*, never from
    request headers.
    """
    max_requests, window = _parse_rate(rate_string)
    key = f"rl:{endpoint}:{subject}"
    if not _check_redis(key, max_requests, window):
        logger.warning("Rate limit exceeded: %s", key)
        raise HTTPException(
            status_code=429,
            detail="Слишком много запросов. Попробуйте позже.",
        )
