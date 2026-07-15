"""
ALGO V1 - Auth Service
B2B: Organizations → Members (owner/admin/teacher) → Students (no login)
JWT, bcrypt, org CRUD, invite flow, student management, diagnostic invites
"""

import os
import hashlib
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional
import logging

from jose import jwt, JWTError
from passlib.context import CryptContext
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func

from .models import (
    Organization,
    User,
    OrganizationMember,
    Invitation,
    Student,
    Group,
    StudentGroup,
    DiagnosticInvite,
    StudentDiagnosticResult,
)
from .schemas import (
    OrgApplicationRequest,
    LoginRequest,
    UserResponse,
    TokenPair,
    AuthResponse,
    OrgResponse,
    InviteMemberRequest,
    AcceptInviteRequest,
    StudentCreateRequest,
    StudentUpdateRequest,
    StudentResponse,
    DiagnosticCreateRequest,
    DiagnosticQuickRequest,
    DiagnosticInviteResponse,
    GroupCreateRequest,
    GroupUpdateRequest,
    GroupResponse,
    OrgApplicationPendingResponse,
)
from src.core.config import get_settings
from src.core.email import send_admin_new_application
from src.core.exceptions import (
    AccountNotFoundError,
    InvalidCredentialsError,
    AccountInactiveError,
)

logger = logging.getLogger(__name__)

# ============================================================================
# КОНФИГУРАЦИЯ
# ============================================================================

_DEFAULT_SECRET = "algo-v1-dev-secret-key-change-in-prod"
SECRET_KEY = os.getenv("SECRET_KEY", _DEFAULT_SECRET)
JWT_SECRET = os.getenv("JWT_SECRET_KEY", SECRET_KEY)

if SECRET_KEY == _DEFAULT_SECRET and os.getenv("ENV", "dev").lower() not in (
    "dev",
    "development",
    "test",
):
    raise RuntimeError(
        "CRITICAL: SECRET_KEY env var not set in production! "
        "Set SECRET_KEY to a strong random string."
    )
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_TTL_MINUTES", "60"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("JWT_REFRESH_TTL_DAYS", "30"))
DIAGNOSTIC_CODE_LENGTH = 8
INVITE_CODE_LENGTH = 8
BASE_DIAGNOSTIC_URL = os.getenv("DIAGNOSTIC_BASE_URL", "http://localhost:3000/d")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ============================================================================
# PASSWORD / TOKEN UTILS
# ============================================================================


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _theta_to_level(theta: float) -> str:
    """Map IRT theta to a pedagogical level label.

    Mirrors diagnostics-service's alpha_engine.theta_to_level() so a
    student's level reads the same everywhere in the app.
    """
    if theta >= 1.0:
        return "above_grade"
    if theta >= -0.5:
        return "at_grade"
    return "below_grade"


def generate_code(
    length: int = 6, digits_only: bool = True, exclude_ambiguous: bool = False
) -> str:
    if digits_only:
        charset = string.digits
    elif exclude_ambiguous:
        # No 0/O, 1/I/L — codes a student reads off a screen and may need to
        # type or read aloud shouldn't hinge on telling those apart.
        charset = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    else:
        charset = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(charset) for _ in range(length))


# ============================================================================
# JWT UTILS
# ============================================================================


