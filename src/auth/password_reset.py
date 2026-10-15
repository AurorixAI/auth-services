"""Password recovery for staff accounts (N13): one-time links, who may hand one out, how one is spent.

Two ways to the same page ``/reset-password?token=...``:

* ``email``  - the user pressed «Забыли пароль?» on the login page; the link is e-mailed, valid 1 hour;
* ``issued`` - an owner / admin / superadmin handed the link over (the teacher has no working e-mail, or
  SMTP is not configured yet); valid 24 hours.

Rules that hold for both:

* The token is 32 random bytes (``secrets.token_urlsafe``). Only its sha256 is stored; the raw token
  exists in the e-mail / in the one response that issued it, and is never logged.
* One live link per account: issuing a new one expires the older unused ones.
* A link is spent atomically (``UPDATE ... WHERE used_at IS NULL AND expires_at > now RETURNING``): two
  concurrent requests with the same token cannot both change the password.
* Spending it changes the password, ends EVERY login of the account (refresh token dropped, access JWTs
  issued before now rejected) and does NOT sign anybody in.

This module is database logic only: the HTTP layer is ``password_reset_router.py``.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from urllib.parse import quote, urlsplit
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.orm import Session

from src.core.config import get_settings
from src.models.auth import OrganizationMember, PasswordResetToken, User

from .service import hash_password, hash_token

logger = logging.getLogger(__name__)

TOKEN_BYTES = 32
PURPOSE_EMAIL = "email"
PURPOSE_ISSUED = "issued"
EMAIL_LINK_TTL = timedelta(hours=1)
ISSUED_LINK_TTL = timedelta(hours=24)

INVALID_LINK_MESSAGE = "Ссылка недействительна или устарела. Попросите новую."
STAFF_NOT_FOUND_MESSAGE = "Сотрудник не найден"

# What ``secrets.token_urlsafe(32)`` produces is 43 characters of this alphabet. Anything else cannot
# be one of our tokens: it is rejected before it is hashed or looked up.
_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

# Which organisation role may receive a link from which organisation role.
_ISSUER_MAY_RESET = {"owner": {"admin", "teacher"}, "admin": {"teacher"}}


class InvalidResetLink(Exception):
    """The link does not exist, was used, expired, or its account is disabled - one answer for all."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_raw_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_digest(raw_token: str) -> str:
    """sha256 hex of the raw token - what the table stores. (A 256-bit random token needs no salt.)"""
    return hash_token(raw_token)


def looks_like_token(raw_token: object) -> bool:
    return isinstance(raw_token, str) and bool(_TOKEN_SHAPE.match(raw_token))


# ---------------------------------------------------------------------------
# Where the link points to
# ---------------------------------------------------------------------------


def _clean_base(value: str) -> Optional[str]:
    """*value* as ``scheme://host[:port]`` (no path, no credentials), or None."""
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def frontend_base_url(origin: Optional[str]) -> Optional[str]:
    """The site the link must open: ``FRONTEND_URL``, else the request's Origin IF it is a CORS origin.

    Never the Host / X-Forwarded-Host header, and never an Origin outside the configured CORS list (a
    wildcard list is not a whitelist: with ``*`` the Origin is not trusted at all) - otherwise anybody
    could have the e-mail carry a link to their own site. None: neither is available.
    """
    base = os.getenv("FRONTEND_URL", "")
    if base.strip():
        return _clean_base(base)
    if origin:
        allowed = {o.strip().rstrip("/") for o in get_settings().cors_origins}
        candidate = origin.strip().rstrip("/")
        if "*" not in allowed and candidate in allowed:
            return _clean_base(candidate)
    return None


def reset_url(base: str, raw_token: str) -> str:
    return f"{base}/reset-password?token={quote(raw_token, safe='')}"


def mask_email(email: str) -> str:
    """a***@g***.com - enough for the owner of the mailbox to recognise it, not to reuse it."""
    local, _, domain = email.partition("@")
    if not domain:
        return (local[:1] or "") + "***"
    host, _, tld = domain.rpartition(".")
    domain_masked = (host[:1] + "***." + tld) if host else (tld[:1] + "***")
    return f"{local[:1]}***@{domain_masked}"


# ---------------------------------------------------------------------------
# Issuing
# ---------------------------------------------------------------------------


