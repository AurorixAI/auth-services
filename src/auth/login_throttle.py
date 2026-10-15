"""Brute-force protection for the endpoints that verify a password.

Two independent layers, both answering ``429`` + ``Retry-After`` with a Russian message:

1. **Per client address** (``enforce_login_ip_limits``): 30 attempts/minute and 300/hour for every login
   attempt, counted on the TRUSTED address (``rate_limit.client_ip``: X-Real-IP set by our nginx, else the
   rightmost X-Forwarded-For element, else the socket peer - never the leftmost element a client typed).
   A school behind one NAT logging in at 08:00 stays far below it.

2. **Per account** (``begin``): after ``FAILURE_THRESHOLD`` (5) consecutive failed attempts the account is
   refused for 1 min, then 2, 4, 8 and at most ``BACKOFF_CAP_SECONDS`` (15 min) for every further failure.
   There is NO permanent lock and nothing to unlock by hand: a success clears the counter, and the counter
   is forgotten after ``QUIET_WINDOW_SECONDS`` without attempts. While an account is refused the request
   is answered before any user lookup or password check (no timing difference between a locked real
   account and a locked unknown one, and a refused attempt cannot be used to test a guess). Refused
   attempts do not extend the block.

   The attempt is counted BEFORE the password is checked (one atomic step), so a burst of parallel guesses
   cannot all slip through a "check, then count" gap: at most FAILURE_THRESHOLD of them are ever evaluated.

An unknown identifier is counted exactly like a known one, so the 429 reveals nothing about whether the
account exists. The identifier is normalised (NFKC, case-folded, invisible characters removed) and keyed by
an HMAC under SECRET_KEY: the store never holds an address and cannot be dictionary-attacked offline.

Storage: Redis (``REDIS_URL``; counters are shared between workers/replicas and survive a restart) with
atomic Lua; if Redis is unreachable the same logic runs on per-process memory (fail OPEN for availability,
one warning per outage from ``redis_client``).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request, status

from src.api.rate_limit import client_ip, hit_window
from src.core import redis_client
from src.core.config import get_settings

logger = logging.getLogger(__name__)

FAILURE_THRESHOLD = 5
BACKOFF_BASE_SECONDS = 60
BACKOFF_CAP_SECONDS = 15 * 60
QUIET_WINDOW_SECONDS = 15 * 60

LOGIN_IP_LIMITS = (("minute", 30, 60), ("hour", 300, 3600))  # (label, attempts, window seconds)

SCOPE_LOGIN = "login"
SCOPE_CHANGE_PASSWORD = "change-password"

_MESSAGES = {
    SCOPE_LOGIN: "Слишком много попыток входа. Попробуйте через {minutes} мин.",
    SCOPE_CHANGE_PASSWORD: "Слишком много неверных попыток смены пароля. Попробуйте через {minutes} мин.",
}


class TooManyAttempts(HTTPException):
    """429 with ``Retry-After`` (seconds) and the Russian wait message."""

    def __init__(self, retry_after: int, scope: str = SCOPE_LOGIN) -> None:
        retry_after = max(int(retry_after), 1)
        minutes = max(math.ceil(retry_after / 60), 1)
        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_MESSAGES[scope].format(minutes=minutes),
            headers={"Retry-After": str(retry_after)},
        )
        self.retry_after = retry_after


def backoff_seconds(attempts: int) -> int:
    """How long the account is refused after its *attempts*-th consecutive attempt: 0 below the threshold,
    then 60, 120, 240, 480, 900, 900, ... seconds."""
    if attempts < FAILURE_THRESHOLD:
        return 0
    return min(BACKOFF_BASE_SECONDS * 2 ** min(attempts - FAILURE_THRESHOLD, 16), BACKOFF_CAP_SECONDS)


def normalise_identifier(raw: str) -> str:
    """The login identifier as the throttle sees it: the same text must give the same key whatever its case,
    surrounding whitespace, width/compatibility forms or invisible characters.

    The user lookup compares the already lower-cased, stripped string exactly, so every spelling that reaches
    one account reaches one key; folding more than the lookup does only ever merges keys (stricter, never
    looser), which a spoofed spelling cannot turn into a fresh counter.
    """
    text = unicodedata.normalize("NFKC", raw or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")  # zero-width, BOM, bidi marks
    return unicodedata.normalize("NFKC", text.strip().casefold())


def _key_base(scope: str, identifier: str) -> str:
    secret = get_settings().secret_key.encode()
    digest = hmac.new(secret, f"{scope}\0{normalise_identifier(identifier)}".encode(), hashlib.sha256)
    return f"lt:{scope}:{digest.hexdigest()[:32]}"


# --------------------------------------------------------------------------------------------------
# Storage. Both implementations answer: (allowed, seconds still blocked, attempts so far, block armed now)
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Decision:
    allowed: bool
    retry_after: int
    attempts: int
    armed_block: int  # seconds of block this very attempt started (0: none)


# KEYS[1] attempts counter, KEYS[2] block marker. ARGV: threshold, base, cap, quiet window (all seconds).
# One script = one atomic step, so parallel requests cannot interleave between "is it blocked" and "count".
_ENTER_LUA = """
local pttl = redis.call('PTTL', KEYS[2])
if pttl > 0 then
  return {0, pttl, tonumber(redis.call('GET', KEYS[1]) or '0'), 0}
