"""Brute-force protection for the endpoints that verify a password.

Three independent limits, all answering ``429`` + ``Retry-After`` with a Russian message:

1. **Per client address** (``enforce_login_ip_limits``): 30 attempts/minute and 300/hour for every login
   attempt, counted on the TRUSTED client bucket (``rate_limit.client_bucket``: X-Real-IP set by our nginx,
   else the rightmost X-Forwarded-For element, else the socket peer - never the leftmost element a client
   typed; an IPv6 client is counted by its /64). A school behind one NAT logging in at 08:00 stays far below.

2. **Per account AND address** (``begin``): after ``FAILURE_THRESHOLD`` (5) consecutive attempts that fail
   from one address, THAT address is refused for this account for 1 min, then 2, 4, 8 and at most
   ``BACKOFF_CAP_SECONDS`` (15 min) for every further failure. So somebody guessing from their own address
   blocks only themselves: the teacher signing in from the school is not affected.

3. **Per account, all addresses** (the ceiling): ``ACCOUNT_CEILING_THRESHOLD`` (20) attempts that fail within
   15 minutes from ANY addresses put the account itself in the same backoff schedule. Only a distributed
   attack (more addresses than limit 2 can see) gets there. An address (client bucket) the owner has already
   signed in from is exempt, see "Known-good addresses": a distributed attack must not lock the real user
   out of the school or the home they always sign in from. A NEW address is refused during the ceiling
   backoff, which is the price of stopping the attack.

Known-good addresses: every successful sign-in (and successful password change) records its client bucket (the
same /64-aware bucket as everywhere) for that account, for ``KNOWN_GOOD_TTL_SECONDS`` (30 days, renewed by each
success), at most ``KNOWN_GOOD_MAX`` (20, the most recent). An attempt from a known-good bucket is neither
refused by the ceiling nor counted into it (its own failures only feed limit 2, the per-address backoff,
which still applies in full); an unknown bucket behaves exactly as before. Only a SUCCESS adds to the list, so
an attacker who has no password cannot grow it, and a success from a known-good bucket does not clear the
ceiling (a legitimate sign-in must not reset the evidence of an attack in progress; a success from a new bucket
cannot happen while the ceiling refuses that bucket). The list holds an HMAC of the bucket per account, not
the address.

Nothing is ever locked for good and nothing needs unlocking by hand: a success clears the counters of the
account (this address, and the ceiling when the address was not known-good), and a counter is forgotten after
``QUIET_WINDOW_SECONDS`` without attempts. While an account is refused the request is answered before any user
lookup or password check (no timing difference between a refused real account and a refused unknown one, and a
refused attempt cannot be used to test a guess). Refused attempts are not counted and do not extend a block.

An attempt is counted BEFORE the password is checked, in one atomic step for both counters, so a burst of
parallel guesses cannot all slip through a "check, then count" gap: at most 5 per address (and 20 per
account) are ever evaluated. The known-good lookup is part of that same step (one Lua script), so the decision
"refuse by the ceiling or not" and the counting always agree about whether the address is known-good.

An unknown identifier is counted exactly like a known one, so the 429 reveals nothing about whether the
account exists. The identifier is normalised (NFKC, case-folded, invisible characters removed) and keyed by
an HMAC under SECRET_KEY: the store never holds an email and cannot be dictionary-attacked offline.

Storage: Redis (``REDIS_URL``; counters and the known-good lists are shared between workers/replicas and
survive a restart) with atomic Lua / MULTI; if Redis is unreachable the same logic runs on per-process memory
(fail OPEN for availability, one warning per outage from ``redis_client``; the known-good lists kept in Redis
are not visible during an outage, so a known address is then treated like any other until Redis is back).
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

from src.api.rate_limit import client_bucket, hit_window
from src.core import redis_client
from src.core.config import get_settings

logger = logging.getLogger(__name__)

FAILURE_THRESHOLD = 5  # per account and address
ACCOUNT_CEILING_THRESHOLD = 20  # per account, all addresses together
BACKOFF_BASE_SECONDS = 60
BACKOFF_CAP_SECONDS = 15 * 60
QUIET_WINDOW_SECONDS = 15 * 60
KNOWN_GOOD_TTL_SECONDS = 30 * 86400  # a client bucket stays "known-good" this long after the last success from it
KNOWN_GOOD_MAX = 20  # ...and at most this many (the most recent) per account

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


def backoff_seconds(attempts: int, threshold: int = FAILURE_THRESHOLD) -> int:
    """How long a counter that reached *attempts* refuses: 0 below *threshold*, then 60, 120, 240, 480, 900,
    900, ... seconds."""
    if attempts < threshold:
        return 0
    return min(BACKOFF_BASE_SECONDS * 2 ** min(attempts - threshold, 16), BACKOFF_CAP_SECONDS)


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
    """Key of the account (all addresses): ``lt:<scope>:<hmac>``."""
    secret = get_settings().secret_key.encode()
    digest = hmac.new(secret, f"{scope}\0{normalise_identifier(identifier)}".encode(), hashlib.sha256)
    return f"lt:{scope}:{digest.hexdigest()[:32]}"


def _pair_base(account_base: str, bucket: str) -> str:
    """Key of the account as seen from one client bucket."""
    return f"{account_base}@{bucket}"


def _good_member(account_base: str, bucket: str) -> str:
    """What the known-good list of an account stores for a client bucket: an HMAC under SECRET_KEY that also
    covers the account, so the list holds no address and the same address is not linkable across accounts."""
    secret = get_settings().secret_key.encode()
    digest = hmac.new(secret, f"good\0{account_base}\0{bucket}".encode(), hashlib.sha256)
    return digest.hexdigest()[:24]


def _good_key(account_base: str) -> str:
    """Redis key of the known-good list (a sorted set: member -> time of the last success)."""
    return f"{account_base}:g"


# --------------------------------------------------------------------------------------------------
# Storage. Both implementations make one atomic decision over BOTH counters.
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Decision:
    allowed: bool
    retry_after: int
    pair_attempts: int = 0
    pair_block: int = 0  # seconds of block this very attempt started for the address (0: none)
    account_attempts: int = 0
    account_block: int = 0  # ...and for the account as a whole
    known: bool = False  # the address is known-good for the account: the ceiling neither refuses nor counts it


# KEYS: 1 address counter, 2 address block, 3 account counter, 4 account block, 5 known-good list of the account.
# ARGV: base, cap, quiet window, address threshold, account threshold (seconds / counts), the address' member
# in the known-good list, wall-clock now and the known-good lifetime (seconds).
# One script = one atomic step, so parallel requests cannot interleave between "is it blocked" and "count",
# and the known-good lookup that decides whether the ceiling applies is part of that same step. A known-good
# address skips the ceiling entirely (not refused by it, not counted into it); its own counter still applies.
_ENTER_LUA = """
local pair_ttl = redis.call('PTTL', KEYS[2])
local known = 0
local seen = redis.call('ZSCORE', KEYS[5], ARGV[6])
if seen and tonumber(seen) > tonumber(ARGV[7]) - tonumber(ARGV[8]) then known = 1 end
local account_ttl = 0
if known == 0 then account_ttl = redis.call('PTTL', KEYS[4]) end
if pair_ttl > 0 or account_ttl > 0 then
  local wait = pair_ttl
  if account_ttl > wait then wait = account_ttl end
  return {0, wait, 0, 0, 0, 0, known}
