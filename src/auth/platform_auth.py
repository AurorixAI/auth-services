"""Sign-in to the console: password check, the lock after 5 failures (15 min, by e-mail AND by address), sessions, password change.

* No second factor yet (``platform_staff.totp_enabled`` is kept for it, always false).
* An unknown e-mail, a wrong password and a deactivated account look the same, cost the same (bcrypt against a dummy hash) and
  are counted the same, so neither the answer nor the lock says whether the address is a staff member.
* The lock: the 5th failure inside 15 minutes (counted per e-mail and per client address) locks that e-mail / that address for 15
  minutes from that moment; while it lasts even the right password is refused (429 + Retry-After). A staff member's lock is also
  written to ``platform_staff.locked_until`` (it survives a restart and is shown to the owner).
* Every attempt (success, failure, locked, forbidden) is a ``platform_audit`` row; the password is never written anywhere.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from typing import Optional

from fastapi import Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.api import rate_limit
from src.api.rate_limit import client_bucket, hit_window, peek_window
from src.core import redis_client
from src.core.security import hash_password, verify_password
from src.models.auth import User
from src.models.platform import PlatformRole, PlatformSession, PlatformStaff

from . import platform_audit as audit
from . import platform_permissions as perms
from .platform_common import client_meta, iso, utcnow
from .platform_errors import PlatformError, validation_error
from .platform_security import Principal, issue_token, session_expiry

logger = logging.getLogger(__name__)

LOCK_FAILURES = 5
LOCK_SECONDS = 15 * 60
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128
LOCKED_TEXT = "Слишком много попыток входа. Повторите через 15 минут."
INVALID_TEXT = "Неверная почта или пароль"
_dummy_hash: Optional[str] = None


def _dummy() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("console-dummy-password-for-timing")
    return _dummy_hash


def _digest(email: str) -> str:
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:32]  # no e-mail in a Redis key


def _keys(email: str, bucket: str) -> dict:
    return {
        "ip_fail": f"rl:console_fail_ip:{bucket}",
        "ip_lock": f"rl:console_lock_ip:{bucket}",
        "mail_fail": f"rl:console_fail_mail:{_digest(email)}",
        "mail_lock": f"rl:console_lock_mail:{_digest(email)}",
    }


def _forget(key: str) -> None:
    rate_limit._window_store.pop(key, None)
    r = redis_client.get_redis()
    if r is not None:
        try:
            r.delete(key)
        except Exception as exc:  # noqa: BLE001 - a counter store must never break a sign-in
            redis_client.mark_down(exc)


def _lock_left(key: str) -> int:
    count, left = peek_window(key)
    return left if count > 0 else 0


def verify_password_safely(plain: str, hashed: str) -> bool:
    try:
        return verify_password(plain, hashed)
    except ValueError:  # e.g. a NUL character: bcrypt refuses it; never an error page, just "wrong"
        return False


def validate_new_password(password: object, email: str = "") -> Optional[str]:
    """None when acceptable, else the Russian reason."""
    if not isinstance(password, str) or not password:
        return "Укажите пароль"
    if "\x00" in password:
        return "Пароль содержит недопустимые символы"
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Пароль не короче {MIN_PASSWORD_LENGTH} символов"
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Пароль не длиннее {MAX_PASSWORD_LENGTH} символов"
    if email and password.strip().lower() == email.strip().lower():
        return "Пароль не должен совпадать с почтой"
    return None


def _locked_error(left: int) -> PlatformError:
    return PlatformError(429, LOCKED_TEXT, "login_locked", retry_after=max(int(left), 1))


def _log(db: Session, request: Request, action: str, user: Optional[User], email: str, extra: Optional[dict] = None) -> None:
    details = {"email": email[:255]}
    if extra:
        details.update(extra)
    audit.record(
        db, actor_id=user.id if user else None, action=action, target_type="console",
        target_id=user.id if user else None, details=details, request=request,
    )
    db.commit()  # the caller may be about to raise (the request transaction would roll back)


def login(db: Session, request: Request, email_raw: str, password: str) -> tuple[Principal, str]:
    email = (email_raw or "").strip().lower()
    bucket = client_bucket(request)
    keys = _keys(email, bucket)
    now = utcnow()

    user = db.query(User).filter(func.lower(User.email) == email).first() if email else None
    staff = db.query(PlatformStaff).filter(PlatformStaff.user_id == user.id).first() if user else None

    left = max(_lock_left(keys["ip_lock"]), _lock_left(keys["mail_lock"]))
    if staff is not None and staff.locked_until is not None and staff.locked_until > now:
        left = max(left, int((staff.locked_until - now).total_seconds()) + 1)
    if left > 0:
        _log(db, request, "console.login.locked", user, email, {"retry_after": left})
        raise _locked_error(left)

    password_ok = verify_password_safely(password or "", user.password_hash if user else _dummy())
    if user is None or not password_ok or not user.is_active:
        locked = _register_failure(db, staff, keys)
        _log(db, request, "console.login.failed", user, email)
        if locked:
            raise _locked_error(LOCK_SECONDS)
        raise PlatformError(401, INVALID_TEXT, "invalid_credentials")

    if staff is None:
        _log(db, request, "console.login.forbidden", user, email, {"why": "not_staff"})
        raise PlatformError(403, "Нет доступа к консоли", "not_staff")
    if not staff.is_active:
        _log(db, request, "console.login.forbidden", user, email, {"why": "staff_inactive"})
        raise PlatformError(403, "Доступ к консоли отключён", "staff_inactive")

    _forget(keys["mail_fail"])
    staff.failed_logins = 0
    staff.locked_until = None
    staff.last_login_at = now
    ip, agent = client_meta(request)
    session = PlatformSession(user_id=user.id, created_at=now, expires_at=session_expiry(), ip=ip, user_agent=agent)
    db.add(session)
    db.flush()
    _sweep_sessions(db, now)
    audit.record(db, actor_id=user.id, action="console.login.success", target_type="console", target_id=user.id, details={"email": email}, request=request)
    db.commit()
    token = issue_token(user.id, session.id, session.expires_at)
    role = db.get(PlatformRole, staff.role_id)
    principal = Principal(user, staff, role, session, perms.effective_permissions(staff.is_owner, role.permissions if role else ()))
    return principal, token


def _register_failure(db: Session, staff: Optional[PlatformStaff], keys: dict) -> bool:
    """Count one failure; True when this one locked the address / the e-mail / the account."""
    locked = False
    for fail, lock in (("ip_fail", "ip_lock"), ("mail_fail", "mail_lock")):
        count, _ = hit_window(keys[fail], LOCK_SECONDS)
        if count == LOCK_FAILURES:
            hit_window(keys[lock], LOCK_SECONDS)
            locked = True
    if staff is not None:
        staff.failed_logins = (staff.failed_logins or 0) + 1
        if staff.failed_logins >= LOCK_FAILURES:
            staff.failed_logins = 0
            staff.locked_until = utcnow() + timedelta(seconds=LOCK_SECONDS)
            locked = True
    return locked


def _sweep_sessions(db: Session, now) -> None:
    """Delete (a bounded batch of) sessions that ended more than 30 days ago, with their address and user agent."""
    old = (
        db.query(PlatformSession.id)
        .filter(PlatformSession.expires_at < now - timedelta(days=30))
        .limit(100)
        .all()
    )
    if old:
        db.query(PlatformSession).filter(PlatformSession.id.in_([r.id for r in old])).delete(synchronize_session=False)


def revoke_sessions(db: Session, user_id, reason: str, *, except_id=None) -> int:
    q = db.query(PlatformSession).filter(PlatformSession.user_id == user_id, PlatformSession.revoked_at.is_(None))
    if except_id is not None:
        q = q.filter(PlatformSession.id != except_id)
    now = utcnow()
    return q.update({"revoked_at": now, "revoke_reason": reason}, synchronize_session=False)


def me_payload(p: Principal) -> dict:
    return {
        "user": {
            "id": str(p.user.id), "email": p.user.email, "first_name": p.user.first_name, "last_name": p.user.last_name, "name": p.name,
        },
        "role": {"id": str(p.role.id), "name": p.role.name} if p.role else None,
        "is_owner": bool(p.staff.is_owner),
        "permissions": sorted(p.permissions, key=perms.ALL_KEYS.index),
        "must_change_password": bool(p.staff.must_change_password),
        "totp_enabled": bool(p.staff.totp_enabled),
        "session": {"expires_at": iso(p.session.expires_at)},
    }


def change_password(db: Session, request: Request, p: Principal, current: str, new: str) -> None:
    count, left = hit_window(f"rl:console_pw_change:{p.user.id}", LOCK_SECONDS)
    if count > 10:
        raise PlatformError(429, "Слишком много попыток. Повторите позже.", "rate_limited", retry_after=left)
    if not verify_password_safely(current or "", p.user.password_hash):
        raise PlatformError(403, "Текущий пароль неверный", "wrong_password")
    reason = validate_new_password(new, p.user.email)
    if reason is None and new == current:
        reason = "Новый пароль должен отличаться от текущего"
    if reason:
        raise validation_error({"new_password": reason})
    p.user.password_hash = hash_password(new)
    p.staff.must_change_password = False
    revoke_sessions(db, p.user.id, "password_changed", except_id=p.session.id)
    audit.record(db, actor_id=p.user.id, action="console.password.changed", target_type="staff", target_id=p.user.id, request=request)
    db.commit()