end
local n = redis.call('INCR', KEYS[1])
local threshold = tonumber(ARGV[1])
local block = 0
if n >= threshold then
  local e = n - threshold
  if e > 16 then e = 16 end
  block = tonumber(ARGV[2]) * (2 ^ e)
  if block > tonumber(ARGV[3]) then block = tonumber(ARGV[3]) end
  redis.call('SET', KEYS[2], n, 'EX', block)
end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[4]) + block)
return {1, 0, n, block}
"""


def _enter_redis(base: str) -> Optional[_Decision]:
    r = redis_client.get_redis()
    if r is None:
        return None
    try:
        allowed, pttl, attempts, armed = r.eval(
            _ENTER_LUA,
            2,
            f"{base}:n",
            f"{base}:b",
            FAILURE_THRESHOLD,
            BACKOFF_BASE_SECONDS,
            BACKOFF_CAP_SECONDS,
            QUIET_WINDOW_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - a counter store must never break a login
        redis_client.mark_down(exc)
        return None
    return _Decision(bool(int(allowed)), math.ceil(int(pttl) / 1000), int(attempts), int(armed))


_MEMORY_SOFT_LIMIT = 50_000
_memory_lock = threading.Lock()
_memory: dict[str, list[float]] = {}  # base -> [attempts, forget_at, blocked_until]


def _now() -> float:
    return time.monotonic()


def _enter_memory(base: str) -> _Decision:
    now = _now()
    with _memory_lock:
        entry = _memory.get(base)
        if entry is not None and entry[1] <= now:
            entry = None
        if entry is not None and entry[2] > now:
            return _Decision(False, math.ceil(entry[2] - now), int(entry[0]), 0)
        attempts = int(entry[0]) + 1 if entry is not None else 1
        block = backoff_seconds(attempts)
        _memory[base] = [attempts, now + QUIET_WINDOW_SECONDS + block, now + block if block else 0.0]
        if len(_memory) > _MEMORY_SOFT_LIMIT:  # a flood of made-up identifiers must not grow memory forever
            for stale in [k for k, v in _memory.items() if v[1] <= now]:
                del _memory[stale]
    return _Decision(True, 0, attempts, block)


def _forget(base: str) -> None:
    with _memory_lock:
        _memory.pop(base, None)
    r = redis_client.get_redis()
    if r is None:
        return
    try:
        r.delete(f"{base}:n", f"{base}:b")
    except Exception as exc:  # noqa: BLE001
        redis_client.mark_down(exc)


def _undo(base: str, armed_block: int) -> None:
    """Take back one counted attempt that never reached a verdict (the request died with a server error)."""
    with _memory_lock:
        entry = _memory.get(base)
        if entry is not None and entry[0] > 0:
            entry[0] -= 1
            if armed_block:
                entry[2] = 0.0
    r = redis_client.get_redis()
    if r is None:
        return
    try:
        if int(r.get(f"{base}:n") or 0) > 0:
            r.decr(f"{base}:n")
        if armed_block:
            r.delete(f"{base}:b")
    except Exception as exc:  # noqa: BLE001
        redis_client.mark_down(exc)


# --------------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------------


class Attempt:
    """One counted attempt. Report its outcome: ``succeeded()``, ``failed()`` or ``aborted()``."""

    def __init__(self, scope: str, base: str, ip: str, decision: _Decision) -> None:
        self.scope = scope
        self._base = base
        self._ip = ip
        self._decision = decision

    def succeeded(self) -> None:
        """Right secret: the account's counter and block are cleared."""
        _forget(self._base)

    def failed(self) -> None:
        """Wrong secret (or unknown account): the attempt stays counted; log when it armed a block."""
        if self._decision.armed_block:
            logger.warning(
                "Login backoff: scope=%s key=%s ip=%s attempts=%d block_seconds=%d",
                self.scope,
                self._base.rsplit(":", 1)[-1][:8],
                self._ip,
                self._decision.attempts,
                self._decision.armed_block,
            )

    def aborted(self) -> None:
        """The request failed for a reason that says nothing about the secret (database down, bug): not counted."""
        _undo(self._base, self._decision.armed_block)


def begin(scope: str, identifier: str, ip: str) -> Attempt:
    """Count one attempt on *identifier* or raise ``TooManyAttempts`` (429) while it is in backoff."""
    base = _key_base(scope, identifier)
    decision = _enter_redis(base) or _enter_memory(base)
    if not decision.allowed:
        raise TooManyAttempts(decision.retry_after, scope)
    return Attempt(scope, base, ip, decision)


def enforce_login_ip_limits(request: Request) -> str:
    """FastAPI dependency: count this login attempt against its trusted client address; returns the address."""
    ip = client_ip(request)
    for label, limit, window in LOGIN_IP_LIMITS:
        count, seconds_left = hit_window(f"lt:ip:{SCOPE_LOGIN}:{label}:{ip}", window)
        if count > limit:
            if count == limit + 1:  # one log line per window, not one per refused request
                logger.warning("Login rate limit: ip=%s window=%s limit=%d", ip, label, limit)
            raise TooManyAttempts(seconds_left, SCOPE_LOGIN)
    return ip
