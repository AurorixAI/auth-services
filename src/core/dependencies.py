"""
ALGO V1 — Shared FastAPI Dependencies

Centralises get_db, JWT extraction (cookie → Bearer fallback), and RBAC.
Replaces src/auth/dependencies.py as the canonical location.

Annotated type aliases let route handlers declare deps cleanly:

    @router.get("/me")
    def get_me(db: DbDep, user: CurrentUser):
        ...

    @router.get("/admin-only")
    def admin_view(user: CurrentUser, _: None = Depends(require_roles("admin", "owner"))):
        ...
"""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.core.security import decode_token

_http_bearer = HTTPBearer(auto_error=False)

# ── Database ──────────────────────────────────────────────────────────────────

DbDep = Annotated[Session, Depends(get_db)]

# ── JWT extraction ────────────────────────────────────────────────────────────


def _extract_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_http_bearer),
) -> Optional[str]:
    """Try cookie first (httpOnly), then Authorization header."""
    token = request.cookies.get("algo_auth_token")
    if not token and credentials:
        token = credentials.credentials
    return token or None


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_http_bearer),
) -> dict:
    """
    Resolve JWT payload.  Raises HTTP 401 on any failure.

    Token resolution order:
      1. Cookie: algo_auth_token
      2. Authorization: Bearer <token>
    """
    token = _extract_token(request, credentials)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    payload = decode_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    return payload


def get_optional_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_http_bearer),
) -> Optional[dict]:
    """Same as get_current_user but returns None instead of raising for unauthenticated requests."""
    try:
        return get_current_user(request, credentials)
    except HTTPException:
        return None


# ── RBAC ──────────────────────────────────────────────────────────────────────


def require_roles(*allowed_roles: str):
    """
    Factory that returns a FastAPI dependency enforcing role membership.

    Usage:
        @router.get("/admin", dependencies=[Depends(require_roles("admin", "owner"))])
        def admin_only(): ...
    """

    def _check(user: dict = Depends(get_current_user)) -> dict:
        role = user.get("role", "")
        if role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{role}' not permitted. Required: {list(allowed_roles)}",
            )
        return user

    return _check


# ── Annotated type aliases ────────────────────────────────────────────────────

CurrentUser = Annotated[dict, Depends(get_current_user)]
OptionalUser = Annotated[Optional[dict], Depends(get_optional_user)]

# ── Backward compatibility aliases for Routers ────────────────────────────────

require_org_owner = require_roles("owner")
require_org_admin = require_roles("owner", "admin")
require_org_teacher = require_roles("owner", "admin", "teacher")
require_org_member = require_roles("owner", "admin", "teacher")
require_superadmin = require_roles("superadmin")


def require_any_auth(user: dict = Depends(get_current_user)) -> dict:
    return user
