"""
ALGO V1 - Auth Dependencies
FastAPI dependencies: JWT extraction, RBAC, org-level permissions
B2B: owner / admin / teacher — students have NO JWT
"""

from typing import Optional, List

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .service import verify_access_token

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

    def __call__(self, payload: dict = Depends(get_current_user_payload)) -> dict:
        role = payload.get("role")
        org_role = payload.get("org_role")

        # Superadmin проходит всегда
        if role == "superadmin":
            return self._extract(payload)

        # Проверка по role или org_role
        if role not in self.allowed_roles and org_role not in self.allowed_roles:
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