end
local base = tonumber(ARGV[1])
local cap = tonumber(ARGV[2])
local quiet = tonumber(ARGV[3])
local function count(n_key, b_key, threshold)
  local n = redis.call('INCR', n_key)
  local block = 0
  if n >= threshold then
    local e = n - threshold
    if e > 16 then e = 16 end
    block = base * (2 ^ e)
    if block > cap then block = cap end
    redis.call('SET', b_key, n, 'EX', block)
  end
  redis.call('EXPIRE', n_key, quiet + block)
  return {n, block}
end
local pair = count(KEYS[1], KEYS[2], tonumber(ARGV[4]))
local account = {0, 0}
if known == 0 then account = count(KEYS[3], KEYS[4], tonumber(ARGV[5])) end
return {1, 0, pair[1], pair[2], account[1], account[2], known}
"""


def _enter_redis(pair: str, account: str, member: str) -> Optional[_Decision]:
    r = redis_client.get_redis()
    if r is None:
        return None
    try:
        allowed, pttl, p_n, p_block, a_n, a_block, known = r.eval(
            _ENTER_LUA,
            5,
            f"{pair}:n",
            f"{pair}:b",
            f"{account}:n",
            f"{account}:b",
            _good_key(account),
            BACKOFF_BASE_SECONDS,
            BACKOFF_CAP_SECONDS,
            QUIET_WINDOW_SECONDS,
            FAILURE_THRESHOLD,
            ACCOUNT_CEILING_THRESHOLD,
            member,
            _wall(),
            KNOWN_GOOD_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - a counter store must never break a login
        redis_client.mark_down(exc)
        return None
    return _Decision(
        bool(int(allowed)), math.ceil(int(pttl) / 1000), int(p_n), int(p_block), int(a_n), int(a_block), bool(int(known))
    )


_MEMORY_SOFT_LIMIT = 50_000
_memory_lock = threading.Lock()
_memory: dict[str, list[float]] = {}  # base -> [attempts, forget_at, blocked_until]
_known: dict[str, dict[str, float]] = {}  # account base -> {known-good member: wall time of its last success}


def _now() -> float:
    return time.monotonic()


def _wall() -> float:
    """Wall-clock seconds: the known-good lists outlive a process, so they are dated with real time."""
    return time.time()


def _count_memory(base: str, threshold: int, now: float) -> tuple[int, int]:
    """Count one attempt on *base* (the lock is held): (attempts so far, seconds of block it armed)."""
    entry = _memory.get(base)
    if entry is not None and entry[1] <= now:
        entry = None
    attempts = int(entry[0]) + 1 if entry is not None else 1
    block = backoff_seconds(attempts, threshold)
    _memory[base] = [attempts, now + QUIET_WINDOW_SECONDS + block, now + block if block else 0.0]
    return attempts, block


def _blocked_for(base: str, now: float) -> float:
    entry = _memory.get(base)
    if entry is None or entry[1] <= now:
        return 0.0
    return max(entry[2] - now, 0.0)


def _is_known_good_memory(account: str, member: str, wall: float) -> bool:
    """(the lock is held) Has this address succeeded for the account within the known-good lifetime?"""
    seen = _known.get(account, {}).get(member)
    return seen is not None and seen > wall - KNOWN_GOOD_TTL_SECONDS


def _enter_memory(pair: str, account: str, member: str) -> _Decision:
    now = _now()
    wall = _wall()
    with _memory_lock:
        known = _is_known_good_memory(account, member, wall)
        wait = _blocked_for(pair, now)
        if not known:
            wait = max(wait, _blocked_for(account, now))
        if wait > 0:
            return _Decision(False, math.ceil(wait))
        p_n, p_block = _count_memory(pair, FAILURE_THRESHOLD, now)
        a_n, a_block = (0, 0) if known else _count_memory(account, ACCOUNT_CEILING_THRESHOLD, now)
        if len(_memory) > _MEMORY_SOFT_LIMIT:  # a flood of made-up identifiers must not grow memory forever
            for stale in [k for k, v in _memory.items() if v[1] <= now]:
                del _memory[stale]
    return _Decision(True, 0, p_n, p_block, a_n, a_block, known)


def _remember_memory(account: str, member: str) -> None:
    """Record a success on the in-process fallback: renew the address, drop the expired, keep the newest MAX."""
    wall = _wall()
    with _memory_lock:
        entries = _known.setdefault(account, {})
        entries[member] = wall
        for old in [m for m, seen in entries.items() if seen <= wall - KNOWN_GOOD_TTL_SECONDS]:
            del entries[old]
        for old in sorted(entries, key=entries.__getitem__)[: max(len(entries) - KNOWN_GOOD_MAX, 0)]:
            del entries[old]
        if len(_known) > _MEMORY_SOFT_LIMIT:  # only successes add accounts, but memory is still bounded
            for stale in [k for k, v in _known.items() if not any(s > wall - KNOWN_GOOD_TTL_SECONDS for s in v.values())]:
                del _known[stale]


def _remember_redis(account: str, member: str) -> bool:
    """Record a success in Redis in ONE transaction (renew the address, drop the expired, keep the newest MAX,
    renew the list's own TTL). False when Redis is unavailable (the caller then uses memory)."""
    r = redis_client.get_redis()
    if r is None:
        return False
    key = _good_key(account)
    wall = _wall()
    try:
        pipe = r.pipeline(transaction=True)
        pipe.zadd(key, {member: wall})
        pipe.zremrangebyscore(key, "-inf", wall - KNOWN_GOOD_TTL_SECONDS)
        pipe.zremrangebyrank(key, 0, -(KNOWN_GOOD_MAX + 1))
        pipe.expire(key, KNOWN_GOOD_TTL_SECONDS)
        pipe.execute()
    except Exception as exc:  # noqa: BLE001 - remembering an address must never break a login
        redis_client.mark_down(exc)
        return False
    return True


def _remember_good(account: str, member: str) -> None:
    if not _remember_redis(account, member):
        _remember_memory(account, member)


def _forget(*bases: str) -> None:
    with _memory_lock:
        for base in bases:
            _memory.pop(base, None)
    r = redis_client.get_redis()
    if r is None:
        return
    try:
        r.delete(*[f"{base}:{suffix}" for base in bases for suffix in ("n", "b")])
    except Exception as exc:  # noqa: BLE001
        redis_client.mark_down(exc)


def _undo(pair: str, account: str, decision: _Decision) -> None:
    """Take back one counted attempt that never reached a verdict (the request died with a server error)."""
    armed = ((pair, decision.pair_block),)
    if not decision.known:  # a known-good address was never counted into the ceiling
        armed += ((account, decision.account_block),)
    with _memory_lock:
        for base, block in armed:
            entry = _memory.get(base)
            if entry is not None and entry[0] > 0:
                entry[0] -= 1
                if block:
                    entry[2] = 0.0
    r = redis_client.get_redis()
    if r is None:
        return
    try:
        for base, block in armed:
            if int(r.get(f"{base}:n") or 0) > 0:
                r.decr(f"{base}:n")
            if block:
                r.delete(f"{base}:b")
    except Exception as exc:  # noqa: BLE001
        redis_client.mark_down(exc)


# --------------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------------


class Attempt:
    """One counted attempt. Report its outcome: ``succeeded()``, ``failed()`` or ``aborted()``."""

    def __init__(
        self, scope: str, account: str, pair: str, bucket: str, decision: _Decision, member: Optional[str] = None
    ) -> None:
        self.scope = scope
        self._account = account
        self._pair = pair
        self._bucket = bucket
        self._decision = decision
        self._member = member if member is not None else _good_member(account, bucket)

    def succeeded(self) -> None:
        """Right secret: this address' counter is cleared, and so is the ceiling unless the address was
        known-good (a legitimate sign-in does not erase the evidence of an attack on the account); the address
        becomes (or stays) known-good for the account for another 30 days."""
        if self._decision.known:
            _forget(self._pair)
        else:
            _forget(self._pair, self._account)
        _remember_good(self._account, self._member)

    def failed(self) -> None:
        """Wrong secret (or unknown account): the attempt stays counted; log when it armed a block."""
        key = self._account.rsplit(":", 1)[-1][:8]  # a prefix of the HMAC, never the identifier
        d = self._decision
        if d.pair_block:
            logger.warning(
                "Login backoff: scope=%s key=%s ip=%s attempts=%d block_seconds=%d",
                self.scope, key, self._bucket, d.pair_attempts, d.pair_block,
            )
        if d.account_block:
            logger.warning(
                "Login backoff for the account from all addresses: scope=%s key=%s ip=%s attempts=%d block_seconds=%d",
                self.scope, key, self._bucket, d.account_attempts, d.account_block,
            )

    def aborted(self) -> None:
        """The request failed for a reason that says nothing about the secret (database down, bug): not counted."""
        _undo(self._pair, self._account, self._decision)


def begin(scope: str, identifier: str, bucket: str) -> Attempt:
    """Count one attempt on *identifier* from client *bucket* or raise ``TooManyAttempts`` (429) while the
    account is in backoff for that address or, unless the address is known-good for it, for all addresses."""
    account = _key_base(scope, identifier)
    pair = _pair_base(account, bucket)
    member = _good_member(account, bucket)
    decision = _enter_redis(pair, account, member) or _enter_memory(pair, account, member)
    if not decision.allowed:
        raise TooManyAttempts(decision.retry_after, scope)
    return Attempt(scope, account, pair, bucket, decision, member)


def enforce_login_ip_limits(request: Request) -> str:
    """FastAPI dependency: count this login attempt against its client bucket; returns the bucket."""
    bucket = client_bucket(request)
    for label, limit, window in LOGIN_IP_LIMITS:
        count, seconds_left = hit_window(f"lt:ip:{SCOPE_LOGIN}:{label}:{bucket}", window)
        if count > limit:
            if count == limit + 1:  # one log line per window, not one per refused request
                logger.warning("Login rate limit: ip=%s window=%s limit=%d", bucket, label, limit)
            raise TooManyAttempts(seconds_left, SCOPE_LOGIN)
    return bucket