def create_access_token(
    user_id: str,
    role: str,
    org_id: Optional[str] = None,
    org_role: Optional[str] = None,
    expires_delta: Optional[timedelta] = None,
) -> str:
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload = {
        "sub": user_id,
        "role": role,
        "type": "access",
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    if org_id:
        payload["org_id"] = org_id
    if org_role:
        payload["org_role"] = org_role
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": user_id,
        "type": "refresh",
        "jti": secrets.token_hex(16),
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError as e:
        logger.warning(f"JWT decode error: {e}")
        return None


def verify_access_token(token: str) -> Optional[dict]:
    payload = decode_token(token)
    if payload and payload.get("type") == "access":
        return payload
    return None


def verify_refresh_token(token: str) -> Optional[dict]:
    payload = decode_token(token)
    if payload and payload.get("type") == "refresh":
        return payload
    return None


# ============================================================================
# AUTH SERVICE
# ============================================================================


class AuthService:
    def __init__(self, db: Session):
        self.db = db

    # ─── Helpers ───

    def _get_user_org_info(
        self, user: User
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Return (org_id, org_role, org_name) for user's default org"""
        if not user.default_org_id:
            return None, None, None
        member = (
            self.db.query(OrganizationMember)
            .filter(
                OrganizationMember.org_id == user.default_org_id,
                OrganizationMember.user_id == user.id,
                OrganizationMember.is_active,
            )
            .first()
        )
        org = (
            self.db.query(Organization)
            .filter(Organization.id == user.default_org_id)
            .first()
        )
        org_role = member.org_role if member else None
        org_name = org.name if org else None
        return (
            str(user.default_org_id) if user.default_org_id else None,
            org_role,
            org_name,
        )

    def _build_user_response(self, user: User) -> UserResponse:
        d = user.to_dict()
        org_id, org_role, org_name = self._get_user_org_info(user)
        d["org_role"] = org_role
        d["org_name"] = org_name
        return UserResponse(**d)

    def _build_org_response(self, org: Organization) -> OrgResponse:
        return OrgResponse(
            id=str(org.id),
            name=org.name,
            org_type=org.org_type or "center",
            city=org.city,
            phone=org.phone,
            email=org.email,
            status=org.status or "active",
            created_at=org.created_at.isoformat() if org.created_at else None,
        )

    def _create_tokens(self, user: User) -> TokenPair:
        org_id, org_role, _ = self._get_user_org_info(user)
        access = create_access_token(str(user.id), user.role, org_id, org_role)
        refresh = create_refresh_token(str(user.id))
        user.refresh_token_hash = hash_token(refresh)
        return TokenPair(
            access_token=access,
            refresh_token=refresh,
            expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

    # ═══════════════════════════════════════════════════════════════════
    # 1. ORG APPLICATION — заявка на регистрацию организации + владелец
    # ═══════════════════════════════════════════════════════════════════

    def apply_organization(self, request: OrgApplicationRequest) -> OrgApplicationPendingResponse:
        """Создать организацию + первого пользователя (owner).

        Auto-approve is ON by default (settings.auto_approve_organizations) —
        the org + owner are activated immediately so the owner can log in
        right away with the email/password they just registered with. The
        manual-review path (approve_organization / approve_org.sh) is left
        fully intact for when this flag is turned back off.
        """
        settings = get_settings()
        auto_approve = settings.auto_approve_organizations

        # Уникальность email
        existing = self.db.query(User).filter(User.email == request.email).first()
        if existing:
            raise ValueError("Пользователь с таким email уже существует")

        # Создание организации
        org = Organization(
            name=request.org_name,
            org_type=request.org_type,
            city=request.city,
            phone=request.phone,
            email=request.email,
            status="active" if auto_approve else "pending",
        )
        if auto_approve:
            org.approved_at = datetime.now(timezone.utc)
        self.db.add(org)
        self.db.flush()

        # Создание пользователя-владельца
        user = User(
            email=request.email,
            password_hash=hash_password(request.password),
            first_name=request.first_name,
            last_name=request.last_name,
            role="owner",
            default_org_id=org.id,
            language=request.language,
            is_active=auto_approve,
            is_verified=auto_approve,
        )
        self.db.add(user)
        self.db.flush()

        # Привязка к организации
        member = OrganizationMember(
            org_id=org.id,
            user_id=user.id,
            org_role="owner",
        )
        self.db.add(member)

        # Обновить владельца
        org.owner_id = user.id

        self.db.commit()

        logger.info(
            f"New org application ({'auto-activated' if auto_approve else 'pending'}): "
            f"{org.name} by {user.email}"
        )

        # Best-effort admin notification — does not block registration
        try:
            send_admin_new_application(
                org_name=org.name,
                org_id=str(org.id),
                owner_email=user.email,
                owner_name=user.full_name,
            )
        except Exception:
            logger.exception("Failed to send admin notification email")

        if auto_approve:
            return OrgApplicationPendingResponse(
                org_id=str(org.id),
                org_name=org.name,
                status="active",
                message="Организация активирована. Вы можете войти со своим email и паролем.",
            )
        return OrgApplicationPendingResponse(
            org_id=str(org.id),
            org_name=org.name,
        )

    # ═══════════════════════════════════════════════════════════════════
    # 1b. ADMIN APPROVAL — одобрение / отклонение заявки
    # ═══════════════════════════════════════════════════════════════════

    def approve_organization(self, org_id: str, action: str = "approve", rejection_reason: str | None = None) -> dict:
        """Admin approves or rejects a pending organization."""
        org = self.db.query(Organization).filter(Organization.id == org_id).first()
        if not org:
            raise ValueError("Организация не найдена")
        if org.status != "pending":
            raise ValueError(f"Организация уже обработана (статус: {org.status})")

        if action == "reject":
            org.status = "rejected"
            org.rejection_reason = rejection_reason
            self.db.commit()
            logger.info(f"Org rejected: {org.name} (id={org_id})")
            return {"org_id": str(org.id), "status": "rejected", "message": "Заявка отклонена"}

        # Approval activates the org and owner immediately — login works
        # right away with the credentials they registered with. There is no
        # separate "click the emailed link" step: SMTP isn't configured here
        # and the admin is already manually vetting the application, so a
        # second confirmation step would only be friction with no security
        # benefit.
        org.status = "active"
        org.approved_at = datetime.now(timezone.utc)

        owner = self.db.query(User).filter(User.id == org.owner_id).first()
        if not owner:
            raise ValueError("Владелец организации не найден")
        owner.is_active = True
        owner.is_verified = True

        self.db.commit()

        logger.info(f"Org approved and activated: {org.name} (id={org_id})")
        return {
            "org_id": str(org.id),
            "status": "active",
            "owner_email": owner.email,
            "message": f"Организация активирована. {owner.email} может входить со своим паролем.",
        }

    # ═══════════════════════════════════════════════════════════════════
    # 1c. ACTIVATION — активация аккаунта по токену из письма
    # ═══════════════════════════════════════════════════════════════════

    def activate_account(self, token: str) -> AuthResponse:
        """Activate an org + owner account using the emailed token."""
        token_hash = hash_token(token)

        org = (
            self.db.query(Organization)
            .filter(
                Organization.activation_token == token_hash,
                Organization.status.in_(["pending", "active"]),
            )
            .first()
        )
        if not org:
            raise ValueError("Невалидная или уже использованная ссылка активации")

        # Check expiry
        if org.activation_token_expires_at:
            exp = (
                org.activation_token_expires_at
                if org.activation_token_expires_at.tzinfo
                else org.activation_token_expires_at.replace(tzinfo=timezone.utc)
            )
            if exp < datetime.now(timezone.utc):
                raise ValueError("Ссылка активации истекла. Обратитесь к администратору.")

        # Activate org
        org.status = "active"
        org.activation_token = None
        org.activation_token_expires_at = None

        # Activate owner
        owner = self.db.query(User).filter(User.id == org.owner_id).first()
        if not owner:
            raise ValueError("Владелец организации не найден")
        owner.is_active = True
        owner.is_verified = True

        # Issue tokens
        tokens = self._create_tokens(owner)
        self.db.commit()

        logger.info(f"Org activated: {org.name} by {owner.email}")

        return AuthResponse(
            user=self._build_user_response(owner),
            tokens=tokens,
            org=self._build_org_response(org),
        )

    # ═══════════════════════════════════════════════════════════════════
    # 2. LOGIN
    # ═══════════════════════════════════════════════════════════════════

    def login(self, request: LoginRequest) -> AuthResponse:
        user = self.db.query(User).filter(User.email == request.email).first()
        if not user:
            raise AccountNotFoundError()
        if not user.is_active:
            raise AccountInactiveError()
        if not verify_password(request.password, user.password_hash):
            raise InvalidCredentialsError()

        user.last_login_at = datetime.now(timezone.utc)
        user.login_count = (user.login_count or 0) + 1

        tokens = self._create_tokens(user)
        self.db.commit()

        logger.info(f"User logged in: {user.email}")

        org = None
        if user.default_org_id:
            org_obj = (
                self.db.query(Organization)
                .filter(Organization.id == user.default_org_id)
                .first()
            )
            if org_obj:
                org = self._build_org_response(org_obj)

        return AuthResponse(
            user=self._build_user_response(user),
            tokens=tokens,
            org=org,
        )

    # ═══════════════════════════════════════════════════════════════════
    # 3. TOKEN MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════

    def refresh_tokens(self, refresh_token: str) -> TokenPair:
        payload = verify_refresh_token(refresh_token)
        if not payload:
            raise ValueError("Невалидный refresh token")

        user = self.db.query(User).filter(User.id == payload.get("sub")).first()
        if not user or not user.is_active:
            raise ValueError("Пользователь не найден или заблокирован")
        if user.refresh_token_hash != hash_token(refresh_token):
            raise ValueError("Refresh token отозван")

        tokens = self._create_tokens(user)
        self.db.commit()
        return tokens

    def logout(self, user_id: str) -> None:
        user = self.db.query(User).filter(User.id == user_id).first()
        if user:
            user.refresh_token_hash = None
            self.db.commit()

    def get_user_by_id(self, user_id: str) -> Optional[User]:
        return self.db.query(User).filter(User.id == user_id).first()

    def change_password(
        self, user_id: str, old_password: str, new_password: str
    ) -> bool:
        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError("Пользователь не найден")
        if not verify_password(old_password, user.password_hash):
            raise ValueError("Неверный текущий пароль")
        user.password_hash = hash_password(new_password)
        user.refresh_token_hash = None
        user.must_change_password = False  # cleared once they set their own
        self.db.commit()
        return True

    def update_profile(self, user_id: str, **kwargs) -> User:
        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError("Пользователь не найден")
        for key, value in kwargs.items():
            if value is not None and hasattr(user, key):
                setattr(user, key, value)
        self.db.commit()
        return user

    # ═══════════════════════════════════════════════════════════════════
    # 4. INVITATIONS — приглашения в организацию
    # ═══════════════════════════════════════════════════════════════════

    def create_invitation(
        self, org_id: str, creator_id: str, request: InviteMemberRequest
    ) -> dict:
        """Создать приглашение (owner/admin приглашает admin/teacher)"""
        code = generate_code(INVITE_CODE_LENGTH, digits_only=False)

        invite = Invitation(
            org_id=org_id,
            invited_by=creator_id,
            email=request.email,
            org_role=request.org_role,
            code=code,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        self.db.add(invite)
        self.db.commit()

        logger.info(
            f"Invitation created: {code} for role={request.org_role} in org={org_id}"
        )

        return {
            "code": code,
            "org_role": request.org_role,
            "expires_at": invite.expires_at.isoformat(),
        }

    def accept_invitation(self, request: AcceptInviteRequest) -> AuthResponse:
        """Принять приглашение — создать аккаунт и привязать к организации"""
        invite = (
            self.db.query(Invitation)
            .filter(Invitation.code == request.code, Invitation.status == "pending")
            .first()
        )
        if not invite:
            raise ValueError("Приглашение не найдено или уже использовано")
        if invite.expires_at:
            exp = (
                invite.expires_at
                if invite.expires_at.tzinfo
                else invite.expires_at.replace(tzinfo=timezone.utc)
            )
            if exp < datetime.now(timezone.utc):
                invite.status = "expired"
                self.db.commit()
                raise ValueError("Приглашение истекло")

        existing = self.db.query(User).filter(User.email == request.email).first()
        if existing:
            raise ValueError("Пользователь с таким email уже существует")

        org = (
            self.db.query(Organization).filter(Organization.id == invite.org_id).first()
        )
        if not org:
            raise ValueError("Организация не найдена")

        # Создание аккаунта
        user = User(
            email=request.email,
            password_hash=hash_password(request.password),
            first_name=request.first_name,
            last_name=request.last_name,
            role=invite.org_role,  # admin or teacher
            default_org_id=org.id,
        )
        self.db.add(user)
        self.db.flush()

        # Привязка
        member = OrganizationMember(
            org_id=org.id,
            user_id=user.id,
            org_role=invite.org_role,
        )
        self.db.add(member)

        # Закрыть приглашение
        invite.status = "accepted"
        invite.accepted_by = user.id
        invite.accepted_at = datetime.now(timezone.utc)

        tokens = self._create_tokens(user)
        self.db.commit()

        logger.info(f"Invite {invite.code} accepted by {user.email}")

        return AuthResponse(
            user=self._build_user_response(user),
            tokens=tokens,
            org=self._build_org_response(org),
        )

    def list_invitations(self, org_id: str) -> list:
        invites = (
            self.db.query(Invitation)
            .filter(Invitation.org_id == org_id)
            .order_by(Invitation.created_at.desc())
            .limit(100)
            .all()
        )
        return [i.to_dict() for i in invites]

    def revoke_invitation(self, org_id: str, invite_id: str) -> bool:
        invite = (
            self.db.query(Invitation)
            .filter(
                Invitation.id == invite_id,
                Invitation.org_id == org_id,
                Invitation.status == "pending",
            )
            .first()
        )
        if not invite:
            raise ValueError("Приглашение не найдено")
        # "revoked" isn't a valid status per the DB check constraint — use
        # "cancelled" (the constraint only allows pending/accepted/expired/cancelled).
        invite.status = "cancelled"
        self.db.commit()
        return True

    def get_invitation_info(self, code: str) -> dict:
        """Публичная информация о приглашении — для экрана принятия (до аутентификации)."""
        invite = (
            self.db.query(Invitation)
            .filter(Invitation.code == code, Invitation.status == "pending")
            .first()
        )
        if not invite:
            raise ValueError("Приглашение не найдено или уже использовано")
        if invite.expires_at:
            exp = (
                invite.expires_at
                if invite.expires_at.tzinfo
                else invite.expires_at.replace(tzinfo=timezone.utc)
            )
            if exp < datetime.now(timezone.utc):
                raise ValueError("Приглашение истекло")

        org = self.db.query(Organization).filter(Organization.id == invite.org_id).first()
        return {
            "org_name": org.name if org else "",
            "org_role": invite.org_role,
            "email": invite.email,
        }

    # ═══════════════════════════════════════════════════════════════════
    # 5. ORG MEMBERS — управление сотрудниками
    # ═══════════════════════════════════════════════════════════════════

    def list_org_members(self, org_id: str) -> list:
        members = (
            self.db.query(OrganizationMember)
            .options(joinedload(OrganizationMember.user))
            .filter(
                OrganizationMember.org_id == org_id,
                OrganizationMember.is_active,
            )
            .all()
        )
        result = []
        for m in members:
            if m.user:
                d = m.user.to_dict()
                d["org_role"] = m.org_role
                d["member_id"] = str(m.id)
                result.append(d)
        return result

    def update_member_role(self, org_id: str, member_id: str, new_role: str) -> dict:
        if new_role not in {"admin", "teacher"}:
            raise ValueError("Роль должна быть admin или teacher")

        member = (
            self.db.query(OrganizationMember)
            .filter(
                OrganizationMember.id == member_id, OrganizationMember.org_id == org_id
            )
            .first()
        )
        if not member:
            raise ValueError("Сотрудник не найден")
        if member.org_role == "owner":
            raise ValueError("Нельзя изменить роль владельца")

        member.org_role = new_role

        # Обновим роль в User тоже
        user = self.db.query(User).filter(User.id == member.user_id).first()
        if user:
            user.role = new_role

        self.db.commit()
        return {"member_id": str(member.id), "new_role": new_role}

    def remove_member(self, org_id: str, member_id: str) -> bool:
        member = (
            self.db.query(OrganizationMember)
            .filter(
                OrganizationMember.id == member_id, OrganizationMember.org_id == org_id
            )
            .first()
        )
        if not member:
            raise ValueError("Сотрудник не найден")
        if member.org_role == "owner":
            raise ValueError("Нельзя удалить владельца")

        member.is_active = False
        user = self.db.query(User).filter(User.id == member.user_id).first()
        if user:
            user.is_active = False

        self.db.commit()
        return True

    # ═══════════════════════════════════════════════════════════════════
    # 6. STUDENTS — ученики (без логина)
    # ═══════════════════════════════════════════════════════════════════

    def create_student(
        self, org_id: str, request: StudentCreateRequest
    ) -> StudentResponse:
        student = Student(
            org_id=org_id,
            first_name=request.first_name,
            last_name=request.last_name,
            class_level=request.class_level,
            student_phone=request.student_phone,
            comment=request.comment,
            status="new",
        )
        self.db.add(student)
        self.db.commit()

        return self._build_student_response(student)

    def update_student(
        self, org_id: str, student_id: str, request: StudentUpdateRequest
    ) -> StudentResponse:
        student = (
            self.db.query(Student)
            .filter(Student.id == student_id, Student.org_id == org_id)
            .first()
        )
        if not student:
            raise ValueError("Ученик не найден")

        update_data = request.model_dump(exclude_none=True)
        for key, value in update_data.items():
            if hasattr(student, key):
                setattr(student, key, value)

        self.db.commit()
        return self._build_student_response(student)

    def get_student(self, org_id: str, student_id: str) -> StudentResponse:
        student = (
            self.db.query(Student)
            .filter(Student.id == student_id, Student.org_id == org_id)
            .first()
        )
        if not student:
            raise ValueError("Ученик не найден")
        return self._build_student_response(student)

    def list_students(
        self,
        org_id: str,
        class_level: Optional[int] = None,
        group_id: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[StudentResponse]:
        query = self.db.query(Student).filter(Student.org_id == org_id)

        if class_level:
            query = query.filter(Student.class_level == class_level)
        if group_id:
            query = query.filter(Student.group_id == group_id)
        if status:
            query = query.filter(Student.status == status)
        if search:
            pattern = f"%{search}%"
            query = query.filter(
                (Student.first_name.ilike(pattern)) | (Student.last_name.ilike(pattern))
            )

        students = (
            query.order_by(Student.created_at.desc()).offset(offset).limit(limit).all()
        )
        return [self._build_student_response(s) for s in students]

    def _build_student_response(self, student: Student) -> StudentResponse:
        return StudentResponse(
            id=str(student.id),
            org_id=str(student.org_id),
            first_name=student.first_name,
            last_name=student.last_name,
            full_name=f"{student.first_name} {student.last_name}",
            class_level=student.class_level,
            group_id=str(student.group_id) if student.group_id else None,
            student_number=student.student_number,
            student_phone=student.student_phone,
            comment=student.comment,
            status=student.status or "new",
            created_at=student.created_at.isoformat() if student.created_at else None,
        )

    # ═══════════════════════════════════════════════════════════════════
    # 7. DIAGNOSTIC INVITES — QR / ссылки для диагностики
    # ═══════════════════════════════════════════════════════════════════

    def create_diagnostic_invite(
        self, org_id: str, creator_id: str, request: DiagnosticCreateRequest
    ) -> DiagnosticInviteResponse:
        """Создать диагностику для существующего ученика"""
        student = (
            self.db.query(Student)
            .filter(Student.id == request.student_id, Student.org_id == org_id)
            .first()
        )
        if not student:
            raise ValueError("Ученик не найден")

        code = generate_code(DIAGNOSTIC_CODE_LENGTH, digits_only=False, exclude_ambiguous=True)

        invite = DiagnosticInvite(
            org_id=org_id,
            student_id=student.id,
            created_by=creator_id,
            code=code,
            class_level=request.class_level,
            diagnostic_type=request.diagnostic_type,
            status="pending",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        self.db.add(invite)
        self.db.commit()

        url = f"{BASE_DIAGNOSTIC_URL}/{code}"

        return DiagnosticInviteResponse(
            id=str(invite.id),
            student_id=str(student.id),
            student_name=f"{student.first_name} {student.last_name}",
            code=code,
            url=url,
            class_level=request.class_level,
            diagnostic_type=request.diagnostic_type,
            status="pending",
            expires_at=invite.expires_at.isoformat() if invite.expires_at else None,
            created_at=invite.created_at.isoformat() if invite.created_at else None,
        )

    def create_quick_diagnostic(
        self, org_id: str, creator_id: str, request: DiagnosticQuickRequest
    ) -> DiagnosticInviteResponse:
        """Быстро: создать ученика + диагностику за один запрос"""
        student = Student(
            org_id=org_id,
            first_name=request.first_name,
            last_name=request.last_name,
            class_level=request.class_level,
            status="new",
        )
        self.db.add(student)
        self.db.flush()

        diag_request = DiagnosticCreateRequest(
            student_id=str(student.id),
            class_level=request.class_level,
            diagnostic_type="initial",
        )
        return self.create_diagnostic_invite(org_id, creator_id, diag_request)

    def get_diagnostic_by_code(self, code: str) -> Optional[dict]:
        """Получить диагностику по коду (публичный, без JWT)"""
        invite = (
            self.db.query(DiagnosticInvite)
            .filter(DiagnosticInvite.code == code)
            .first()
        )
        if not invite:
            return None

        student = self.db.query(Student).filter(Student.id == invite.student_id).first()

        return {
            "id": str(invite.id),
            "student_id": str(invite.student_id) if invite.student_id else None,
            "student_first_name": student.first_name if student else None,
            "student_last_name": student.last_name if student else None,
            "class_level": invite.class_level,
            "diagnostic_type": invite.diagnostic_type,
            "status": invite.status,
            "org_id": str(invite.org_id),
            "expires_at": invite.expires_at.isoformat() if invite.expires_at else None,
        }

    def confirm_diagnostic(self, code: str) -> dict:
        """Подтверждение перехода pending → confirmed (idempotent).

        No name-matching: the code itself is the sole identity anchor —
        it's already unique and bound 1:1 to one student_id at creation time.
        Fuzzy-matching a typed name against the DB record used to be the
        actual "who is this" check, which silently broke for students that
        share a name (a false match/mismatch either way). Trusting the code
        removes that failure mode and the typing step entirely.
        """
        invite = (
            self.db.query(DiagnosticInvite)
            .filter(
                DiagnosticInvite.code == code,
                DiagnosticInvite.status.in_(["pending", "confirmed"]),
            )
            .first()
        )
        if not invite:
            raise ValueError("Диагностика не найдена или уже начата")

        if invite.expires_at:
            exp = (
                invite.expires_at
                if invite.expires_at.tzinfo
                else invite.expires_at.replace(tzinfo=timezone.utc)
            )
            if exp < datetime.now(timezone.utc):
                invite.status = "expired"
                self.db.commit()
                raise ValueError("Ссылка на диагностику истекла")

        if invite.status != "confirmed":
            invite.status = "confirmed"
            self.db.commit()

        return {
            "id": str(invite.id),
            "status": "confirmed",
            "class_level": invite.class_level,
            "student_id": str(invite.student_id),
            "org_id": str(invite.org_id),
        }

    def start_diagnostic(self, code: str, cat_session_id: Optional[str] = None) -> dict:
        """Перевод confirmed/pending/in_progress → in_progress (вызывается из CAT router).
        Idempotent: повторный вызов для in_progress возвращает успех."""
        invite = (
            self.db.query(DiagnosticInvite)
            .filter(
                DiagnosticInvite.code == code,
                DiagnosticInvite.status.in_(["confirmed", "pending", "in_progress"]),
            )
            .first()
        )
        if not invite:
            raise ValueError("Диагностика не найдена или уже завершена")

        # Если уже in_progress — idempotent, обновляем только cat_session_id
        if invite.status != "in_progress":
            invite.status = "in_progress"
            invite.started_at = datetime.now(timezone.utc)
        if cat_session_id:
            invite.cat_session_id = cat_session_id
        self.db.commit()

        return {"id": str(invite.id), "status": "in_progress"}

    def complete_diagnostic(self, code: str, result_data: dict) -> dict:
        """Завершение диагностики — сохранение результатов"""
        invite = (
            self.db.query(DiagnosticInvite)
            .filter(
                DiagnosticInvite.code == code, DiagnosticInvite.status == "in_progress"
            )
            .first()
        )
        if not invite:
            raise ValueError("Диагностика не найдена или не начата")

        invite.status = "completed"
        invite.completed_at = datetime.now(timezone.utc)

        # Сохранить результат
        result = StudentDiagnosticResult(
            student_id=invite.student_id,
            org_id=invite.org_id,
            invite_id=invite.id,
            session_id=invite.cat_session_id,
            diagnostic_type=invite.diagnostic_type,
            final_theta=result_data.get("final_theta"),
            se=result_data.get("se"),
            accuracy=result_data.get("accuracy"),
            total_tasks=result_data.get("total_items"),
            identified_gaps=result_data.get("identified_gaps"),
            root_gaps=result_data.get("root_gaps"),
            error_summary=result_data.get("error_summary"),
            integrity_report=result_data.get("integrity_report"),
            recommended_level=result_data.get("recommended_level"),
            recommended_group_id=result_data.get("recommended_group_id"),
        )
        self.db.add(result)

        # Обновить статус ученика
        student = self.db.query(Student).filter(Student.id == invite.student_id).first()
        if student and student.status == "new":
            student.status = "diagnosed"

        self.db.commit()

        logger.info(
            f"Diagnostic completed: code={code}, theta={result_data.get('final_theta')}"
        )

        return {
            "id": str(result.id),
            "status": "completed",
            "student_id": str(invite.student_id),
            "org_id": str(invite.org_id),
        }

    def list_diagnostic_invites(
        self, org_id: str, status: Optional[str] = None, limit: int = 50
    ) -> list:
        """Список диагностик организации (для admin dashboard / live queue)"""
        query = (
            self.db.query(DiagnosticInvite)
            .options(joinedload(DiagnosticInvite.student))
            .filter(DiagnosticInvite.org_id == org_id)
        )
        if status:
            query = query.filter(DiagnosticInvite.status == status)
        invites = query.order_by(DiagnosticInvite.created_at.desc()).limit(limit).all()

        result = []
        for inv in invites:
            student = inv.student
            result.append(
                {
                    "id": str(inv.id),
                    "code": inv.code,
                    "student_id": str(inv.student_id) if inv.student_id else None,
                    "student_name": f"{student.first_name} {student.last_name}"
                    if student
                    else "—",
                    "class_level": inv.class_level,
                    "diagnostic_type": inv.diagnostic_type,
                    "status": inv.status,
                    "created_at": inv.created_at.isoformat()
                    if inv.created_at
                    else None,
                    "started_at": inv.started_at.isoformat()
                    if inv.started_at
                    else None,
                    "completed_at": inv.completed_at.isoformat()
                    if inv.completed_at
                    else None,
                }
            )
        return result

    # ═══════════════════════════════════════════════════════════════════
    # 8. GROUPS — группы
    # ═══════════════════════════════════════════════════════════════════

    def create_group(self, org_id: str, request: GroupCreateRequest) -> GroupResponse:
        group = Group(
            org_id=org_id,
            name=request.name,
            class_level=request.class_level,
            teacher_id=request.teacher_id,
            description=request.description,
            max_students=request.max_students,
        )
        self.db.add(group)
        self.db.commit()
        return self._build_group_response(group)

    def update_group(
        self, org_id: str, group_id: str, request: GroupUpdateRequest
    ) -> GroupResponse:
        group = (
            self.db.query(Group)
            .filter(Group.id == group_id, Group.org_id == org_id)
            .first()
        )
        if not group:
            raise ValueError("Группа не найдена")

        for key, value in request.model_dump(exclude_none=True).items():
            if hasattr(group, key):
                setattr(group, key, value)

        self.db.commit()
        return self._build_group_response(group)

    def list_groups(
        self,
        org_id: str,
        teacher_id: Optional[str] = None,
        class_level: Optional[int] = None,
    ) -> list[GroupResponse]:
        # No status filter here — the frontend has an "Архив" tab that needs
        # archived groups back too; it filters by status client-side.
        query = self.db.query(Group).filter(Group.org_id == org_id)
        if teacher_id:
            query = query.filter(Group.teacher_id == teacher_id)
        if class_level:
            query = query.filter(Group.class_level == class_level)

        groups = query.order_by(Group.name).all()
        return [self._build_group_response(g) for g in groups]

    def get_group_detail(self, org_id: str, group_id: str) -> dict:
        group = (
            self.db.query(Group)
            .filter(Group.id == group_id, Group.org_id == org_id)
            .first()
        )
        if not group:
            raise ValueError("Группа не найдена")

        memberships = (
            self.db.query(StudentGroup)
            .filter(StudentGroup.group_id == group_id, StudentGroup.is_active)
            .all()
        )
        student_ids = [m.student_id for m in memberships]
        students_by_id = {
            s.id: s
            for s in self.db.query(Student).filter(Student.id.in_(student_ids)).all()
        } if student_ids else {}
        latest_results = self._latest_diagnostic_results(student_ids)

        students_out = []
        for m in memberships:
            s = students_by_id.get(m.student_id)
            if not s:
                continue
            r = latest_results.get(s.id)
            theta = float(r.final_theta) if r and r.final_theta is not None else None
            students_out.append({
                "id": str(s.id),
                "first_name": s.first_name,
                "last_name": s.last_name,
                "full_name": f"{s.last_name} {s.first_name}",
                "class_level": s.class_level,
                "status": s.status,
                "student_phone": s.student_phone,
                "joined_at": m.joined_at.isoformat() if m.joined_at else None,
                "last_theta": theta,
                "last_level": _theta_to_level(theta) if theta is not None else None,
                "last_diagnosed_at": r.diagnosed_at.isoformat() if r and r.diagnosed_at else None,
            })

        result = self._build_group_response(group).model_dump()
        result["students"] = students_out
        return result

    def get_group_stats(self, org_id: str, group_id: str) -> dict:
        group = (
            self.db.query(Group)
            .filter(Group.id == group_id, Group.org_id == org_id)
            .first()
        )
        if not group:
            raise ValueError("Группа не найдена")

        student_ids = [
            m.student_id
            for m in self.db.query(StudentGroup)
            .filter(StudentGroup.group_id == group_id, StudentGroup.is_active)
            .all()
        ]
        total_students = len(student_ids)
        latest_results = self._latest_diagnostic_results(student_ids)
        thetas = [
            float(r.final_theta)
            for r in latest_results.values()
            if r.final_theta is not None
        ]

        dist = {"below_grade": 0, "at_grade": 0, "above_grade": 0}
        for t in thetas:
            dist[_theta_to_level(t)] += 1

        return {
            "group_id": str(group_id),
            "total_students": total_students,
            "diagnosed_count": len(thetas),
            "undiagnosed_count": total_students - len(thetas),
            "avg_theta": (sum(thetas) / len(thetas)) if thetas else None,
            "theta_distribution": dist,
            # Aggregated weak-topic analysis isn't built yet (would need to
            # roll up identified_gaps/root_gaps across every student) — an
            # empty list is a valid, honest state the UI already handles.
            "weak_skills": [],
        }

    def _latest_diagnostic_results(
        self, student_ids: list
    ) -> dict:
        """Most recent StudentDiagnosticResult per student_id."""
        if not student_ids:
            return {}
        results = (
            self.db.query(StudentDiagnosticResult)
            .filter(StudentDiagnosticResult.student_id.in_(student_ids))
            .order_by(StudentDiagnosticResult.diagnosed_at.desc())
            .all()
        )
        latest: dict = {}
        for r in results:
            if r.student_id not in latest:
                latest[r.student_id] = r
        return latest

    def bulk_add_students(
        self, org_id: str, group_id: str, student_ids: list[str]
    ) -> dict:
        added = 0
        skipped = 0
        for student_id in student_ids:
            try:
                self.add_student_to_group(org_id, group_id, student_id)
                added += 1
            except ValueError:
                skipped += 1
        return {"group_id": str(group_id), "added": added, "skipped": skipped}

    def add_student_to_group(self, org_id: str, group_id: str, student_id: str) -> dict:
        group = (
            self.db.query(Group)
            .filter(Group.id == group_id, Group.org_id == org_id)
            .first()
        )
        if not group:
            raise ValueError("Группа не найдена")
        student = (
            self.db.query(Student)
            .filter(Student.id == student_id, Student.org_id == org_id)
            .first()
        )
        if not student:
            raise ValueError("Ученик не найден")

        # Проверить дубликат
        existing = (
            self.db.query(StudentGroup)
            .filter(
                StudentGroup.student_id == student_id,
                StudentGroup.group_id == group_id,
                StudentGroup.is_active,
            )
            .first()
        )
        if existing:
            raise ValueError("Ученик уже в этой группе")

        sg = StudentGroup(student_id=student_id, group_id=group_id)
        self.db.add(sg)

        # Обновить основную группу в student если нет
        if not student.group_id:
            student.group_id = group_id

        if student.status in ("new", "diagnosed"):
            student.status = "active"

        self.db.commit()
        return {
            "student_id": str(student_id),
            "group_id": str(group_id),
            "status": "added",
        }

    def remove_student_from_group(
        self, org_id: str, group_id: str, student_id: str
    ) -> bool:
        sg = (
            self.db.query(StudentGroup)
            .filter(
                StudentGroup.student_id == student_id,
                StudentGroup.group_id == group_id,
                StudentGroup.is_active,
            )
            .first()
        )
        if not sg:
            raise ValueError("Ученик не в этой группе")
        sg.is_active = False
        sg.left_at = datetime.now(timezone.utc)
        self.db.commit()
        return True

    def _build_group_response(self, group: Group) -> GroupResponse:
        student_count = (
            self.db.query(func.count(StudentGroup.id))
            .filter(StudentGroup.group_id == group.id, StudentGroup.is_active)
            .scalar()
        ) or 0

        teacher_name = None
        if group.teacher_id:
            teacher = self.db.query(User).filter(User.id == group.teacher_id).first()
            if teacher:
                teacher_name = f"{teacher.first_name} {teacher.last_name}"

        return GroupResponse(
            id=str(group.id),
            org_id=str(group.org_id),
            name=group.name,
            class_level=group.class_level,
            teacher_id=str(group.teacher_id) if group.teacher_id else None,
            teacher_name=teacher_name,
            status=group.status or "active",
            max_students=group.max_students or 30,
            student_count=student_count,
            created_at=group.created_at.isoformat() if group.created_at else None,
        )

    # ═══════════════════════════════════════════════════════════════════
    # 9. ORG MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════

    def get_organization(self, org_id: str) -> OrgResponse:
        org = self.db.query(Organization).filter(Organization.id == org_id).first()
        if not org:
            raise ValueError("Организация не найдена")
        return self._build_org_response(org)

    def update_organization(self, org_id: str, **kwargs) -> OrgResponse:
        org = self.db.query(Organization).filter(Organization.id == org_id).first()
        if not org:
            raise ValueError("Организация не найдена")
        for key, value in kwargs.items():
            if value is not None and hasattr(org, key):
                setattr(org, key, value)
        self.db.commit()
        return self._build_org_response(org)

    def get_org_stats(self, org_id: str) -> dict:
        """Краткая статистика организации для dashboard"""
        student_count = (
            self.db.query(func.count(Student.id))
            .filter(Student.org_id == org_id)
            .scalar()
            or 0
        )
        group_count = (
            self.db.query(func.count(Group.id))
            .filter(Group.org_id == org_id, Group.status == "active")
            .scalar()
            or 0
        )
        member_count = (
            self.db.query(func.count(OrganizationMember.id))
            .filter(
                OrganizationMember.org_id == org_id,
                OrganizationMember.is_active,
            )
            .scalar()
            or 0
        )

        active_diagnostics = (
            self.db.query(func.count(DiagnosticInvite.id))
            .filter(
                DiagnosticInvite.org_id == org_id,
                DiagnosticInvite.status.in_(["pending", "confirmed", "in_progress"]),
            )
            .scalar()
        ) or 0

        completed_diagnostics = (
            self.db.query(func.count(DiagnosticInvite.id))
            .filter(
                DiagnosticInvite.org_id == org_id,
                DiagnosticInvite.status == "completed",
            )
            .scalar()
        ) or 0

        new_students = (
            self.db.query(func.count(Student.id))
            .filter(Student.org_id == org_id, Student.status == "new")
            .scalar()
        ) or 0

        return {
            "total_students": student_count,
            "total_groups": group_count,
            "total_members": member_count,
            "active_diagnostics": active_diagnostics,
            "completed_diagnostics": completed_diagnostics,
            "new_students": new_students,
        }

    def list_users(self, role: Optional[str] = None, limit: int = 50, offset: int = 0):
        """Backward compatible — superadmin list"""
        query = self.db.query(User).filter(User.is_active)
        if role:
            query = query.filter(User.role == role)
        return query.order_by(User.created_at.desc()).offset(offset).limit(limit).all()