def issue_token(
    db: Session,
    user_id: UUID,
    purpose: str,
    ttl: timedelta,
    created_by: Optional[UUID] = None,
    ip: Optional[str] = None,
) -> Tuple[str, datetime]:
    """Create a link for *user_id*; the older unused links of the account stop working. Commits.

    Returns ``(raw_token, expires_at)``: the raw token is shown to nobody but the caller.
    """
    # Serialise issuing per account, so "one live link" holds under concurrent requests.
    db.query(User.id).filter(User.id == user_id).with_for_update().one()
    now = utcnow()
    db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
        .values(expires_at=now)
    )
    raw = new_raw_token()
    expires_at = now + ttl
    db.add(
        PasswordResetToken(
            user_id=user_id,
            token_hash=token_digest(raw),
            purpose=purpose,
            created_by=created_by,
            created_at=now,
            expires_at=expires_at,
            ip=ip[:45] if ip else None,
        )
    )
    db.commit()
    return raw, expires_at


# ---------------------------------------------------------------------------
# Who may hand out a link
# ---------------------------------------------------------------------------


def staff_target_for(db: Session, actor: dict, target_user_id: str) -> Optional[User]:
    """The account *actor* may issue a link for, or None (the caller answers 404 - no leaks).

    *actor* is what ``require_org_admin`` returns (live role and live membership):

    * owner      -> admins and teachers of their OWN organisation (not other owners, not themselves);
    * admin      -> teachers of their own organisation;
    * superadmin -> organisation owners;
    * anybody else never gets here (403 before).

    A user who is an active member of ANOTHER organisation as well is out of reach for the
    organisation-level actors: resetting the password would hand them a login to that other tenant.
    """
    try:
        target_id = UUID(str(target_user_id))
    except (TypeError, ValueError, AttributeError):
        return None
    target = db.query(User).filter(User.id == target_id, User.is_active.is_(True)).first()
    if target is None or target.role == "superadmin":
        return None
    memberships = (
        db.query(OrganizationMember)
        .filter(OrganizationMember.user_id == target.id, OrganizationMember.is_active.is_(True))
        .all()
    )

    if actor.get("role") == "superadmin":
        return target if any(m.org_role == "owner" for m in memberships) else None

    org_id, org_role = str(actor.get("org_id") or ""), actor.get("org_role")
    allowed = _ISSUER_MAY_RESET.get(org_role)
    if not org_id or not allowed or str(target.id) == str(actor.get("user_id")):
        return None
    if not memberships or any(str(m.org_id) != org_id for m in memberships):
        return None
    return target if all(m.org_role in allowed for m in memberships) else None


# ---------------------------------------------------------------------------
# Checking and spending a link
# ---------------------------------------------------------------------------


def find_active_user_by_email(db: Session, email: str) -> Optional[User]:
    """The active account whose login e-mail is *email* (already lower-cased and stripped), else None."""
    return db.query(User).filter(User.email == email, User.is_active.is_(True)).first()


def find_live_token(db: Session, raw_token: str) -> Optional[Tuple[PasswordResetToken, User]]:
    """(token row, its active user) for a link that exists, is unused and not expired; else None."""
    if not looks_like_token(raw_token):
        return None
    row = (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.token_hash == token_digest(raw_token),
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > utcnow(),
        )
        .first()
    )
    if row is None:
        return None
    user = db.query(User).filter(User.id == row.user_id, User.is_active.is_(True)).first()
    return (row, user) if user is not None else None


def spend_token(db: Session, raw_token: str, new_password: str) -> User:
    """Change the password with the link and end every login of the account. Commits.

    Raises InvalidResetLink for any link that cannot be spent. Does not sign the user in.
    """
    if not looks_like_token(raw_token):
        raise InvalidResetLink()
    now = utcnow()
    claimed = db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.token_hash == token_digest(raw_token),
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(PasswordResetToken.user_id)
    ).first()
    if claimed is None:
        db.rollback()
        raise InvalidResetLink()
    user = (
        db.query(User)
        .filter(User.id == claimed[0], User.is_active.is_(True))
        .with_for_update()
        .first()
    )
    if user is None:
        db.rollback()  # the link stays unused; it is of no use to a disabled account anyway
        raise InvalidResetLink()

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    # Every existing login ends: the refresh token is gone, access JWTs issued before now are rejected
    # (see get_current_active_user_payload).
    user.refresh_token_hash = None
    user.sessions_valid_after = now
    db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
        .values(expires_at=now)
    )
    db.commit()
    return user
