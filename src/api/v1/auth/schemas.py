"""
ALGO V1 — Auth API Schemas  (Request + Response Pydantic models)

All response models use from_attributes=True for ORM serialisation.
Replaces src/auth/schemas.py as the canonical location.
"""

from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime


# ── Requests ──────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=6)


class ApplyOrgRequest(BaseModel):
    # Owner info
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    password: str = Field(..., min_length=8)

    # Organisation info
    org_name: str = Field(..., min_length=2, max_length=300)
    org_type: str = Field(..., pattern="^(center|private_school|university|tutoring_center|tutor|other)$")
    city: Optional[str] = None
    org_email: Optional[str] = None
    phone: Optional[str] = None


class AcceptInvitationRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=20)
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=8)


# ── Responses ─────────────────────────────────────────────────────────────────

class UserResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    email: str
    first_name: str
    last_name: str
    role: str
    default_org_id: Optional[str] = None
    language: str = "ru"
    is_active: bool
    created_at: Optional[datetime] = None


class OrgResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    name: str
    org_type: str
    city: Optional[str] = None
    status: str
    created_at: Optional[datetime] = None


class TokensResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserResponse
