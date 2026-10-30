"""Sign-in to the console: password check, the lock after 5 failures (15 min, by e-mail AND by address), sessions, password change.

* No second factor yet (``platform_staff.totp_enabled`` is kept for it, always false).
* An unknown e-mail, a wrong password and a deactivated account look the same, cost the same (bcrypt against a dummy hash) and
  are counted the same, so neither the answer nor the lock says whether the address is a staff member.
* The lock: the 5th failure inside 15 minutes locks, for 15 minutes from that moment, (1) that e-mail from THAT address (the primary
  key), (2) that address for any e-mail, (3) that e-mail from anywhere - except from an address the person has signed in from
  during the last 30 days (so a stranger's guesses cannot lock the owner out of his own office). While it lasts even the right
  password is refused (429 + Retry-After). A staff member's lock is also written to ``platform_staff.locked_until`` and his failures are
  counted in the same 15-minute window as everybody's (``failed_at``).
* A row for every sign-in attempt that was checked (success, failure, forbidden) and ONE ``console.login.locked`` row when a lock starts;
  an attempt refused because of a lock writes nothing. The password is never written anywhere; a typed login that is not an e-mail
  address is journaled as «[не почта]» and its length only.
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
from . import password_rules
from . import platform_permissions as perms
from .platform_common import client_meta, iso, utcnow
from .platform_errors import PlatformError, validation_error
from .platform_security import SESSION_HOURS, Principal, issue_token
from .platform_validation import EMAIL_RE

logger = logging.getLogger(__name__)

LOCK_FAILURES = 5
LOCK_SECONDS = 15 * 60
TRUSTED_IP_DAYS = 30
MIN_PASSWORD_LENGTH = 12
LOCKED_TEXT = "Слишком много попыток входа. Повторите через 15 минут."
INVALID_TEXT = "Неверная почта или пароль"
_DUMMY_HASH = hash_password("console-dummy-password-for-timing")  # built at import: the first sign-in costs the same as every other


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]  # no e-mail / address in a Redis key


def _keys(email: str, bucket: str) -> dict:
    mail, where = _digest(email), _digest(bucket)
    return {
        "ip_fail": f"rl:console_fail_ip:{bucket}",
        "ip_lock": f"rl:console_lock_ip:{bucket}",
        "mail_fail": f"rl:console_fail_mail:{mail}",
        "mail_lock": f"rl:console_lock_mail:{mail}",
        "pair_fail": f"rl:console_fail_pair:{mail}:{where}",
        "pair_lock": f"rl:console_lock_pair:{mail}:{where}",
    }


def _forget(key: str) -> None:
    rate_limit._window_store.pop(key, None)
    r = redis_client.get_redis()
    if r is not None:
        try:
            r.delete(key)
        except Exception as exc:  # noqa: BLE001 - a counter store must never break a sign-in
            redis_client.mark_down(exc)


def unlock_email(email: str) -> None:
    """Forget every failure count and lock of an e-mail address, from any address (the owner's reset script)."""
    mail = _digest((email or "").strip().lower())
    for prefix in ("rl:console_fail_mail:", "rl:console_lock_mail:"):
        _forget(prefix + mail)
    for prefix in ("rl:console_fail_pair:", "rl:console_lock_pair:"):
        for key in [k for k in list(rate_limit._window_store) if k.startswith(f"{prefix}{mail}:")]:
            rate_limit._window_store.pop(key, None)
        r = redis_client.get_redis()
        if r is not None:
            try:
                for key in r.scan_iter(match=f"{prefix}{mail}:*"):
                    r.delete(key)
            except Exception as exc:  # noqa: BLE001
                redis_client.mark_down(exc)


def _lock_left(key: str) -> int:
    count, left = peek_window(key)
    return left if count > 0 else 0


def verify_password_safely(plain: str, hashed: str) -> bool:
    try:
        return verify_password(plain, hashed)
    except ValueError:  # e.g. a NUL character: bcrypt refuses it; never an error page, just "wrong"
        return False


def password_problem(password: object, *, min_length: int, email: str = "") -> Optional[str]:
    """None when acceptable, else the Russian reason (one rule for every flow that sets a password: ``password_rules``, K2-2 F)."""
    return password_rules.problem(password, min_length=min_length, email=email)


def validate_new_password(password: object, email: str = "") -> Optional[str]:
    """The console's rule: at least 12 characters (and the common limits above)."""
    return password_problem(password, min_length=MIN_PASSWORD_LENGTH, email=email)


def _locked_error(left: int) -> PlatformError:
    return PlatformError(429, LOCKED_TEXT, "login_locked", retry_after=max(int(left), 1))


def _email_detail(email: str) -> dict:
    """What the journal keeps of the typed login: the address when it is one, else only its length (people paste passwords into the login box)."""
    if EMAIL_RE.match(email or "") and len(email) <= 255:
        return {"email": email}
    return {"email": "[не почта]", "email_length": len(email or "")}


def _log(db: Session, request: Request, action: str, user: Optional[User], email: str, extra: Optional[dict] = None) -> None:
    details = _email_detail(email)
    if extra:
        details.update(extra)
    audit.record(
        db, actor_id=user.id if user else None, action=action, target_type="console",
        target_id=user.id if user else None, details=details, request=request,
    )
    db.commit()  # the caller may be about to raise (the request transaction would roll back)


def _trusted_address(db: Session, user: Optional[User], ip: Optional[str]) -> bool:
    """An address this person has signed in to the console from within the last 30 days (his own office / phone)."""
    if user is None or not ip:
        return False
    since = utcnow() - timedelta(days=TRUSTED_IP_DAYS)
    return (
        db.query(PlatformSession.id).filter(PlatformSession.user_id == user.id, PlatformSession.ip == ip, PlatformSession.created_at >= since).first()
        is not None
    )


def login(db: Session, request: Request, email_raw: str, password: str) -> tuple[Principal, str]:
    email = (email_raw or "").strip().lower()
    bucket = client_bucket(request)
    keys = _keys(email, bucket)
    now = utcnow()
    ip, _agent = client_meta(request)

    user = db.query(User).filter(func.lower(User.email) == email).first() if email else None
    staff = db.query(PlatformStaff).filter(PlatformStaff.user_id == user.id).first() if user else None

    # Locks, from the narrowest: this e-mail from this address (primary), this address, this e-mail from anywhere.  The last one is the
    # backstop against a spread-out guess; it does not shut a person out of his own known address (an attacker could otherwise lock the
    # owner out of the console from anywhere), while the first two still apply to that address.
    left = max(_lock_left(keys["pair_lock"]), _lock_left(keys["ip_lock"]))
    email_left = _lock_left(keys["mail_lock"])
    if staff is not None and staff.locked_until is not None and staff.locked_until > now:
        email_left = max(email_left, int((staff.locked_until - now).total_seconds()) + 1)
    if email_left > 0 and not _trusted_address(db, user, ip):
        left = max(left, email_left)
    if left > 0:
        # no row per refused attempt: the lock was journaled once, when it started
        logger.info("console sign-in refused: locked for %s more seconds", left)
        raise _locked_error(left)

    password_ok = verify_password_safely(password or "", user.password_hash if user else _DUMMY_HASH)
    if user is None or not password_ok or not user.is_active:
        scopes = _register_failure(staff, keys, now)
        _log(db, request, "console.login.failed", user, email)
        if scopes:
            _log(db, request, "console.login.locked", user, email, {"scopes": scopes, "minutes": LOCK_SECONDS // 60})
            raise _locked_error(LOCK_SECONDS)
        raise PlatformError(401, INVALID_TEXT, "invalid_credentials")

    if staff is None:
        # K2-2 (F): a person of a cabinet who types HIS password here must not learn that it was right (an oracle with a budget of its own): the standard
        # refusal, counted as a failure; only the journal knows the difference
        scopes = _register_failure(staff, keys, now)
        _log(db, request, "console.login.forbidden", user, email, {"why": "not_staff"})
        if scopes:
            _log(db, request, "console.login.locked", user, email, {"scopes": scopes, "minutes": LOCK_SECONDS // 60})
            raise _locked_error(LOCK_SECONDS)
        raise PlatformError(401, INVALID_TEXT, "invalid_credentials")
    if not staff.is_active:
        _log(db, request, "console.login.forbidden", user, email, {"why": "staff_inactive"})
        raise PlatformError(403, "Доступ к консоли отключён", "staff_inactive")

    _forget(keys["mail_fail"])
    _forget(keys["pair_fail"])
    staff.failed_logins = 0
    staff.failed_at = None
    staff.locked_until = None
    staff.last_login_at = now
    session = PlatformSession(user_id=user.id, created_at=now, expires_at=now + timedelta(hours=SESSION_HOURS), ip=ip, user_agent=_agent)
    db.add(session)
    db.flush()
    _sweep_sessions(db, now)
    audit.record(db, actor_id=user.id, action="console.login.success", target_type="console", target_id=user.id, details={"email": email}, request=request)
    db.commit()
    token = issue_token(user.id, session.id, session.expires_at)
    role = db.get(PlatformRole, staff.role_id)
    principal = Principal(user, staff, role, session, perms.effective_permissions(staff.is_owner, role.permissions if role else ()))
    return principal, token


def _register_failure(staff: Optional[PlatformStaff], keys: dict, now) -> list:
    """Count one failure; the list of the scopes (``pair`` / ``ip`` / ``email``) that this failure locked, empty when none."""
    locked = []
    for fail, lock, scope in (("pair_fail", "pair_lock", "pair"), ("ip_fail", "ip_lock", "ip"), ("mail_fail", "mail_lock", "email")):
        count, _ = hit_window(keys[fail], LOCK_SECONDS)
        if count == LOCK_FAILURES:
            hit_window(keys[lock], LOCK_SECONDS)
            locked.append(scope)
    if staff is not None:
        # the same 15-minute window as the counters above, so a staff member and an unknown address behave alike
        if staff.failed_at is None or now - staff.failed_at > timedelta(seconds=LOCK_SECONDS):
            staff.failed_logins = 0
        staff.failed_logins = (staff.failed_logins or 0) + 1
        staff.failed_at = now
        if staff.failed_logins >= LOCK_FAILURES:
            staff.failed_logins = 0
            staff.locked_until = now + timedelta(seconds=LOCK_SECONDS)
            if "email" not in locked:
                locked.append("email")
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
        audit.record(db, actor_id=p.user.id, action="console.password.change_failed", target_type="staff", target_id=p.user.id, request=request)
        db.commit()  # the request is about to fail: the row must survive the rollback
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
