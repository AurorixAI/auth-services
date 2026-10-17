"""Per-device sessions: one signed-in device = one row of ``user_sessions`` (ROADMAP #101).

Before this module a user had ONE refresh token (``users.refresh_token_hash``) and every login and every refresh
overwrote it, so the same person on two devices (the owner on phone and laptop, one reception account on two PCs)
was signed out of the first device at its next refresh, at most an access-token lifetime (60 min) later.

The rules, in one place (``AuthService`` only calls these functions):

* **Issue** (login, dev quick-login, invitation accept, activation): a NEW row; no other session of the user is
  touched. At most ``MAX_ACTIVE_SESSIONS`` (20) stay active per user: beyond that the sessions used LEAST recently
  are revoked (``session_cap``). Sessions that expired more than ``EXPIRED_RETENTION_DAYS`` ago are deleted, a
  bounded batch per sign-in.
* **Refresh**: the session is found by the hash of the presented refresh token (unique index; no claim in the JWT
  is needed, so a token issued before the migration finds its backfilled session) and its row is locked
  (``FOR UPDATE``), so two parallel refreshes of one token cannot both rotate from the same hash:

  - the token is the session's CURRENT one, and the session is neither revoked nor expired -> rotate: the old hash
    becomes ``previous_token_hash``, ``refresh_token_hash`` is the new one, ``rotated_at`` / ``last_used_at`` are
    now, ``expires_at`` slides (the new refresh JWT gets a fresh TTL);
  - the token is the session's PREVIOUS one and was rotated at most ``GRACE_SECONDS`` (60) ago -> another tab of
    the same browser sent it concurrently: rotate again and answer with a valid pair, revoke nothing;
  - the token is the session's PREVIOUS one, later than that -> an old token was replayed: THAT session is revoked
    (``refresh_reuse``, WARNING with the user and session ids, never the token), every other session is untouched;
  - an unknown hash, a revoked or expired session, another user's session -> refused, nothing changes.
* **Logout** revokes only the session the refresh token belongs to (``logout``); a token that was rotated away is
  no session identity (it only gets its cookies removed by the caller).
* **Password change / account blocked**: every session of the user is revoked (``password_changed`` /
  ``user_blocked``).

Moments are naive UTC (TIME_CONVENTIONS.md, group "UTC"). Only the SHA-256 of a token is ever stored or logged
by id. ``users.refresh_token_hash`` is a legacy column: nothing reads or writes it since revision a7b8c9d0e1f
(a later migration drops it; the downgrade of a7b8c9d0e1f fills it again from the live sessions).

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

from src.models.auth import UserSession

logger = logging.getLogger(__name__)

GRACE_SECONDS = 60
MAX_ACTIVE_SESSIONS = 20
EXPIRED_RETENTION_DAYS = 30
SWEEP_BATCH = 100  # expired rows deleted per sign-in: cheap and bounded, the backlog drains over many sign-ins

USER_AGENT_MAX = 200
IP_MAX = 64

REVOKE_LOGOUT = "logout"
REVOKE_REFRESH_REUSE = "refresh_reuse"
REVOKE_PASSWORD_CHANGED = "password_changed"
REVOKE_USER_BLOCKED = "user_blocked"
REVOKE_SESSION_CAP = "session_cap"


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
        return str(ipaddress.ip_address(str(value).strip()))[:IP_MAX] if value else None
    except ValueError:
        return None


@dataclass(frozen=True)
class Claim:
    """A locked session that may be rotated; ``graced`` = the presented token was the PREVIOUS one (see module doc)."""

    session: UserSession
    graced: bool


# --------------------------------------------------------------------------- issuing


def _lock_user_sessions(db: Session, user_id) -> None:
    """Serialise the multi-row operations (issue + cap, revoke all) of ONE user for the rest of the transaction,
    so two parallel sign-ins cannot both see 19 sessions and end with 21, and two bulk updates cannot deadlock.
    A single-row lock (refresh, logout) does not need it."""
    if db.get_bind().dialect.name != "postgresql":
        return
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"user_sessions:{user_id}"})


def issue(
    db: Session,
    user_id,
    token_hash: str,
    ttl: timedelta,
    client: Optional[SessionClient] = None,
) -> UserSession:
    """Create the session of a freshly issued refresh token. Other sessions of the user are NOT touched, except that
    the ones beyond the cap are revoked and long-expired ones are deleted (module doc)."""
    now = _now()
    _lock_user_sessions(db, user_id)
    ip, user_agent = (client or SessionClient()).cleaned()
    session = UserSession(
        id=uuid4(),
        user_id=user_id,
        refresh_token_hash=token_hash,
        created_at=now,
        last_used_at=now,
        expires_at=now + ttl,
        ip=ip,
        user_agent=user_agent,
    )
    db.add(session)
    db.flush()  # the session factory runs with autoflush off: the statements below must see the new row
    _enforce_cap(db, user_id, session.id, now)
    _sweep_expired(db, now)
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


def _sweep_expired(db: Session, now: datetime) -> None:
    """Delete a bounded batch of sessions that expired more than ``EXPIRED_RETENTION_DAYS`` ago (SKIP LOCKED: a
    row another transaction is rotating or deleting is left to it, so this never waits and never deadlocks)."""
    cutoff = now - timedelta(days=EXPIRED_RETENTION_DAYS)
    stale = (
        select(UserSession.id)
        .where(UserSession.expires_at < cutoff)
        .order_by(UserSession.expires_at)
        .limit(SWEEP_BATCH)
        .with_for_update(skip_locked=True)
    )
    db.execute(delete(UserSession).where(UserSession.id.in_(stale)).execution_options(synchronize_session=False))


# --------------------------------------------------------------------------- refreshing


def _lock_one(db: Session, column, token_hash: str) -> Optional[UserSession]:
    """The session whose *column* equals the hash, locked FOR UPDATE and read fresh (``populate_existing``: a row
    another transaction changed while we waited for the lock must not be answered from a stale identity map)."""
    return (
        db.execute(
            select(UserSession)
            .where(column == token_hash)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .scalars()
        .first()
    )


def _usable(session: UserSession, user_id, now: datetime) -> bool:
    return (
        session.revoked_at is None
        and session.expires_at is not None
        and session.expires_at > now
        and str(session.user_id) == str(user_id)
    )


def claim(db: Session, user_id, token_hash: str) -> Optional[Claim]:
    """Lock the session the presented refresh-token hash belongs to and decide whether it may be rotated.

    Returns None when the token must be refused. When the refusal is a detected replay the session is revoked here
    (flushed, not committed): the caller commits before it answers 401, otherwise the revocation would be rolled
    back with the request.
    """
    now = _now()
    current = _lock_one(db, UserSession.refresh_token_hash, token_hash)
    if current is not None:
        # A parallel refresh of the same token that won the lock has rotated the row: PostgreSQL re-checks the
        # condition after the wait, so a loser does not get here but falls through to the "previous" lookup below.
        return Claim(current, graced=False) if _usable(current, user_id, now) else None

    previous = _lock_one(db, UserSession.previous_token_hash, token_hash)
    if previous is None or not _usable(previous, user_id, now):
        return None
    if previous.rotated_at is not None and now - previous.rotated_at <= timedelta(seconds=GRACE_SECONDS):
        return Claim(previous, graced=True)

    previous.revoked_at = now
    previous.revoke_reason = REVOKE_REFRESH_REUSE
    db.flush()
    logger.warning(
        "Refresh token reuse: an old refresh token was replayed, session revoked (user_id=%s session_id=%s)",
        previous.user_id,
        previous.id,
    )
    return None


def rotate(
    db: Session,
    session: UserSession,
    new_token_hash: str,
    ttl: timedelta,
    client: Optional[SessionClient] = None,
) -> None:
    """Make *new_token_hash* the session's current token; the one it replaces becomes the previous (grace) token."""
    now = _now()
    session.previous_token_hash = session.refresh_token_hash
    session.refresh_token_hash = new_token_hash
    session.rotated_at = now
    session.last_used_at = now
    session.expires_at = now + ttl
    if client is not None:
        ip, user_agent = client.cleaned()
        session.ip = ip or session.ip
        session.user_agent = user_agent or session.user_agent
    db.flush()


# --------------------------------------------------------------------------- revoking


def revoke_by_current_token(db: Session, user_id, token_hash: str, reason: str = REVOKE_LOGOUT) -> bool:
    """Revoke the live session whose CURRENT refresh token this is (and which belongs to *user_id*). A token that
    was rotated away, an unknown one, or an already revoked / expired session revokes nothing. Not committed."""
    now = _now()
    session = _lock_one(db, UserSession.refresh_token_hash, token_hash)
    if session is None or not _usable(session, user_id, now):
        return False
    session.revoked_at = now
    session.revoke_reason = reason
    db.flush()
    return True


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

