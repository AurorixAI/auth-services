"""One-time «Задать пароль» / «Сброс пароля» links.

* ``centre_invite`` (72 h): the owner of an approved centre sets his first password. ``reset`` (1 h): a forgotten password, started by the team.
* The token is 43 urlsafe characters; only its SHA-256 is stored. A link works once; a NEW link of a user revokes every live link of his.
* ``lookup`` classifies a token: ok / used / revoked / expired / missing. The consuming route takes the row ``FOR UPDATE``, so two
  clicks at once set the password once.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from src.models.platform import PasswordLink

from .platform_common import utcnow

TTL = {"centre_invite": timedelta(hours=72), "reset": timedelta(hours=1)}
EXPIRES_PHRASE = {"centre_invite": "72 часа", "reset": "1 час"}

OK, USED, REVOKED, EXPIRED, MISSING = "ok", "used", "revoked", "expired", "missing"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue(db: Session, user_id, purpose: str, created_by=None) -> Tuple[str, PasswordLink]:
    """A new link; every live link of the user is revoked. Not committed. Returns (the token to put in the letter, the row)."""
    if purpose not in TTL:
        raise ValueError(purpose)
    now = utcnow()
    db.query(PasswordLink).filter(
        PasswordLink.user_id == user_id, PasswordLink.used_at.is_(None), PasswordLink.revoked_at.is_(None)
    ).update({"revoked_at": now}, synchronize_session=False)
    raw = secrets.token_urlsafe(32)
    link = PasswordLink(
        user_id=user_id, purpose=purpose, token_hash=hash_token(raw), expires_at=now + TTL[purpose], created_by=created_by,
        created_at=now,
    )
    db.add(link)
    db.flush()
    return raw, link


def classify(link: Optional[PasswordLink]) -> str:
    if link is None:
        return MISSING
    if link.used_at is not None:
        return USED
    if link.revoked_at is not None:
        return REVOKED
    if link.expires_at <= utcnow():
        return EXPIRED
    return OK


def lookup(db: Session, raw: str, *, for_update: bool = False) -> Tuple[str, Optional[PasswordLink]]:
    if not raw or len(raw) > 200:
        return MISSING, None
    q = db.query(PasswordLink).filter(PasswordLink.token_hash == hash_token(raw))
    if for_update:
        q = q.with_for_update()
    link = q.first()
    return classify(link), link


def live_link(db: Session, user_id) -> Optional[PasswordLink]:
    link = (
        db.query(PasswordLink)
        .filter(PasswordLink.user_id == user_id, PasswordLink.used_at.is_(None), PasswordLink.revoked_at.is_(None))
        .order_by(PasswordLink.created_at.desc())
        .first()
    )
    return link if link is not None and link.expires_at > utcnow() else None
