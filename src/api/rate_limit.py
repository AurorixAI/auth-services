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
        "second": 1, "sec": 1, "s": 1,
        "minute": 60, "min": 60, "m": 60,
        "hour": 3600, "hr": 3600, "h": 3600,
        "day": 86400, "d": 86400,
    }
    window = period_map.get(period_s.strip().lower())
    if window is None:
        raise ValueError(f"Unknown rate period: {period_s}")
    return count, window


def _client_key(request: Request, endpoint: str) -> str:
    """Build a unique key from IP + endpoint."""
    forwarded = request.headers.get("x-forwarded-for")
    ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")
    return f"rl:{endpoint}:{ip}"


# ---------------------------------------------------------------------------
# Core check functions
# ---------------------------------------------------------------------------

def _check_redis(key: str, max_requests: int, window: int) -> bool:
    """Return True if request is allowed. Uses Redis INCR + EXPIRE."""
    try:
        from src.api.redis_sessions import _get_redis
        r = _get_redis()
        if r is None:
            return _check_memory(key, max_requests, window)

        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, window)
        results = pipe.execute()
        current = results[0]
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
