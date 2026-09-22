"""
ALGO V1 - Auth Dependencies
FastAPI dependencies: JWT extraction, RBAC, org-level permissions
B2B: owner / admin / teacher — students have NO JWT
"""

from typing import Optional, List
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from .service import verify_access_token
from src.core.database import get_db
from src.models.auth import Organization, OrganizationMember, User

# HTTP Bearer scheme
security = HTTPBearer(auto_error=False)

# Cookie name for httpOnly JWT
AUTH_COOKIE_NAME = "algo_auth_token"
REFRESH_COOKIE_NAME = "algo_refresh_token"


# ============================================================================
# JWT EXTRACTION
# ============================================================================


def get_current_user_payload(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """
    Извлекает и верифицирует JWT.
    Порядок: 1) httpOnly cookie  2) Authorization Bearer header.
    """
    token: Optional[str] = None

    # 1. Try httpOnly cookie first
    cookie_token = request.cookies.get(AUTH_COOKIE_NAME)
    if cookie_token:
        token = cookie_token

    # 2. Fallback to Authorization header
    if not token and credentials:
        token = credentials.credentials

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется аутентификация",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Невалидный или истёкший токен",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return payload


def get_current_active_user_payload(
    payload: dict = Depends(get_current_user_payload),
    db: Session = Depends(get_db),
) -> dict:
    """Resolve a JWT against the current account and organisation state.

    Access tokens carry identity, while role and membership are read from the
    database for protected requests. Removing a member, suspending an
    organisation, or changing a role therefore takes effect immediately
    rather than waiting for token expiry. Downstream services use ``/auth/me``
    as their authority check, making this the single source of truth.
    """
    try:
        user_id = UUID(str(payload.get("sub")))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Невалидный идентификатор пользователя",
        ) from exc

    user = (
        db.query(User)
        .filter(User.id == user_id, User.is_active.is_(True))
        .first()
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Учётная запись отключена или не найдена",
        )

    resolved = dict(payload)
    resolved["role"] = user.role

    # Platform superadmins do not belong to a tenant organisation. Their
    # current database role is still verified above; tenant RBAC below stays
    # membership-based and cannot be bypassed by a stale JWT claim.
    if user.role == "superadmin":
        resolved.pop("org_id", None)
        resolved.pop("org_role", None)
        return resolved

    try:
        org_id = UUID(str(payload.get("org_id")))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Пользователь не привязан к активной организации",
        ) from exc

    membership = (
        db.query(OrganizationMember)
        .filter(
            OrganizationMember.org_id == org_id,
            OrganizationMember.user_id == user.id,
            OrganizationMember.is_active.is_(True),
        )
        .first()
    )
    if not membership:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Доступ к организации отозван",
        )

    organisation = (
        db.query(Organization)
        .filter(Organization.id == org_id, Organization.status == "active")
        .first()
    )
    if not organisation:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Организация недоступна",
        )

    resolved["org_id"] = str(organisation.id)
    resolved["org_role"] = membership.org_role
    return resolved


def get_optional_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[dict]:
    """Опциональная аутентификация — не падает если токена нет"""
    token: Optional[str] = None

    cookie_token = request.cookies.get(AUTH_COOKIE_NAME)
    if cookie_token:
        token = cookie_token
    elif credentials:
        token = credentials.credentials

    if not token:
        return None
    payload = verify_access_token(token)
    if not payload:
        return None
    return payload


# ============================================================================
# RBAC — Role-Based Access Control
# ============================================================================


class RoleChecker:
    """
    Проверка роли пользователя из JWT.

    Использование:
        @router.get("/admin/...", dependencies=[Depends(require_org_admin)])
    Или:
        def endpoint(user=Depends(require_org_admin)):
    """

    def __init__(self, allowed_roles: List[str], check_org: bool = False):
        self.allowed_roles = allowed_roles
        self.check_org = check_org

    def __call__(self, payload: dict = Depends(get_current_active_user_payload)) -> dict:
        role = payload.get("role")
        org_role = payload.get("org_role")

        # Superadmin keeps its established platform-wide access semantics,
        # but only after ``get_current_active_user_payload`` has checked that
        # the account is currently active in the database.
        if role == "superadmin":
            return self._extract(payload)

        # Tenant permissions come solely from the fresh membership role.
        # ``users.role`` is a legacy/global convenience field and must not
        # grant administrator access in another organisation.
        effective_role = org_role if self.check_org else (org_role or role)
        if effective_role not in self.allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Недостаточно прав. Требуется: {', '.join(self.allowed_roles)}",
            )

        # Если check_org — должен быть org_id
        if self.check_org and not payload.get("org_id"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Пользователь не привязан к организации",
            )

        return self._extract(payload)

    def _extract(self, payload: dict) -> dict:
        return {
            "user_id": payload.get("sub"),
            "role": payload.get("role"),
            "org_id": payload.get("org_id"),
            "org_role": payload.get("org_role"),
        }


# ─── Готовые чекеры (org-level) ───

# Владелец организации
require_org_owner = RoleChecker(["owner"], check_org=True)

# Администратор (owner + admin)
require_org_admin = RoleChecker(["owner", "admin"], check_org=True)

# Учитель и выше
require_org_teacher = RoleChecker(["owner", "admin", "teacher"], check_org=True)

# Любой аутентифицированный с org
require_org_member = RoleChecker(["owner", "admin", "teacher"], check_org=True)

# Любой аутентифицированный (без проверки org)
require_any_auth = RoleChecker(["owner", "admin", "teacher", "superadmin"])

# Superadmin
require_superadmin = RoleChecker(["superadmin"])
