"""One-time «Задать пароль» / «Сброс пароля» links.

* ``centre_invite`` (72 h): the owner of an approved centre sets his first password. ``reset`` (1 h): a forgotten password, started by the person
  himself («Забыли пароль»), by the leadership of his centre or by the team.
* The token is 43 urlsafe characters; only its SHA-256 is stored. A link works once. A NEW ``reset`` link revokes the live ``reset`` links of the user
  ONLY (a stranger who types his address cannot kill the welcome link of a new owner: K2-2, B); a NEW ``centre_invite`` revokes every live link.
* ``lookup`` classifies a token: ok / used / revoked / expired / missing. The consuming route takes the row ``FOR UPDATE``, so two
  clicks at once set the password once. A ``reset`` link has a grace of 5 minutes for the POST (the page was open when the hour ran out).
* Lock order (no deadlock between an issue, a consume, a sign-in and a confirmation of a new e-mail): the per-user session lock (``user_sessions.lock_user``)
  FIRST, then the link rows, then the user row. A sign-in or a password change that revokes the live ``reset`` links takes only the rows nobody holds
  (``SKIP LOCKED``).
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from src.models.platform import PasswordLink

from . import user_sessions
from .platform_common import utcnow

TTL = {"centre_invite": timedelta(hours=72), "reset": timedelta(hours=1)}
EXPIRES_PHRASE = {"centre_invite": "72 часа", "reset": "1 час"}
RESET_GRACE = timedelta(minutes=5)  # the POST of a ``reset`` link may come this long after its expiry (the page was open when the hour ran out)

OK, USED, REVOKED, EXPIRED, MISSING = "ok", "used", "revoked", "expired", "missing"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _live(db: Session, user_id, purpose: Optional[str] = None):
    q = db.query(PasswordLink).filter(PasswordLink.user_id == user_id, PasswordLink.used_at.is_(None), PasswordLink.revoked_at.is_(None))
    return q.filter(PasswordLink.purpose == purpose) if purpose else q


def issue(db: Session, user_id, purpose: str, created_by=None) -> Tuple[str, PasswordLink]:
    """A new link. A ``reset`` revokes the live ``reset`` links of the user (his invitation / welcome link lives on); a ``centre_invite`` revokes every
    live link. Not committed. Returns (the token to put in the letter, the row). Invariant: after N parallel issues exactly ONE live link of the purpose."""
    if purpose not in TTL:
        raise ValueError(purpose)
    now = utcnow()
    revoking = "reset" if purpose == "reset" else None
    # the per-user lock serialises the issuers and the POST that consumes a link; the UPDATE after the lock is a NEW statement, so it sees the link the
    # issuer before us has just committed
    user_sessions.lock_user(db, user_id)
    _live(db, user_id, revoking).update({"revoked_at": now}, synchronize_session=False)
    raw = secrets.token_urlsafe(32)
    link = PasswordLink(
        user_id=user_id, purpose=purpose, token_hash=hash_token(raw), expires_at=now + TTL[purpose], created_by=created_by,
        created_at=now,
    )
    db.add(link)
    db.flush()
    return raw, link


def classify(link: Optional[PasswordLink], *, grace: bool = False) -> str:
    """``grace=True`` (the POST that sets the password): a ``reset`` link lives ``RESET_GRACE`` longer than the time the page shows."""
    if link is None:
        return MISSING
    if link.used_at is not None:
        return USED
    if link.revoked_at is not None:
        return REVOKED
    limit = link.expires_at + (RESET_GRACE if grace and link.purpose == "reset" else timedelta(0))
    if limit <= utcnow():
        return EXPIRED
    return OK


def lookup(db: Session, raw: str, *, for_update: bool = False, grace: bool = False) -> Tuple[str, Optional[PasswordLink]]:
    if not raw or len(raw) > 200:
        return MISSING, None
    q = db.query(PasswordLink).filter(PasswordLink.token_hash == hash_token(raw))
    if for_update:
        q = q.with_for_update().populate_existing()
    link = q.first()
    return classify(link, grace=grace), link


def owner_of(db: Session, raw: str):
    """The user a token belongs to (or None), read WITHOUT loading the link: the consuming route takes the per-user lock before it locks the row."""
    if not raw or len(raw) > 200:
        return None
    return db.query(PasswordLink.user_id).filter(PasswordLink.token_hash == hash_token(raw)).scalar()


def revoke_live_resets(db: Session, user_id) -> int:
    """A sign-in with the password, or a password changed in the settings, ends the live ``reset`` links of the user (he remembered it, or set a new one).
    Rows that another transaction holds (a POST that is consuming one right now) are skipped: that POST ends them itself. Not committed."""
    ids = [
        row_id for (row_id,) in _live(db, user_id, "reset").with_entities(PasswordLink.id).with_for_update(skip_locked=True).all()
    ]
    if not ids:
        return 0
    return db.query(PasswordLink).filter(PasswordLink.id.in_(ids)).update({"revoked_at": utcnow()}, synchronize_session=False)


def live_link(db: Session, user_id) -> Optional[PasswordLink]:
    link = (
        db.query(PasswordLink)
        .filter(PasswordLink.user_id == user_id, PasswordLink.used_at.is_(None), PasswordLink.revoked_at.is_(None))
        .order_by(PasswordLink.created_at.desc())
        .first()
    )
    return link if link is not None and link.expires_at > utcnow() else None
