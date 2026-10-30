"""
ALGO V1 - Dev Router (Quick Login for Development)
Создаёт тестовых пользователей и возвращает токены для быстрого входа.
НЕ ИСПОЛЬЗОВАТЬ В PRODUCTION!
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from typing import Optional
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .models import Organization, User, OrganizationMember
from .service import (
    hash_password,
    create_access_token,
    create_refresh_token,
    hash_token,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
)
from .schemas import AuthResponse, UserResponse, TokenPair, OrgResponse, SessionResponse
from .cookies import set_auth_cookies
from . import user_sessions

from src.core.database import get_db

router = APIRouter(prefix="/auth", tags=["Dev (Development Only)"])

# ─── Test user configs ───
DEV_USERS = {
    "owner": {
        "email": "dev_owner@algo.test",
        "first_name": "Владелец",
        "last_name": "Тестовый",
        "role": "owner",
        "org_role": "owner",
    },
    "admin": {
        "email": "dev_admin@algo.test",
        "first_name": "Админ",
        "last_name": "Тестовый",
        "role": "admin",
        "org_role": "admin",
    },
    "teacher": {
        "email": "dev_teacher@algo.test",
        "first_name": "Учитель",
        "last_name": "Тестовый",
        "role": "teacher",
        "org_role": "teacher",
    },
}

DEV_PASSWORD = "dev123"
DEV_ORG_NAME = "ALGO Dev Organization"


def _session_client(request: Optional[Request]) -> Optional[user_sessions.SessionClient]:
    """The trusted client address and the user agent of the quick-login request (see router._session_client)."""
    if request is None:
        return None
    from src.api.rate_limit import client_ip  # late: src.api imports this module while it is being initialised

    ip = client_ip(request)
    return user_sessions.SessionClient(
        ip=None if ip == "unknown" else ip, user_agent=request.headers.get("user-agent")
    )


@router.post("/dev-login", response_model=SessionResponse)
def dev_login(
    role: str = Query(..., description="Role: owner, admin, teacher"),
    db: Session = Depends(get_db),
    raw_request: Request = None,
):
    """
    DEV ONLY: Quick login as any role.
    Creates test users + org on first call.
    """
    if role not in DEV_USERS:
        raise HTTPException(
            status_code=400,
            detail=f"Role must be one of: {', '.join(DEV_USERS.keys())}",
        )

    config = DEV_USERS[role]

    # ─── Ensure dev org exists ───
    org = db.query(Organization).filter(Organization.name == DEV_ORG_NAME).first()
    if not org:
        org = Organization(
            name=DEV_ORG_NAME,
            org_type="private_school",
            city="Dev City",
            phone="+998901234567",
            email="dev@algo.test",
            status="active",
        )
        db.add(org)
        db.flush()

    # ─── Ensure user exists ───
    user = db.query(User).filter(User.email == config["email"]).first()
    if not user:
        user = User(
            email=config["email"],
            password_hash=hash_password(DEV_PASSWORD),
            first_name=config["first_name"],
            last_name=config["last_name"],
            role=config["role"],
            default_org_id=org.id,
            is_active=True,
            is_verified=True,
            language="ru",
        )
        db.add(user)
        db.flush()

        # Create membership
        member = OrganizationMember(
            org_id=org.id,
            user_id=user.id,
            org_role=config["org_role"],
            is_active=True,
        )
        db.add(member)
        db.flush()
    else:
        # Update org link and role if changed
        user.default_org_id = org.id
        user.role = config["role"]
        # Update membership
        member = (
            db.query(OrganizationMember)
            .filter(
                OrganizationMember.org_id == org.id,
                OrganizationMember.user_id == user.id,
            )
            .first()
        )
        if not member:
            member = OrganizationMember(
                org_id=org.id,
                user_id=user.id,
                org_role=config["org_role"],
                is_active=True,
            )
            db.add(member)
        else:
            member.org_role = config["org_role"]
            member.is_active = True

    # ─── Create tokens ───
    user.last_login_at = datetime.now(timezone.utc)
    user.login_count = (user.login_count or 0) + 1

    session_id = uuid4()
    access = create_access_token(
        str(user.id), user.role, str(org.id), config["org_role"], sid=str(session_id)
    )
    refresh = create_refresh_token(str(user.id))
    # A new session per quick-login (several browsers can be signed in as the same dev user, #101). Remembered: a developer does not want to sign in every 12 h.
    user_sessions.issue(
        db,
        user.id,
        hash_token(refresh),
        timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        _session_client(raw_request),
        remember=True,
        session_id=session_id,
    )

    db.commit()

    tokens = TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )

    user_response = UserResponse(
        id=str(user.id),
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        full_name=f"{user.last_name} {user.first_name}",
        role=user.role,
        default_org_id=str(org.id),
        org_role=config["org_role"],
        org_name=org.name,
        language=user.language or "ru",
        is_active=user.is_active,
        is_verified=user.is_verified,
        created_at=user.created_at.isoformat() if user.created_at else None,
    )

    org_response = OrgResponse(
        id=str(org.id),
        name=org.name,
        org_type=org.org_type or "private_school",
        city=org.city,
        phone=org.phone,
        email=org.email,
        status=org.status or "active",
        created_at=org.created_at.isoformat() if org.created_at else None,
    )

    body = SessionResponse(user=user_response, org=org_response).model_dump(mode="json")  # no token in the body (K2-1, A2)
    resp = JSONResponse(content=body)

    # One cookie policy for every sign-in path (flags, lifetimes).
    set_auth_cookies(resp, tokens.access_token, tokens.refresh_token, True)
    return resp


@router.get("/dev-invite-code")
def dev_invite_code(email: str = Query(..., min_length=5, max_length=255), db: Session = Depends(get_db)):
    """DEV ONLY (this router is mounted only when ``APP_ENV=development``): the code of the newest pending staff invitation for *email*.

    K2-1 (A1): the product never hands the code to anybody - it travels in the letter - and a development stack has no mailbox, so the
    end-to-end harness (``e2e/support/api.ts``) reads the code here to play the invited person.
    """
    from .models import Invitation

    invite = (
        db.query(Invitation)
        .filter(Invitation.status == "pending", Invitation.email == email.strip().lower())
        .order_by(Invitation.created_at.desc())
        .first()
    )
    if invite is None:
        raise HTTPException(status_code=404, detail="No pending invitation for this e-mail")
    return {"code": invite.code}
