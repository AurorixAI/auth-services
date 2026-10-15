"""
ALGO V1 — Rate Limiting Middleware

Redis-backed rate limiter with in-memory fallback.
Reuses the lazy Redis client from redis_sessions.py.

Usage in router:
    from src.api.rate_limit import rate_limit

    @router.post("/login")
    def login(request: Request, _rl=Depends(rate_limit("5/minute")), ...):
        ...
"""

from __future__ import annotations

import ipaddress
import logging
import time
from collections import defaultdict
from typing import Tuple

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory fallback store  {key: [(timestamp, ...),]}
# ---------------------------------------------------------------------------
_memory_store: dict[str, list[float]] = defaultdict(list)


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


def _client_key(request: Request, endpoint: str) -> str:
    """Build a unique key from IP + endpoint."""
    return f"rl:{endpoint}:{client_ip(request)}"


# ---------------------------------------------------------------------------
# Core check functions
# ---------------------------------------------------------------------------


def _check_redis(key: str, max_requests: int, window: int) -> bool:
    """Return True if request is allowed. Uses Redis INCR + EXPIRE.

    EXPIRE is only set on the first increment of a window — setting it on
    every call would keep pushing the TTL out on each request (including
    rejected ones), so a client that keeps retrying within the window would
    never actually get un-blocked.
    """
    try:
        from src.api.redis_sessions import _get_redis

        r = _get_redis()
        if r is None:
            return _check_memory(key, max_requests, window)

        current = r.incr(key)
        if current == 1:
            r.expire(key, window)
        return current <= max_requests
    except Exception as e:
        logger.warning("Rate limit Redis error, falling back to memory: %s", e)
        return _check_memory(key, max_requests, window)


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
