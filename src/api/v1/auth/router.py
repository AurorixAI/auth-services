"""
ALGO V1 — Auth API Router  (thin handler layer)

Each handler: validate input → call service → return schema.
Zero direct DB access. Zero business logic.
"""

from fastapi import APIRouter, Depends, Response

from src.core.dependencies import CurrentUser, DbDep, require_roles
from src.repositories.org_repo import InvitationRepo, OrgRepo
from src.repositories.user_repo import OrgMemberRepo, UserRepo
from src.services.auth_service import AuthService
from src.services.org_service import OrgService

from .schemas import (
    AcceptInvitationRequest,
    ApplyOrgRequest,
    LoginRequest,
    LoginResponse,
    OrgResponse,
    UserResponse,
)

router = APIRouter(prefix="/auth", tags=["Auth"])


# ── Dependency factories ───────────────────────────────────────────────────


def _auth_service(db: DbDep) -> AuthService:
    return AuthService(
        user_repo=UserRepo(db),
        org_member_repo=OrgMemberRepo(db),
    )


def _org_service(db: DbDep) -> OrgService:
    return OrgService(
        org_repo=OrgRepo(db),
        user_repo=UserRepo(db),
        org_member_repo=OrgMemberRepo(db),
        invitation_repo=InvitationRepo(db),
    )


# ── Auth endpoints ─────────────────────────────────────────────────────────


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    response: Response,
    service: AuthService = Depends(_auth_service),
):
    result = service.login(payload.email, payload.password)
    response.set_cookie(
        key="algo_auth_token",
        value=result["access_token"],
        httponly=True,
        samesite="lax",
    )
    return result


@router.post("/logout")
def logout(user: CurrentUser, service: AuthService = Depends(_auth_service)):
    service.logout(user["sub"])
    return {"message": "Logged out successfully"}


@router.get("/me", response_model=UserResponse)
def get_me(user: CurrentUser, service: AuthService = Depends(_auth_service)):
    return service.get_me(user["sub"])


# ── Organisation endpoints ─────────────────────────────────────────────────


@router.post("/apply", response_model=OrgResponse, status_code=201)
def apply_org(payload: ApplyOrgRequest, service: OrgService = Depends(_org_service)):
    return service.register_org(
        name=payload.org_name,
        org_type=payload.org_type,
        city=payload.city,
        email=payload.org_email,
        phone=payload.phone,
        owner_first_name=payload.first_name,
        owner_last_name=payload.last_name,
        owner_email=payload.email,
        owner_password=payload.password,
    )


@router.post("/invite/accept", response_model=UserResponse, status_code=201)
def accept_invitation(
    payload: AcceptInvitationRequest,
    service: OrgService = Depends(_org_service),
):
    return service.accept_invitation(
        code=payload.code,
        first_name=payload.first_name,
        last_name=payload.last_name,
        password=payload.password,
    )


@router.get("/admin/orgs/pending", dependencies=[Depends(require_roles("superadmin"))])
def list_pending_orgs(service: OrgService = Depends(_org_service)):
    return service.list_pending()
