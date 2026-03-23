"""
ALGO V1 — Security Utilities  (JWT + password hashing)

Extracted from src/auth/service.py — pure functions, no DB dependency.
auth_service.py calls these; core/dependencies.py uses decode_token() for middleware.

Unit tests can import and call these without a running DB or FastAPI app.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from src.core.config import get_settings

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Password hashing ─────────────────────────────────────────────────────────


def hash_password(plain: str) -> str:
    """Return bcrypt hash of plain-text password."""
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if plain matches the stored bcrypt hash."""
    return _pwd_context.verify(plain, hashed)


# ── JWT ───────────────────────────────────────────────────────────────────────


def _base_payload(user_id: str, role: str, extra: dict = None) -> dict:
    payload = {"sub": user_id, "role": role}
    if extra:
        payload.update(extra)
    return payload


def create_access_token(payload: dict) -> str:
    """
    Issue a short-lived access JWT.

    Adds 'exp' (expiry) and 'type': 'access' to the provided payload.
    """
    s = get_settings()
    data = payload.copy()
    data["exp"] = datetime.now(timezone.utc) + timedelta(
        minutes=s.access_token_expire_minutes
    )
    data["type"] = "access"
    return jwt.encode(data, s.secret_key, algorithm=s.algorithm)


def create_refresh_token(payload: dict) -> str:
    """
    Issue a long-lived refresh JWT.

    Adds 'exp' and 'type': 'refresh' to the provided payload.
    """
    s = get_settings()
    data = payload.copy()
    data["exp"] = datetime.now(timezone.utc) + timedelta(
        days=s.refresh_token_expire_days
    )
    data["type"] = "refresh"
    return jwt.encode(data, s.secret_key, algorithm=s.algorithm)


def decode_token(token: str) -> Optional[dict]:
    """
    Decode and validate a JWT.

    Returns the payload dict on success.
    Returns None on ANY error (expired, invalid signature, malformed).
    Never raises — callers must check for None.
    """
    try:
        s = get_settings()
        return jwt.decode(token, s.secret_key, algorithms=[s.algorithm])
    except JWTError:
        return None


def issue_token_pair(user_id: str, role: str, extra: dict = None) -> dict:
    """
    Convenience: returns {'access_token': ..., 'refresh_token': ...}.

    Use this in auth_service.py to issue both tokens in one call.
    """
    base = _base_payload(user_id, role, extra)
    return {
        "access_token": create_access_token(base),
        "refresh_token": create_refresh_token(base),
        "token_type": "bearer",
    }
