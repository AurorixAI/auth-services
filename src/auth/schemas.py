"""
ALGO V1 - Pydantic Schemas для аутентификации
B2B модель: Organizations → Members → Students (no login)
"""

from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ============================================================================
# ORG APPLICATION (заявка на регистрацию организации)
# ============================================================================


class OrgApplicationRequest(BaseModel):
    """Заявка на регистрацию организации + владелец"""

    # Организация
    org_name: str = Field(..., min_length=2, max_length=300)
    org_type: str = Field(
        default="private_school"
    )  # private_school, tutoring_center, other, center, university, tutor
    city: Optional[str] = Field(None, max_length=200)
    phone: Optional[str] = Field(None, max_length=30)

    # Владелец (первый пользователь)
    email: str = Field(..., min_length=5, max_length=255)
    password: str = Field(..., min_length=6, max_length=128)
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    language: str = Field(default="ru")

    @field_validator("org_type")
    @classmethod
    def validate_org_type(cls, v):
        allowed = {
            "center",
            "private_school",
            "university",
            "tutoring_center",
            "tutor",
            "other",
        }
        if v not in allowed:
            raise ValueError(f"org_type must be one of: {', '.join(sorted(allowed))}")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        if "@" not in v:
            raise ValueError("Invalid email format")
        return v.lower().strip()


# ============================================================================
# LOGIN (без изменений)
# ============================================================================


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=255)
    password: str = Field(..., min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v):
        return v.lower().strip()


class RefreshTokenRequest(BaseModel):
    refresh_token: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=6, max_length=128)


class UpdateProfileRequest(BaseModel):
    first_name: Optional[str] = Field(None, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
    patronymic: Optional[str] = Field(None, max_length=100)
    language: Optional[str] = Field(None, max_length=5)


# ============================================================================
# INVITATIONS (приглашения в организацию)
# ============================================================================


class InviteMemberRequest(BaseModel):
    """Пригласить сотрудника в организацию"""

    email: Optional[str] = Field(None, max_length=255)
    org_role: str = Field(default="teacher")

    @field_validator("org_role")
    @classmethod
    def validate_role(cls, v):
        if v not in {"admin", "teacher"}:
            raise ValueError("org_role must be admin or teacher")
        return v


class AcceptInviteRequest(BaseModel):
    """Принять приглашение (создать аккаунт)"""

    code: str = Field(..., min_length=4, max_length=20)
    email: str = Field(..., min_length=5, max_length=255)
    password: str = Field(..., min_length=6, max_length=128)
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v):
        return v.lower().strip()


# ============================================================================
# STUDENTS (ученики без логина)
# ============================================================================


class StudentCreateRequest(BaseModel):
    """Добавить ученика"""

    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    class_level: int = Field(..., ge=1, le=11)
    student_phone: str = Field(..., max_length=30)
    comment: Optional[str] = None


class StudentUpdateRequest(BaseModel):
    """Обновить данные ученика (после диагностики и т.д.)"""

    first_name: Optional[str] = Field(None, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
    class_level: Optional[int] = Field(None, ge=1, le=11)
    group_id: Optional[str] = None
    student_number: Optional[str] = Field(None, max_length=30)
    student_phone: Optional[str] = Field(None, max_length=30)
    comment: Optional[str] = None
    status: Optional[str] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v and v not in {"new", "diagnosed", "active", "archived"}:
            raise ValueError("Invalid student status")
        return v


class StudentResponse(BaseModel):
    id: str
    org_id: str
    first_name: str
    last_name: str
    full_name: str
    class_level: int
    group_id: Optional[str] = None
    student_number: Optional[str] = None
    student_phone: Optional[str] = None
    comment: Optional[str] = None
    status: str
    created_at: Optional[str] = None


# ============================================================================
# DIAGNOSTIC INVITES
# ============================================================================


class DiagnosticCreateRequest(BaseModel):
    """Создать диагностику для ученика"""

    student_id: str
    class_level: int = Field(..., ge=1, le=11)
    diagnostic_type: str = Field(default="initial")

    @field_validator("diagnostic_type")
    @classmethod
    def validate_type(cls, v):
        if v not in {"initial", "recheck", "pre_exam"}:
            raise ValueError("diagnostic_type must be initial, recheck, or pre_exam")
        return v


class DiagnosticQuickRequest(BaseModel):
    """Быстрое создание: ученик + диагностика за раз"""

    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    class_level: int = Field(..., ge=1, le=11)


class DiagnosticInviteResponse(BaseModel):
    id: str
    student_id: str
    student_name: str
    code: str
    url: str
    class_level: int
    diagnostic_type: str
    status: str
    expires_at: Optional[str] = None
    created_at: Optional[str] = None


# ============================================================================
# GROUPS
# ============================================================================


class GroupCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    class_level: Optional[int] = Field(None, ge=1, le=11)
    teacher_id: Optional[str] = None
    description: Optional[str] = None
    max_students: int = Field(default=30, ge=1, le=100)


class GroupUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    class_level: Optional[int] = Field(None, ge=1, le=11)
    teacher_id: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    max_students: Optional[int] = Field(None, ge=1, le=100)


class GroupResponse(BaseModel):
    id: str
    org_id: str
    name: str
    class_level: Optional[int] = None
    teacher_id: Optional[str] = None
    teacher_name: Optional[str] = None
    status: str = "active"
    max_students: int = 30
    student_count: int = 0
    created_at: Optional[str] = None


class BulkAddStudentsRequest(BaseModel):
    student_ids: list[str] = Field(..., min_length=1)


# ============================================================================
# RESPONSE SCHEMAS
# ============================================================================


class UserResponse(BaseModel):
    id: str
    email: str
    first_name: str
    last_name: str
    full_name: str
    role: str
    default_org_id: Optional[str] = None
    org_role: Optional[str] = None
    org_name: Optional[str] = None
    language: str = "ru"
    is_active: bool = True
    is_verified: bool = False
    must_change_password: bool = False
    created_at: Optional[str] = None


class OrgResponse(BaseModel):
    id: str
    name: str
    org_type: str
    city: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    status: str
    created_at: Optional[str] = None


class OrgUpdateRequest(BaseModel):
    """Обновление данных организации"""

    name: Optional[str] = None
    city: Optional[str] = None
    phone: Optional[str] = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 3600


class AuthResponse(BaseModel):
    user: UserResponse
    tokens: Optional[TokenPair] = None
    org: Optional[OrgResponse] = None


class MessageResponse(BaseModel):
    message: str
    success: bool = True


# ============================================================================
# REGISTRATION FLOW — new schemas
# ============================================================================


class OrgApplicationPendingResponse(BaseModel):
    """Returned when an organization registers — pending admin approval."""

    message: str = "Заявка принята и ожидает подтверждения администратора."
    org_id: str
    org_name: str
    status: str = "pending"
    success: bool = True


class AdminApproveRequest(BaseModel):
    """Admin approves (or rejects) an organization application."""

    action: str = Field(default="approve")  # "approve" or "reject"
    rejection_reason: Optional[str] = None

    @field_validator("action")
    @classmethod
    def validate_action(cls, v):
        if v not in {"approve", "reject"}:
            raise ValueError("action must be 'approve' or 'reject'")
        return v


class ActivateAccountRequest(BaseModel):
    """Organization owner activates their account via emailed token."""

    token: str = Field(..., min_length=10)
