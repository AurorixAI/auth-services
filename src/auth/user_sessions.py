"""Per-device sessions: one signed-in device = one row of ``user_sessions`` (ROADMAP #101).

Before this module a user had ONE refresh token (``users.refresh_token_hash``) and every login and every refresh
overwrote it, so the same person on two devices (the owner on phone and laptop, one reception account on two PCs)
was signed out of the first device at its next refresh, at most an access-token lifetime (60 min) later.

Model: a session (the device) owns refresh tokens, ONE ROW EACH in ``user_session_tokens`` (SHA-256 only): LIVE
(``superseded_at IS NULL``) or SUPERSEDED. The rules (``AuthService`` only calls these functions):

* **Issue** (login, dev quick-login, invitation accept, password link): a NEW session with one live token; no other
  session of the user is touched (the session this BROWSER held before is ended by the sign-in route, ``revoke_by_token`` with
  ``new_login``). At most ``MAX_ACTIVE_SESSIONS`` (20) stay active per user: beyond that the
  sessions used LEAST recently are revoked (``session_cap``). Sessions that expired more than
  ``EXPIRED_RETENTION_DAYS`` ago (with their tokens, ON DELETE CASCADE) and tokens superseded more than
  ``SUPERSEDED_RETENTION_DAYS`` ago are deleted, a bounded batch per sign-in.
* **Refresh**: the token is found by its hash (primary key; no claim in the JWT is needed, so a token issued before
  the migration finds the token the backfill made for it), then its session and the token are locked FOR UPDATE:

  - the token is LIVE -> supersede ALL live tokens of the session (``superseded_at`` = now) and issue one new live
    token; ``last_used_at`` / ``expires_at`` slide;
  - the token is SUPERSEDED, at most ``GRACE_SECONDS`` (60) ago -> another tab of the same browser sent it
    concurrently: issue a NEW live SIBLING token and supersede nothing. So any number of simultaneous refreshes of
    one token all succeed, and whichever response the browser keeps stays valid until a live token of the
    session is used again;
  - the token is SUPERSEDED, longer ago -> an old token was replayed: THAT session is revoked (``refresh_reuse``,
    WARNING with the user and session ids, never the token), every other session is untouched;
  - an unknown hash, a revoked or expired session, another user's session -> refused, nothing changes. Superseded
    tokens are kept ``SUPERSEDED_RETENTION_DAYS`` (7) for replay detection and then deleted: the replay of a token
    that is gone is an unknown token, i.e. a plain refusal WITHOUT revoking the session.
* **Logout** revokes only the session the presented refresh token belongs to (``logout``): any LIVE token of it, or
  a token superseded at most 60 s ago (the browser just raced a refresh and still holds the older cookie). An older
  superseded token revokes nothing (the caller only clears the cookies).
* **Password change / account blocked**: every session of the user is revoked (``password_changed`` /
  ``user_blocked``).

Lock order, everywhere: the session row, then the token row. (Token first would deadlock two simultaneous refreshes
with two different live siblings of one session: each holds its own token row and waits for the session row while
the winner, superseding ALL live tokens, waits for the other's token row. The session lock serialises every
operation on a session, so the token lock under it is never contended.) The token -> session lookup that finds the
session to lock is a plain read: a token never changes its session.

Leeway trade-off (accepted): for ``GRACE_SECONDS`` after a refresh the superseded token still works, so a token
STOLEN just before the refresh is usable for at most that long (it can mint a sibling; the owner's next refresh
supersedes it, and a replay after the window revokes the session). A shorter window would make a legitimate race
(two tabs, a retry after a slow response) look like theft.

**Mode (K2-1, D).** A session is created with ``remember``: True = «Запомнить меня», ``expires_at`` slides to now + the remember lifetime
(14 days, ``JWT_REFRESH_TTL_DAYS``) at EVERY refresh, no absolute ceiling; False = ``expires_at`` is fixed at ``SHORT_SESSION_HOURS`` (12 h)
from the sign-in and a refresh does NOT extend it. The mode is a column of the session row, so it survives any number of rotations
(two in a row, a race of two tabs): the refresh token carries no mode at all.

Moments are naive UTC (TIME_CONVENTIONS.md, group "UTC"). Privacy: ``ip`` and ``user_agent`` are kept until the
session's expiry plus the sweep window (``EXPIRED_RETENTION_DAYS``), then they are deleted with the row.
``users.refresh_token_hash`` is a legacy column: nothing reads or writes it since revision a7b8c9d0e1f (a later
migration drops it; the downgrade of a7b8c9d0e1f fills it again from the live sessions).

This module takes token HASHES and never imports ``service`` (``service`` imports it); the trusted client
address is resolved by the router (``rate_limit.client_ip``) and handed over as ``SessionClient``.
"""
from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from src.models.auth import UserSession, UserSessionToken

logger = logging.getLogger(__name__)

GRACE_SECONDS = 60
MAX_ACTIVE_SESSIONS = 20
EXPIRED_RETENTION_DAYS = 30
SUPERSEDED_RETENTION_DAYS = 7
SWEEP_BATCH = 100  # rows deleted per sign-in and per kind: cheap and bounded, a backlog drains over many sign-ins

USER_AGENT_MAX = 200

REVOKE_LOGOUT = "logout"
REVOKE_REFRESH_REUSE = "refresh_reuse"
REVOKE_PASSWORD_CHANGED = "password_changed"
REVOKE_USER_BLOCKED = "user_blocked"
REVOKE_SESSION_CAP = "session_cap"
REVOKE_NEW_LOGIN = "new_login"  # the browser signed in again: the session its refresh cookie still named is over (A3)

SHORT_SESSION_HOURS = 12  # a session WITHOUT «Запомнить меня»: at most this long from the sign-in, never extended


def _now() -> datetime:
    """Naive UTC (tests move the clock by replacing this function)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class SessionClient:
    """Where a sign-in / refresh came from. ``ip`` is the TRUSTED client address (``rate_limit.client_ip``:
    X-Real-IP set by nginx), never a raw header; ``user_agent`` is client input and is only stored, truncated."""

    ip: Optional[str] = None
    user_agent: Optional[str] = None

    def cleaned(self) -> tuple[Optional[str], Optional[str]]:
        return _clean_ip(self.ip), _clean_text(self.user_agent, USER_AGENT_MAX)


def _clean_text(value: Optional[str], limit: int) -> Optional[str]:
    """Printable text of at most *limit* characters, or None (PostgreSQL text cannot hold a NUL byte)."""
    if not value:
        return None
    value = "".join(ch for ch in str(value) if ch.isprintable()).strip()
    return value[:limit] or None


def _clean_ip(value: Optional[str]) -> Optional[str]:
    """The address as a normalised IP, or None when it is not one (a socket peer such as ``testclient``)."""
    try:
        return str(ipaddress.ip_address(str(value).strip())) if value else None
    except ValueError:
        return None


@dataclass(frozen=True)
class Claim:
    """A locked session and the presented token, which may be exchanged for a new one.

    ``graced`` = the token was already SUPERSEDED (within the grace): the new token is a sibling and the live ones
    stay live."""

    session: UserSession
    token: UserSessionToken
    graced: bool


# --------------------------------------------------------------------------- issuing


def _lock_user_sessions(db: Session, user_id) -> None:
    """Serialise the multi-row operations (issue + cap, revoke all) of ONE user for the rest of the transaction,
    so two parallel sign-ins cannot both see 19 sessions and end with 21, and two bulk updates cannot deadlock.
    A single-session operation (refresh, logout) does not need it: it locks its session row."""
    if db.get_bind().dialect.name != "postgresql":
        return
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"user_sessions:{user_id}"})


def lock_user(db: Session, user_id) -> None:
    """Take the per-user session lock (the one ``issue`` and ``revoke_all`` take) for the rest of the transaction. The lock is re-entrant inside one
    transaction, so a sign-in takes it BEFORE it re-checks the password it verified (F8) and ``issue`` then finds it already held."""
    _lock_user_sessions(db, user_id)


def issue(
    db: Session,
    user_id,
    token_hash: str,
    ttl: timedelta,
    client: Optional[SessionClient] = None,
    *,
    remember: bool = False,
    session_id=None,
) -> UserSession:
    """Create the session of a freshly issued refresh token, with that token as its first live one. Other sessions
    of the user are NOT touched, except that the ones beyond the cap are revoked and old rows are deleted.

    *ttl* is the lifetime of a REMEMBERED session (it slides at every refresh); without *remember* the session lives
    ``SHORT_SESSION_HOURS`` from now and *ttl* is not used. *session_id* lets the caller put the id into the access
    token (``sid``) before the row exists."""
    now = _now()
    _lock_user_sessions(db, user_id)
    ip, user_agent = (client or SessionClient()).cleaned()
    session = UserSession(
        id=session_id or uuid4(),
        user_id=user_id,
        created_at=now,
        last_used_at=now,
        expires_at=now + (ttl if remember else timedelta(hours=SHORT_SESSION_HOURS)),
        remember=bool(remember),
        ip=ip,
        user_agent=user_agent,
    )
    db.add(session)
    db.flush()  # the session factory runs with autoflush off: the statements below must see the new row
    db.add(UserSessionToken(token_hash=token_hash, session_id=session.id, issued_at=now))
    db.flush()
    _enforce_cap(db, user_id, session.id, now)
    _sweep(db, now)
    return session


def _enforce_cap(db: Session, user_id, keep_id, now: datetime) -> None:
    """Revoke the active sessions of the user beyond the newest ``MAX_ACTIVE_SESSIONS`` (least recently used first
    to go). The session being issued always stays."""
    beyond_cap = (
        select(UserSession.id)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None), UserSession.expires_at > now)
        .order_by((UserSession.id == keep_id).desc(), UserSession.last_used_at.desc(), UserSession.created_at.desc())
        .offset(MAX_ACTIVE_SESSIONS)
    )
    db.execute(
        update(UserSession)
        .where(UserSession.id.in_(beyond_cap))
        .values(revoked_at=now, revoke_reason=REVOKE_SESSION_CAP)
        .execution_options(synchronize_session="fetch")
    )


def _sweep(db: Session, now: datetime) -> None:
    """Delete a bounded batch of sessions that expired more than ``EXPIRED_RETENTION_DAYS`` ago (their tokens go
    with them, ON DELETE CASCADE) and of tokens superseded more than ``SUPERSEDED_RETENTION_DAYS`` ago. SKIP
    LOCKED: a row another transaction is rotating or deleting is left to it, so this never waits and never
    deadlocks."""
    stale_sessions = (
        select(UserSession.id)
        .where(UserSession.expires_at < now - timedelta(days=EXPIRED_RETENTION_DAYS))
        .order_by(UserSession.expires_at)
        .limit(SWEEP_BATCH)
        .with_for_update(skip_locked=True)
    )
    db.execute(delete(UserSession).where(UserSession.id.in_(stale_sessions)).execution_options(synchronize_session=False))
    stale_tokens = (
        select(UserSessionToken.token_hash)
        .where(UserSessionToken.superseded_at < now - timedelta(days=SUPERSEDED_RETENTION_DAYS))
        .order_by(UserSessionToken.superseded_at)
        .limit(SWEEP_BATCH)
        .with_for_update(skip_locked=True)
    )
    db.execute(
        delete(UserSessionToken).where(UserSessionToken.token_hash.in_(stale_tokens)).execution_options(synchronize_session=False)
    )


# --------------------------------------------------------------------------- locking


def _lock_session_and_token(db: Session, token_hash: str):
    """(session, token) of the token, both locked FOR UPDATE in the fixed order session -> token and read fresh
    (``populate_existing``: a row another transaction changed while we waited must not be answered from a stale
    identity map); (None, None) when the token is unknown. The first lookup is a plain read: it only finds the session
    to lock, and a token never changes its session."""
    session_id = db.execute(
        select(UserSessionToken.session_id).where(UserSessionToken.token_hash == token_hash)
    ).scalar_one_or_none()
    if session_id is None:
        return None, None
    session = (
        db.execute(
            select(UserSession).where(UserSession.id == session_id).with_for_update().execution_options(populate_existing=True)
        )
        .scalars()
        .first()
    )
    token = (
        db.execute(
            select(UserSessionToken)
            .where(UserSessionToken.token_hash == token_hash)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .scalars()
        .first()
    )
    if session is None or token is None or token.session_id != session.id:
        return None, None  # swept between the two reads
    return session, token


def _usable(session: UserSession, user_id, now: datetime) -> bool:
    return (
        session.revoked_at is None
        and session.expires_at is not None
        and session.expires_at > now
        and str(session.user_id) == str(user_id)
    )


def _within_grace(token: UserSessionToken, now: datetime) -> bool:
    return token.superseded_at is not None and now - token.superseded_at <= timedelta(seconds=GRACE_SECONDS)


# --------------------------------------------------------------------------- refreshing


def claim(db: Session, user_id, token_hash: str) -> Optional[Claim]:
    """Lock the session the presented refresh-token hash belongs to and decide whether the token may be exchanged.

    Returns None when the token must be refused. When the refusal is a detected replay the session is revoked here
    (flushed, not committed): the caller commits before it answers 401, otherwise the revocation would be rolled
    back with the request.
    """
    now = _now()
    session, token = _lock_session_and_token(db, token_hash)
    if session is None or not _usable(session, user_id, now):
        return None
    if token.superseded_at is None:
        return Claim(session, token, graced=False)
    if _within_grace(token, now):
        return Claim(session, token, graced=True)

    session.revoked_at = now
    session.revoke_reason = REVOKE_REFRESH_REUSE
    db.flush()
    logger.warning(
        "Refresh token reuse: an old refresh token was replayed, session revoked (user_id=%s session_id=%s)",
        session.user_id,
        session.id,
    )
    return None


def rotate(
    db: Session,
    claim_: Claim,
    new_token_hash: str,
    ttl: timedelta,
    client: Optional[SessionClient] = None,
) -> None:
    """Exchange the claimed token for *new_token_hash*: a live token supersedes every live token of the session; a
    superseded one (the grace) adds a sibling and supersedes nothing. *ttl* slides the end of a REMEMBERED session only."""
    now = _now()
    session = claim_.session
    if not claim_.graced:
        db.execute(
            update(UserSessionToken)
            .where(UserSessionToken.session_id == session.id, UserSessionToken.superseded_at.is_(None))
            .values(superseded_at=now)
            .execution_options(synchronize_session="fetch")
        )
    db.add(UserSessionToken(token_hash=new_token_hash, session_id=session.id, issued_at=now))
    # this session's old superseded tokens go now (we hold its lock): its rows stay bounded without a global sweep
    db.execute(
        delete(UserSessionToken)
        .where(
            UserSessionToken.session_id == session.id,
            UserSessionToken.superseded_at < now - timedelta(days=SUPERSEDED_RETENTION_DAYS),
        )
        .execution_options(synchronize_session=False)
    )
    session.last_used_at = now
    if session.remember:  # «Запомнить меня»: 14 days from THIS refresh; the short session keeps its fixed end (sign-in + 12 h)
        session.expires_at = now + ttl
    if client is not None:
        ip, user_agent = client.cleaned()
        session.ip = ip or session.ip
        session.user_agent = user_agent or session.user_agent
    db.flush()


# --------------------------------------------------------------------------- revoking


def revoke_by_token(db: Session, user_id, token_hash: str, reason: str = REVOKE_LOGOUT) -> bool:
    """Revoke the live session this refresh token belongs to (and which belongs to *user_id*) when the token is a
    LIVE one or was superseded at most ``GRACE_SECONDS`` ago (the browser just raced a refresh and holds the older
    cookie). An older superseded token, an unknown one, an already revoked or expired session revokes nothing.
    Not committed."""
    now = _now()
    session, token = _lock_session_and_token(db, token_hash)
    if session is None or not _usable(session, user_id, now):
        return False
    if token.superseded_at is not None and not _within_grace(token, now):
        return False
    session.revoked_at = now
    session.revoke_reason = reason
    db.flush()
    return True


def end_browser_session(db: Session, token_hash: str, reason: str = REVOKE_NEW_LOGIN) -> bool:
    """The browser signs in again (A3): revoke the live session the refresh cookie it still holds names, WHOEVER owns it - on a shared
    computer the cookie of the person who used it before must not stay alive after the next person signs in. The cookie is the proof
    (its hash is looked up); the same rule as ``revoke_by_token`` otherwise: a live token or one superseded at most ``GRACE_SECONDS``
    ago. Unknown, old, already revoked or expired -> False, nothing changes. Not committed."""
    now = _now()
    session, token = _lock_session_and_token(db, token_hash)
    if session is None or session.revoked_at is not None or session.expires_at is None or session.expires_at <= now:
        return False
    if token.superseded_at is not None and not _within_grace(token, now):
        return False
    session.revoked_at = now
    session.revoke_reason = reason
    db.flush()
    return True


def revoke_by_id(db: Session, user_id, session_id, reason: str = REVOKE_LOGOUT) -> bool:
    """Revoke the live session *session_id* when it belongs to *user_id* (the ``sid`` of an access token). Not committed."""
    result = db.execute(
        update(UserSession)
        .where(
            UserSession.id == session_id,
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > _now(),
        )
        .values(revoked_at=_now(), revoke_reason=reason)
        .execution_options(synchronize_session="fetch")
    )
    return bool(isinstance(result.rowcount, int) and result.rowcount > 0)


def is_alive(db: Session, user_id, session_id) -> bool:
    """The session an access token names (``sid``) is live: not revoked, not expired, and it is the token user's own (A5).
    One primary-key read per request, no cache: a revocation (sign-out, password, block, removal) ends the access token at once."""
    if session_id is None:
        return False
    row = db.execute(
        select(UserSession.id).where(
            UserSession.id == session_id,
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > _now(),
        )
    ).first()
    return row is not None


def revoke_all(db: Session, user_id, reason: str) -> int:
    """Revoke every live session of the user (password changed, account blocked). Not committed. Returns how many."""
    _lock_user_sessions(db, user_id)
    result = db.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=_now(), revoke_reason=reason)
        .execution_options(synchronize_session="fetch")
    )
    count = result.rowcount
    return count if isinstance(count, int) and count > 0 else 0
