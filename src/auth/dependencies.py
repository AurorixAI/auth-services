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

from . import user_sessions
from .cookies import AUTH_COOKIE_NAME, REFRESH_COOKIE_NAME  # noqa: F401
from .service import verify_access_token
from src.core.database import get_db
from src.models.auth import Organization, OrganizationMember, User

# HTTP Bearer scheme
security = HTTPBearer(auto_error=False)

# Cookie names, lifetimes and flags live in cookies.py (re-exported here for the existing imports).


# ============================================================================
# JWT EXTRACTION
# ============================================================================


def _credential_tokens(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials],
) -> List[str]:
    """Credentials of the request in the order they are tried: httpOnly cookie, then Bearer header.

    The cookie outlives the access JWT it carries, so a stale cookie must not shadow a valid
    Bearer header (the client sends the fresh token with the retry after a refresh).
    """
    tokens: List[str] = []
    cookie_token = request.cookies.get(AUTH_COOKIE_NAME)
    if cookie_token:
        tokens.append(cookie_token)
    if credentials and credentials.credentials:
        tokens.append(credentials.credentials)
    return tokens


def get_current_user_payload(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """
    Извлекает и верифицирует JWT.
    Порядок: 1) httpOnly cookie  2) Authorization Bearer header.
    Просроченный/невалидный токен в cookie даёт 401 (клиент обновит сессию через
    /auth/refresh), а не 403/500; валидный Bearer при протухшей cookie принимается.
    """
    tokens = _credential_tokens(request, credentials)
    if not tokens:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется аутентификация",
            headers={"WWW-Authenticate": "Bearer"},
        )

    for token in tokens:
        payload = verify_access_token(token)
        if payload:
            return payload

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Невалидный или истёкший токен",
        headers={"WWW-Authenticate": "Bearer"},
    )


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

    K2-1 (A5): the access token names its session (``sid``) and the session must be alive (not revoked, not expired): a sign-out, a password
    set by a link, a blocked or removed employee end the token AT ONCE, not when its 60 minutes run out. One primary-key read, no cache.
    A token without ``sid`` (issued before this rule) is refused: the client refreshes or signs in again.
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

    try:
        session_id = UUID(str(payload.get("sid")))
    except (TypeError, ValueError, AttributeError):
        session_id = None
    if not user_sessions.is_alive(db, user.id, session_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Сессия завершена. Войдите снова",
        )

    resolved = dict(payload)
    resolved["role"] = user.role

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
            detail="Центр не активен. Обратитесь в ALGO",
        )

    if organisation.blocked_at is not None:  # blocked from the platform console (pack «Консоль-1»)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Доступ центра приостановлен. Свяжитесь с ALGO",
        )

    resolved["org_id"] = str(organisation.id)
    resolved["org_role"] = membership.org_role
    return resolved


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
require_any_auth = RoleChecker(["owner", "admin", "teacher"])


# ─── Деньги (N13 P2) ───


def payments_enabled_for_admin(db: Session, org_id: str) -> bool:
    """``organizations.settings["payments"]["admin_can_manage"]`` is exactly ``true``."""
    try:
        org_uuid = UUID(str(org_id))
    except (TypeError, ValueError):
        return False
    settings = db.query(Organization.settings).filter(Organization.id == org_uuid).scalar()
    block = settings.get("payments") if isinstance(settings, dict) else None
    return isinstance(block, dict) and block.get("admin_can_manage") is True


def ensure_payments_access(user: dict, db: Session) -> dict:
    """Owner: always.  Admin: only if the owner allowed it.  Everybody else: 403."""
    if not user.get("org_id"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет организации")
    org_role = user.get("org_role")
    if org_role == "owner":
        return user
    if org_role == "admin" and payments_enabled_for_admin(db, user["org_id"]):
        return user
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет доступа к оплатам")


def has_payments_access(user: dict, db: Session) -> bool:
    """``ensure_payments_access`` as a yes/no (a caller that only changes what it may say)."""
    try:
        ensure_payments_access(user, db)
    except HTTPException:
        return False
    return True


def require_payments_access(
    user: dict = Depends(require_org_admin), db: Session = Depends(get_db)
) -> dict:
    """Every payment endpoint.  Money is visible nowhere else."""
    return ensure_payments_access(user, db)


def require_payments_settings(user: dict = Depends(require_org_owner)) -> dict:
    """Payment settings: the owner only (and a tenant must exist)."""
    if not user.get("org_id"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет организации")
    return user


# ─── Зарплата (N13 P3) ───


def require_payroll_owner(user: dict = Depends(require_org_owner)) -> dict:
    """Every payroll endpoint.  The owner only - an admin is refused even with the right to payments."""
    if not user.get("org_id"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет организации")
    if user.get("org_role") != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Зарплата доступна только владельцу центра")
    return user
