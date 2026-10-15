"""
ALGO V1 - Auth Service
B2B: Organizations → Members (owner/admin/teacher) → Students (no login)
JWT, bcrypt, org CRUD, invite flow, student management, diagnostic invites
"""

import os
import hashlib
import secrets
import string
import httpx
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4
import logging
from zoneinfo import ZoneInfo

from jose import jwt, JWTError
from passlib.context import CryptContext
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy import func, or_

from . import student_identity
from .student_id_service import assign_display_id
from .attendance_excuses import AttendanceExcuseService
from .diagnostic_level import level_fields, normalize_level_payload
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
    GroupAttendance,
    GroupAttendanceAudit,
    GroupAudit,
    GroupScheduleOverride,
    GroupScheduleOneOffLesson,
    GroupScheduleAudit,
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
    ScheduleLessonResponse,
    ScheduleOccurrenceResponse,
    CreateLessonRequest,
    CreateOneOffLessonRequest,
    UpdateLessonRequest,
    RescheduleLessonRequest,
    ReviseRecurringLessonRequest,
    EndRecurringLessonRequest,
    RecurringLessonEndImpactResponse,
    EndRecurringLessonResponse,
    StartAttendanceRequest,
    SaveAttendanceRequest,
    ReconcileMissedLessonRequest,
    GroupAttendanceResponse,
    OpenAttendanceActionResponse,
    TeacherDashboardLessonAttendanceStateResponse,
    TeacherDashboardSummaryResponse,
    LessonAttendanceStatus,
)
from src.core.config import get_settings
from src.core.email import send_admin_new_application
from src.core.exceptions import (
    AccountNotFoundError,
    InvalidCredentialsError,
    AccountInactiveError,
)

logger = logging.getLogger(__name__)

# A transfer closes one membership and opens the next one in the same request;
# the two timestamps may differ only by the time the request itself takes.
TRANSFER_WINDOW_SECONDS = 10
MEMBERSHIP_MODES = ("transfer", "add")


class MembershipConflict(ValueError):
    """The membership change contradicts the current state (HTTP 409)."""
BUSINESS_TIMEZONE = ZoneInfo(os.getenv("BUSINESS_TIMEZONE", "Asia/Tashkent"))


def missed_lesson_window_start(today: date) -> date:
    """First day a never-started lesson is still offered for follow-up.

    A stale schedule must not turn into an unbounded queue, but a rolling
    window can cut a payroll period in half. Payroll is monthly, so the window
    always covers the whole previous month and the current one: 15.10 -> 01.09,
    01.11 -> 01.10, 05.01 -> 01.12 of the year before.
    """
    first_of_month = today.replace(day=1)
    return (first_of_month - timedelta(days=1)).replace(day=1)


def lesson_start_as_naive_utc(lesson_date: date, start_minutes: int) -> datetime:
    """A lesson's start (Tashkent wall time) as the naive UTC the membership columns use.

    ``student_groups.joined_at`` / ``left_at`` are naive UTC, a lesson's time is
    business-local; comparing the two without converting shifts the roster by
    five hours around the lesson day.
    """
    local = datetime.combine(lesson_date, datetime.min.time()) + timedelta(minutes=start_minutes)
    return local.replace(tzinfo=BUSINESS_TIMEZONE).astimezone(timezone.utc).replace(tzinfo=None)


def membership_covers_instant(
    joined_at: Optional[datetime], left_at: Optional[datetime], instant: datetime
) -> bool:
    """Python twin of the SQL rule in ``AuthService._attendance_roster_at``.

    ``joined_at <= instant`` (a missing ``joined_at`` counts) and
    ``left_at is None or left_at > instant``; all naive UTC.
    """
    return (joined_at is None or joined_at <= instant) and (left_at is None or left_at > instant)


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
_EXPIRABLE_DIAGNOSTIC_STATUSES = ("pending", "confirmed", "in_progress")

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


def _completion_counts(result_data: dict) -> tuple:
    """(correct, total, accuracy) from a completion payload; None when absent."""
    correct_cnt = next(
        (
            result_data[key]
            for key in ("correct_answers", "correct_count", "questions_correct")
            if result_data.get(key) is not None
        ),
        None,
    )
    total_cnt = next(
        (
            result_data[key]
            for key in ("total_items", "total_tasks", "total_questions")
            if result_data.get(key) is not None
        ),
        None,
    )
    accuracy_val = result_data.get("accuracy")
    if accuracy_val is None and total_cnt and correct_cnt is not None:
        accuracy_val = round(float(correct_cnt) / float(total_cnt), 4)
    return correct_cnt, total_cnt, accuracy_val


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


class _SchedulePreload:
    """Per-group one-off lessons and schedule overrides loaded in bulk."""

    def __init__(self) -> None:
        self.one_offs: Dict[Any, List[GroupScheduleOneOffLesson]] = {}
        self.overrides: Dict[Any, List[GroupScheduleOverride]] = {}


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
            # A removed staff member must not be able to mint a fresh access
            # token with a previously issued refresh cookie.
            user.refresh_token_hash = None

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
        requested_group_id = update_data.pop("group_id", None)
        if (
            requested_group_id is not None
            and str(requested_group_id) != str(student.group_id or "")
        ):
            # The generic profile edit is deliberately not a transfer API:
            # it has no target-capacity, live-lesson or roster-history guards.
            # All group changes must go through add_student_to_group.
            raise ValueError(
                "Для перевода используйте действие «Перевести в другую группу»"
            )
        for key, value in update_data.items():
            if hasattr(student, key):
                setattr(student, key, value)

        if student.group_id or student.status == "active":
            if not student.display_id:
                assign_display_id(self.db, org_id, student)

        self.db.commit()
        return self._build_student_response(student)

    def _require_student_visibility(
        self,
        org_id: str,
        student_id: str,
        *,
        actor_id: Optional[str],
        is_org_admin: bool,
    ) -> Student:
        """Return a student only when the staff actor may view their data.

        Owner/admin may view every student in their organisation.  A teacher
        may view only a current member of a group assigned to that teacher.
        This guard is shared by profile and diagnostic-report integrations.
        """
        student = (
            self.db.query(Student)
            .filter(Student.id == student_id, Student.org_id == org_id)
            .first()
        )
        if not student:
            raise ValueError("Ученик не найден")
        if is_org_admin:
            return student
        if not actor_id:
            raise PermissionError("Нет доступа к данным ученика")

        is_primary_group_teacher = bool(
            student.group_id
            and self.db.query(Group.id)
            .filter(Group.id == student.group_id, Group.teacher_id == actor_id)
            .first()
        )
        is_current_group_teacher = bool(
            self.db.query(StudentGroup.id)
            .join(Group, Group.id == StudentGroup.group_id)
            .filter(
                StudentGroup.student_id == student.id,
                StudentGroup.is_active.is_(True),
                StudentGroup.left_at.is_(None),
                Group.teacher_id == actor_id,
            )
            .first()
        )
        if not (is_primary_group_teacher or is_current_group_teacher):
            raise PermissionError("Нет доступа к данным ученика")
        return student

    def get_student(
        self,
        org_id: str,
        student_id: str,
        *,
        actor_id: Optional[str],
        is_org_admin: bool,
    ) -> StudentResponse:
        student = self._require_student_visibility(
            org_id, student_id, actor_id=actor_id, is_org_admin=is_org_admin
        )
        return self._build_student_response(student)

    def get_student_profile(
        self,
        org_id: str,
        student_id: str,
        *,
        actor_id: Optional[str],
        is_org_admin: bool,
    ) -> dict:
        self._require_student_visibility(
            org_id, student_id, actor_id=actor_id, is_org_admin=is_org_admin
        )
        student = (
            self.db.query(Student)
            .options(
                joinedload(Student.primary_group).joinedload(Group.teacher)
            )
            .filter(Student.id == student_id, Student.org_id == org_id)
            .first()
        )
        if not student:
            raise ValueError("Ученик не найден")

        # 1. Ensure display_id
        if (student.group_id or student.status == "active") and not student.display_id:
            assign_display_id(self.db, org_id, student)
            self.db.commit()

        # 2. Current group & teacher
        primary_group = student.primary_group
        group_info = None
        if primary_group:
            teacher_name = None
            if primary_group.teacher:
                teacher_name = f"{primary_group.teacher.first_name} {primary_group.teacher.last_name}".strip()
            group_info = {
                "id": str(primary_group.id),
                "name": primary_group.name,
                "class_level": primary_group.class_level,
                "teacher_id": str(primary_group.teacher_id) if primary_group.teacher_id else None,
                "teacher_name": teacher_name,
                "description": primary_group.description,
            }

        # 3. Diagnostic history
        invites = (
            self.db.query(DiagnosticInvite)
            .filter(
                DiagnosticInvite.student_id == student_id,
                DiagnosticInvite.org_id == org_id,
            )
            .order_by(DiagnosticInvite.created_at.desc())
            .all()
        )
        results = (
            self.db.query(StudentDiagnosticResult)
            .filter(
                StudentDiagnosticResult.student_id == student_id,
                StudentDiagnosticResult.org_id == org_id,
            )
            .order_by(StudentDiagnosticResult.diagnosed_at.desc())
            .all()
        )
        results_list = []
        results_by_invite = {}
        for r in results:
            theta = float(r.final_theta) if r.final_theta is not None else None
            gaps = r.identified_gaps if isinstance(r.identified_gaps, list) else []

            res_obj = {
                "id": str(r.id),
                "invite_id": str(r.invite_id) if r.invite_id else None,
                "session_id": str(r.session_id) if r.session_id else None,
                "theta": theta,
                "accuracy": float(r.accuracy) if r.accuracy is not None else None,
                **level_fields(r),
                "total_questions": r.total_tasks,
                "correct_answers": r.correct_answers,
                "duration_seconds": int((r.total_duration_ms or 0) / 1000) if r.total_duration_ms else None,
                "diagnosed_at": r.diagnosed_at.isoformat() if r.diagnosed_at else None,
                "identified_gaps": gaps,
            }
            results_list.append(res_obj)
            if r.invite_id:
                results_by_invite[str(r.invite_id)] = res_obj

        diagnostics_list = []
        for inv in invites:
            matching_res = results_by_invite.get(str(inv.id))
            if matching_res:
                matching_res = dict(matching_res)

            if matching_res:
                # Derive duration only from this invite's real timestamps.
                # Do not invent a default duration/question count or borrow
                # another attempt's result: that would make the profile and
                # a future re-diagnostic silently point to the wrong attempt.
                if not matching_res.get("duration_seconds") and inv.started_at and inv.completed_at:
                    matching_res["duration_seconds"] = max(60, int((inv.completed_at - inv.started_at).total_seconds()))

                total_q = matching_res.get("total_questions")
                if matching_res.get("correct_answers") is None and total_q:
                    acc = matching_res.get("accuracy")
                    if acc is not None:
                        matching_res["correct_answers"] = int(round(acc * total_q))

                level = matching_res.get("recommended_level")
                if level == "below_grade":
                    matching_res["recommended_load"] = "Усиленный курс (3–4 ч/нед) для закрытия пробелов"
                elif level == "above_grade":
                    matching_res["recommended_load"] = "Продвинутый курс (2 ч/нед) с углублённой программой"
                else:
                    matching_res["recommended_load"] = "Стандартный курс (2–3 ч/нед) по программе класса"

            diagnostics_list.append({
                "invite_id": str(inv.id),
                "code": inv.code,
                "class_level": inv.class_level,
                "diagnostic_type": inv.diagnostic_type,
                "status": inv.status,
                "session_id": str(inv.cat_session_id) if inv.cat_session_id else None,
                "created_at": inv.created_at.isoformat() if inv.created_at else None,
                "started_at": inv.started_at.isoformat() if inv.started_at else None,
                "completed_at": inv.completed_at.isoformat() if inv.completed_at else None,
                "result": matching_res,
            })

        # 4. Group memberships history
        memberships = (
            self.db.query(StudentGroup)
            .options(joinedload(StudentGroup.group))
            .filter(StudentGroup.student_id == student_id)
            .order_by(StudentGroup.joined_at.desc())
            .all()
        )
        group_history = []
        for m in memberships:
            group_history.append({
                "group_id": str(m.group_id),
                "group_name": m.group.name if m.group else "—",
                "class_level": m.group.class_level if m.group else None,
                "is_active": m.is_active,
                "joined_at": m.joined_at.isoformat() if m.joined_at else None,
                "left_at": m.left_at.isoformat() if m.left_at else None,
            })

        # 5. Every active membership (primary first); no money here.
        group_rows = (
            self.db.query(StudentGroup, Group, User)
            .join(Group, Group.id == StudentGroup.group_id)
            .outerjoin(User, User.id == Group.teacher_id)
            .filter(
                StudentGroup.student_id == student_id,
                StudentGroup.is_active.is_(True),
                Group.org_id == org_id,
            )
            .order_by(StudentGroup.joined_at, StudentGroup.id)
            .all()
        )
        groups_list = [
            {
                "membership_id": str(m.id),
                "group_id": str(g.id),
                "group_name": g.name,
                "teacher_id": str(g.teacher_id) if g.teacher_id else None,
                "teacher_name": (
                    f"{t.first_name} {t.last_name}".strip() if t is not None else None
                ),
                "class_level": g.class_level,
                "is_primary": str(student.group_id or "") == str(g.id),
                "joined_at": m.joined_at.isoformat() if m.joined_at else None,
            }
            for m, g, t in group_rows
        ]
        groups_list.sort(key=lambda item: not item["is_primary"])  # stable: primary first

        return {
            "student": self._build_student_response(student).model_dump(),
            "group": group_info,
            "groups": groups_list,
            "group_history": group_history,
            "diagnostics": diagnostics_list,
            "diagnostic_results": results_list,
            "exams": [],
        }

    def get_student_attendance_summary(
        self,
        org_id: str,
        student_id: str,
        *,
        month: Optional[str] = None,
    ) -> dict[str, Any]:
        """Return one student's attendance across every group for one month.

        Attendance belongs to a concrete lesson and its frozen roster, not to
        the student's current group.  This is why a mid-month transfer keeps
        the four lessons in the old group and starts counting in the new one
        only from the first lesson after the transfer.
        """
        student = (
            self.db.query(Student)
            .filter(Student.id == student_id, Student.org_id == org_id)
            .first()
        )
        if not student:
            raise ValueError("Ученик не найден")

        selected_month = month or datetime.now(BUSINESS_TIMEZONE).strftime("%Y-%m")
        try:
            period_start = datetime.strptime(selected_month + "-01", "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("month должен иметь формат YYYY-MM") from exc
        if period_start.strftime("%Y-%m") != selected_month:
            raise ValueError("month должен иметь формат YYYY-MM")
        period_end = (
            date(period_start.year + 1, 1, 1)
            if period_start.month == 12
            else date(period_start.year, period_start.month + 1, 1)
        ) - timedelta(days=1)

        attendance_rows = (
            self.db.query(GroupAttendance, Group)
            .join(Group, Group.id == GroupAttendance.group_id)
            .filter(
                GroupAttendance.org_id == org_id,
                GroupAttendance.lesson_date >= period_start.isoformat(),
                GroupAttendance.lesson_date <= period_end.isoformat(),
                GroupAttendance.status != LessonAttendanceStatus.CANCELLED.value,
            )
            .order_by(GroupAttendance.lesson_date, GroupAttendance.created_at)
            .all()
        )
        teacher_ids = {group.teacher_id for _, group in attendance_rows if group.teacher_id}
        teachers_by_id = {
            teacher.id: teacher
            for teacher in self.db.query(User).filter(User.id.in_(teacher_ids)).all()
        } if teacher_ids else {}

        tracked_statuses = {"present", "late", "absent", "excused"}
        counts = {status: 0 for status in tracked_statuses}
        not_marked = 0
        groups: dict[str, dict[str, Any]] = {}
        lessons: list[dict[str, Any]] = []
        student_key = str(student.id)

        for attendance, group in attendance_rows:
            records = dict(attendance.records or {})
            roster_ids = self._roster_ids_from_snapshot(attendance.roster_snapshot)
            # Legacy rows did not have a frozen roster. A record is still a
            # durable fact, but absence from such a row must not be guessed.
            if roster_ids and student_key not in roster_ids:
                continue
            if not roster_ids and student_key not in records:
                continue

            status = records.get(student_key)
            if status not in tracked_statuses:
                status = "not_marked"
                not_marked += 1
            else:
                counts[status] += 1

            group_key = str(group.id)
            if group_key not in groups:
                teacher = teachers_by_id.get(group.teacher_id)
                groups[group_key] = {
                    "group_id": group_key,
                    "group_name": group.name,
                    "class_level": group.class_level,
                    "teacher_name": (
                        f"{teacher.first_name} {teacher.last_name}".strip()
                        if teacher
                        else None
                    ),
                    "total_lessons": 0,
                    "present": 0,
                    "late": 0,
                    "absent": 0,
                    "excused": 0,
                    "not_marked": 0,
                }
            group_summary = groups[group_key]
            group_summary["total_lessons"] += 1
            group_summary[status] += 1
            lessons.append({
                "lesson_id": attendance.lesson_id,
                "date": attendance.lesson_date,
                "topic": attendance.topic or "Занятие",
                "group_id": group_key,
                "group_name": group.name,
                "status": status,
                "attendance_status": attendance.status,
            })

        for group_summary in groups.values():
            accountable = (
                group_summary["present"]
                + group_summary["late"]
                + group_summary["absent"]
            )
            group_summary["attendance_rate"] = (
                round(
                    (group_summary["present"] + group_summary["late"])
                    / accountable
                    * 100,
                    1,
                )
                if accountable
                else None
            )

        accountable_total = counts["present"] + counts["late"] + counts["absent"]
        return {
            "student_id": str(student.id),
            "period": {
                "month": selected_month,
                "date_from": period_start.isoformat(),
                "date_to": period_end.isoformat(),
            },
            "totals": {
                "total_lessons": len(lessons),
                **counts,
                "not_marked": not_marked,
                "attended": counts["present"] + counts["late"],
                "attendance_rate": (
                    round((counts["present"] + counts["late"]) / accountable_total * 100, 1)
                    if accountable_total
                    else None
                ),
            },
            "groups": sorted(groups.values(), key=lambda item: item["group_name"].lower()),
            "lessons": lessons,
        }

    def _students_query(
        self,
        org_id: str,
        class_level: Optional[int] = None,
        group_id: Optional[str] = None,
        has_group: Optional[bool] = None,
        status: Optional[str] = None,
        exclude_status: Optional[str] = None,
        search: Optional[str] = None,
        has_primary_group: Optional[bool] = None,
        *,
        actor_id: Optional[str],
        is_org_admin: bool,
    ):
        """Filtered student query, or ``None`` when the actor can see nobody."""
        query = self.db.query(Student).filter(Student.org_id == org_id)

        if not is_org_admin:
            if not actor_id:
                return None
            taught_group_ids = [
                group_id
                for (group_id,) in self.db.query(Group.id)
                .filter(Group.org_id == org_id, Group.teacher_id == actor_id)
                .all()
            ]
            if not taught_group_ids:
                return None
            active_member_ids = (
                self.db.query(StudentGroup.student_id)
                .filter(
                    StudentGroup.group_id.in_(taught_group_ids),
                    StudentGroup.is_active.is_(True),
                    StudentGroup.left_at.is_(None),
                )
            )
            query = query.filter(
                or_(
                    Student.group_id.in_(taught_group_ids),
                    Student.id.in_(active_member_ids),
                )
            )

        if class_level:
            query = query.filter(Student.class_level == class_level)
        if group_id:
            # Any active membership counts: a student of two groups is found
            # by the filter of either one.
            query = query.filter(
                Student.id.in_(
                    self.db.query(StudentGroup.student_id).filter(
                        StudentGroup.group_id == group_id,
                        StudentGroup.is_active.is_(True),
                    )
                )
            )
        if has_primary_group is True:
            query = query.filter(Student.group_id.isnot(None))
        if has_group is True:
            active_memberships = self.db.query(StudentGroup.student_id).filter(StudentGroup.is_active == True)
            query = query.filter(
                (Student.group_id.isnot(None)) | (Student.id.in_(active_memberships))
            )
        elif has_group is False:
            active_memberships = self.db.query(StudentGroup.student_id).filter(StudentGroup.is_active == True)
            query = query.filter(
                Student.group_id.is_(None),
                ~Student.id.in_(active_memberships),
            )
        if status:
            query = query.filter(Student.status == status)
        if exclude_status:
            query = query.filter(Student.status != exclude_status)
        if search:
            # Every word must match the first or last name, so a full-name
            # query such as "Иван Петров" finds the student too.
            for word in search.split():
                pattern = f"%{word}%"
                query = query.filter(
                    (Student.first_name.ilike(pattern)) | (Student.last_name.ilike(pattern))
                )
        return query

    def list_students(
        self,
        org_id: str,
        class_level: Optional[int] = None,
        group_id: Optional[str] = None,
        has_group: Optional[bool] = None,
        status: Optional[str] = None,
        exclude_status: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        has_primary_group: Optional[bool] = None,
        *,
        actor_id: Optional[str],
        is_org_admin: bool,
    ) -> list[StudentResponse]:
        return self.list_students_page(
            org_id,
            class_level=class_level,
            group_id=group_id,
            has_group=has_group,
            status=status,
            exclude_status=exclude_status,
            search=search,
            limit=limit,
            offset=offset,
            has_primary_group=has_primary_group,
            actor_id=actor_id,
            is_org_admin=is_org_admin,
        )[0]

    def list_students_page(
        self,
        org_id: str,
        class_level: Optional[int] = None,
        group_id: Optional[str] = None,
        has_group: Optional[bool] = None,
        status: Optional[str] = None,
        exclude_status: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        has_primary_group: Optional[bool] = None,
        *,
        actor_id: Optional[str],
        is_org_admin: bool,
    ) -> tuple[list[StudentResponse], int]:
        """One page of students plus the exact total of the filtered set."""
        query = self._students_query(
            org_id,
            class_level=class_level,
            group_id=group_id,
            has_group=has_group,
            status=status,
            exclude_status=exclude_status,
            search=search,
            has_primary_group=has_primary_group,
            actor_id=actor_id,
            is_org_admin=is_org_admin,
        )
        if query is None:
            return [], 0
        total = query.count()
        students = (
            query.order_by(Student.created_at.desc(), Student.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        counts: dict = {}
        if students:
            # One grouped query for the whole page: a count, never money.
            counts = dict(
                self.db.query(StudentGroup.student_id, func.count(StudentGroup.id))
                .filter(
                    StudentGroup.student_id.in_([s.id for s in students]),
                    StudentGroup.is_active.is_(True),
                )
                .group_by(StudentGroup.student_id)
                .all()
            )
        items = []
        for s in students:
            item = self._build_student_response(s)
            item.groups_count = int(counts.get(s.id, 0))
            items.append(item)
        return items, total

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
            display_id=student.display_id,
            sequence_number=student.sequence_number,
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

    @staticmethod
    def _diagnostic_invite_is_expired(
        invite: DiagnosticInvite, now: Optional[datetime] = None
    ) -> bool:
        """Whether an unfinished invite has passed its hard expiry time."""
        if invite.status not in _EXPIRABLE_DIAGNOSTIC_STATUSES or not invite.expires_at:
            return False
        expires_at = (
            invite.expires_at
            if invite.expires_at.tzinfo
            else invite.expires_at.replace(tzinfo=timezone.utc)
        )
        return expires_at < (now or datetime.now(timezone.utc))

    def _expire_diagnostic_invite_if_due(self, invite: DiagnosticInvite) -> bool:
        """Persist expiry before returning an invite through any read path."""
        if not self._diagnostic_invite_is_expired(invite):
            return False
        invite.status = "expired"
        self.db.commit()
        logger.info("diagnostic_invite_transition invite_id=%s session_id=%s status=expired", invite.id, invite.cat_session_id)
        return True

    def _expire_due_diagnostic_invites(self, org_id: str) -> int:
        """Reconcile stale unfinished invites before organisation-level counts/lists.

        The database uses a timezone-naive ``DateTime`` column, hence UTC is
        deliberately passed as a naive value for this bulk predicate.  Public
        and detail reads use the timezone-safe helper above.
        """
        updated = (
            self.db.query(DiagnosticInvite)
            .filter(
                DiagnosticInvite.org_id == org_id,
                DiagnosticInvite.status.in_(_EXPIRABLE_DIAGNOSTIC_STATUSES),
                DiagnosticInvite.expires_at < datetime.utcnow(),
            )
            .update({"status": "expired"}, synchronize_session=False)
        )
        if updated:
            self.db.commit()
            logger.info("diagnostic_invites_expired org_id=%s count=%s", org_id, updated)
        return int(updated or 0)

    def get_diagnostic_by_code(self, code: str) -> Optional[dict]:
        """Получить диагностику по коду (публичный, без JWT)"""
        invite = (
            self.db.query(DiagnosticInvite)
            .filter(DiagnosticInvite.code == code)
            .first()
        )
        if not invite:
            return None

        self._expire_diagnostic_invite_if_due(invite)

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
            invite.confirmed_at = datetime.now(timezone.utc)
            self.db.commit()
            logger.info("diagnostic_invite_transition invite_id=%s status=confirmed", invite.id)

        return {
            "id": str(invite.id),
            "status": "confirmed",
            "class_level": invite.class_level,
            "student_id": str(invite.student_id),
            "org_id": str(invite.org_id),
        }

    def start_diagnostic(self, code: str, cat_session_id: Optional[str] = None) -> dict:
        """Atomically claim an invite for exactly one diagnostic session.

        Retrying the same callback is safe.  A different session may never
        replace an already bound ``cat_session_id`` — that would make one
        invite point at two attempts and corrupt the report/placement flow.
        """
        if not cat_session_id:
            raise ValueError("Не передан идентификатор сессии диагностики")
        try:
            requested_session_id = UUID(str(cat_session_id))
        except (TypeError, ValueError) as exc:
            raise ValueError("Некорректный идентификатор сессии диагностики") from exc

        invite = (
            self.db.query(DiagnosticInvite)
            .filter(
                DiagnosticInvite.code == code,
                DiagnosticInvite.status.in_(["confirmed", "in_progress"]),
            )
            .with_for_update()
            .first()
        )
        if not invite:
            raise ValueError("Диагностика не найдена или уже завершена")

        if invite.expires_at:
            expires_at = (
                invite.expires_at
                if invite.expires_at.tzinfo
                else invite.expires_at.replace(tzinfo=timezone.utc)
            )
            if expires_at < datetime.now(timezone.utc):
                invite.status = "expired"
                self.db.commit()
                raise ValueError("Ссылка на диагностику истекла")

        if invite.status == "in_progress":
            if invite.cat_session_id and invite.cat_session_id != requested_session_id:
                raise ValueError("По этому приглашению уже запущена другая сессия диагностики")
        else:
            invite.status = "in_progress"
            invite.started_at = datetime.now(timezone.utc)
        invite.cat_session_id = requested_session_id
        self.db.commit()
        logger.info("diagnostic_invite_transition invite_id=%s session_id=%s status=in_progress", invite.id, requested_session_id)

        return {
            "id": str(invite.id),
            "status": "in_progress",
            "session_id": str(invite.cat_session_id),
        }

    def complete_diagnostic(self, code: str, result_data: dict) -> dict:
        """Завершение диагностики — сохранение результатов"""
        session_value = result_data.get("session_id")
        if not session_value:
            raise ValueError("Не передан идентификатор завершённой сессии")
        try:
            completed_session_id = UUID(str(session_value))
        except (TypeError, ValueError) as exc:
            raise ValueError("Некорректный идентификатор завершённой сессии") from exc

        invite = (
            self.db.query(DiagnosticInvite)
            .filter(
                DiagnosticInvite.code == code,
                DiagnosticInvite.status.in_(["confirmed", "in_progress", "completed", "expired"]),
            )
            .with_for_update()
            .first()
        )
        if not invite:
            raise ValueError("Диагностика не найдена или не начата")

        if invite.cat_session_id is None and invite.status in {"confirmed", "expired"}:
            # The engine can have durably started before expiry while the
            # start callback was unavailable. Only the authenticated internal
            # completion callback may repair that projection, using its
            # database creation time and the confirmed invite's time bounds.
            try:
                started_at = datetime.fromisoformat(str(result_data.get("session_started_at") or "").replace("Z", "+00:00"))
                started_at = started_at.replace(tzinfo=timezone.utc) if started_at.tzinfo is None else started_at
                confirmed_at = invite.confirmed_at
                if confirmed_at and confirmed_at.tzinfo is None:
                    confirmed_at = confirmed_at.replace(tzinfo=timezone.utc)
                expires_at = invite.expires_at
                if expires_at and expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if not confirmed_at or started_at < confirmed_at or started_at > datetime.now(timezone.utc):
                    raise ValueError("Unconfirmed start")
                if expires_at and started_at >= expires_at:
                    raise ValueError("Start after expiry")
            except (TypeError, ValueError) as exc:
                raise ValueError("Не подтверждён допустимый старт сессии диагностики") from exc
            invite.cat_session_id = completed_session_id
            invite.started_at = started_at

        if invite.cat_session_id != completed_session_id:
            raise ValueError("Сессия не соответствует приглашению на диагностику")

        existing_result = (
            self.db.query(StudentDiagnosticResult)
            .filter(StudentDiagnosticResult.invite_id == invite.id)
            .order_by(StudentDiagnosticResult.diagnosed_at.desc())
            .first()
        )
        if invite.status == "completed":
            if existing_result and existing_result.session_id == completed_session_id:
                # A re-sent completion (diagnostic-side resync after the report
                # was rebuilt) is authoritative: overwrite the level columns and
                # the answer counts when provided. theta, se, id and
                # diagnosed_at are never touched.
                correct_cnt, total_cnt, accuracy_val = _completion_counts(result_data)
                provided = {
                    key: value
                    for key, value in {
                        **normalize_level_payload(result_data),
                        "correct_answers": correct_cnt,
                        "total_tasks": total_cnt,
                        "accuracy": accuracy_val,
                    }.items()
                    if value is not None
                }
                if provided:
                    for key, value in provided.items():
                        setattr(existing_result, key, value)
                    self.db.commit()
                return {
                    "id": str(existing_result.id),
                    "status": "completed",
                    "student_id": str(invite.student_id),
                    "org_id": str(invite.org_id),
                }
            raise ValueError("Диагностика уже завершена другой сессией")

        invite.status = "completed"
        invite.completed_at = datetime.now(timezone.utc)

        # Сохранить результат
        correct_cnt, total_cnt, accuracy_val = _completion_counts(result_data)

        result = StudentDiagnosticResult(
            student_id=invite.student_id,
            org_id=invite.org_id,
            invite_id=invite.id,
            session_id=completed_session_id,
            diagnostic_type=invite.diagnostic_type,
            final_theta=result_data.get("final_theta"),
            se=result_data.get("se"),
            accuracy=accuracy_val,
            total_tasks=total_cnt,
            correct_answers=correct_cnt,
            identified_gaps=result_data.get("identified_gaps"),
            root_gaps=result_data.get("root_gaps"),
            error_summary=result_data.get("error_summary"),
            integrity_report=result_data.get("integrity_report"),
            # recommended_level (VARCHAR(5)) is a legacy column nobody fills; the
            # level lives in overall_level / level_status below.
            recommended_level=result_data.get("recommended_level"),
            **normalize_level_payload(result_data),
            recommended_group_id=result_data.get("recommended_group_id"),
        )
        self.db.add(result)

        # Обновить статус ученика
        student = self.db.query(Student).filter(Student.id == invite.student_id).first()
        if student and student.status == "new":
            student.status = "diagnosed"

        self.db.commit()

        logger.info("diagnostic_invite_transition invite_id=%s session_id=%s status=completed", invite.id, completed_session_id)

        return {
            "id": str(result.id),
            "status": "completed",
            "student_id": str(invite.student_id),
            "org_id": str(invite.org_id),
        }

    def list_diagnostic_invites(
        self,
        org_id: str,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list, int]:
        """Список диагностик организации и точное число строк в выборке."""
        self._expire_due_diagnostic_invites(org_id)
        query = (
            self.db.query(DiagnosticInvite)
            .options(
                joinedload(DiagnosticInvite.student).joinedload(Student.primary_group)
            )
            .filter(DiagnosticInvite.org_id == org_id)
        )
        if status:
            query = query.filter(DiagnosticInvite.status == status)
        total = query.count()
        invites = (
            query
            .order_by(DiagnosticInvite.created_at.desc(), DiagnosticInvite.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        results_by_invite = self._diagnostic_results_by_invite([inv.id for inv in invites])

        result = []
        for inv in invites:
            student = inv.student
            group = student.primary_group if student else None
            group_id = str(student.group_id) if (student and student.group_id) else None
            group_name = group.name if group else None
            is_assigned = bool(student and (student.group_id is not None or student.status == "active"))

            r = results_by_invite.get(inv.id)
            res_dict = None
            if r:
                theta = float(r.final_theta) if r.final_theta is not None else None
                res_dict = {
                    "theta": theta,
                    "accuracy": float(r.accuracy) if r.accuracy is not None else None,
                    **level_fields(r),
                    "total_questions": r.total_tasks,
                    "correct_answers": r.correct_answers,
                }

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
                    "session_id": str(r.session_id or inv.cat_session_id)
                    if r and (r.session_id or inv.cat_session_id)
                    else str(inv.cat_session_id) if inv.cat_session_id else None,
                    "student_status": student.status if student else None,
                    "group_id": group_id,
                    "group_name": group_name,
                    "is_assigned": is_assigned,
                    "result": res_dict,
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
        return result, total

    # ═══════════════════════════════════════════════════════════════════
    # 8. GROUPS — группы
    # ═══════════════════════════════════════════════════════════════════

    def create_group(
        self,
        org_id: str,
        request: GroupCreateRequest,
        *,
        actor_id: str,
        is_org_admin: bool,
        actor_role: Optional[str] = None,
    ) -> GroupResponse:
        """Create a group without trusting a client-supplied teacher id.

        A teacher can create a group for themself only.  Organisation managers
        retain the existing ability to create an unassigned group or assign an
        active teacher of their organisation.  The resulting assignment and
        actor are persisted in ``group_audit`` before the transaction commits.
        """
        try:
            actor_uuid = UUID(str(actor_id))
        except (TypeError, ValueError) as exc:
            raise PermissionError("Не удалось определить текущего сотрудника") from exc

        actor_membership = (
            self.db.query(OrganizationMember)
            .filter(
                OrganizationMember.org_id == org_id,
                OrganizationMember.user_id == actor_uuid,
                OrganizationMember.is_active,
            )
            .first()
        )
        if not actor_membership:
            raise PermissionError("Вы больше не состоите в этой организации")

        if is_org_admin:
            teacher_uuid = None
            if request.teacher_id:
                try:
                    teacher_uuid = UUID(str(request.teacher_id))
                except (TypeError, ValueError) as exc:
                    raise ValueError("Некорректный преподаватель") from exc
                self._require_active_group_teacher(org_id, teacher_uuid)
        else:
            # Never silently reinterpret a forged request as a safe one: a
            # teacher receives a clear forbidden response if a foreign teacher
            # was supplied, whether it came from an old UI or a crafted call.
            if request.teacher_id and str(request.teacher_id) != str(actor_uuid):
                raise PermissionError(
                    "Преподаватель может создать группу только для себя"
                )
            if actor_membership.org_role != "teacher":
                raise PermissionError("Недостаточно прав для создания группы")
            teacher_uuid = actor_uuid

        group = Group(
            org_id=org_id,
            name=request.name,
            class_level=request.class_level,
            teacher_id=teacher_uuid,
            description=request.description,
            max_students=request.max_students,
        )
        self.db.add(group)
        self.db.flush()
        self.db.add(
            GroupAudit(
                org_id=org_id,
                group_id=group.id,
                actor_id=actor_uuid,
                actor_role=actor_role or actor_membership.org_role,
                action="created",
                assigned_teacher_id=teacher_uuid,
                snapshot={
                    "name": group.name,
                    "class_level": group.class_level,
                    "description": group.description,
                    "max_students": group.max_students,
                    "status": group.status or "active",
                },
            )
        )
        self.db.commit()
        return self._build_group_response(group)

    def _require_active_group_teacher(self, org_id: str, teacher_id: UUID) -> None:
        """Ensure a manager cannot assign a group outside their organisation."""
        membership = (
            self.db.query(OrganizationMember)
            .filter(
                OrganizationMember.org_id == org_id,
                OrganizationMember.user_id == teacher_id,
                OrganizationMember.org_role == "teacher",
                OrganizationMember.is_active,
            )
            .first()
        )
        if not membership:
            raise ValueError("Выберите активного преподавателя этой организации")

    def update_group(
        self,
        org_id: str,
        group_id: str,
        request: GroupUpdateRequest,
        *,
        actor_id: str,
        actor_role: Optional[str] = None,
    ) -> GroupResponse:
        group = (
            self.db.query(Group)
            .filter(Group.id == group_id, Group.org_id == org_id)
            .first()
        )
        if not group:
            raise ValueError("Группа не найдена")

        changes = request.model_dump(exclude_none=True)
        teacher_changed = False
        if "teacher_id" in changes:
            try:
                new_teacher_id = UUID(str(changes["teacher_id"]))
            except ValueError as exc:
                raise ValueError("Некорректный преподаватель") from exc
            changes["teacher_id"] = new_teacher_id
            teacher_changed = str(new_teacher_id) != str(group.teacher_id)
            if teacher_changed:
                try:
                    self._require_active_group_teacher(org_id, new_teacher_id)
                except ValueError as exc:
                    raise ValueError("Выбранный сотрудник не преподаватель центра") from exc

        # {field: [old, new]} for the audit trail; unchanged fields are skipped
        # so a no-op save never writes a journal row.
        diff: dict[str, list[Any]] = {}
        for key, value in changes.items():
            if not hasattr(group, key):
                continue
            old_value = getattr(group, key)
            if key == "teacher_id":
                # UUIDs go to the JSONB snapshot as strings.
                old_value = str(old_value) if old_value else None
                value = str(value)
            if old_value == value:
                continue
            diff[key] = [old_value, value]
            setattr(group, key, UUID(value) if key == "teacher_id" else value)

        if diff:
            self.db.add(
                GroupAudit(
                    org_id=org_id,
                    group_id=group.id,
                    actor_id=actor_id,
                    actor_role=actor_role or "admin",
                    action="teacher_changed" if teacher_changed else "updated",
                    assigned_teacher_id=group.teacher_id,
                    snapshot=diff,
                )
            )
        self.db.commit()
        return self._build_group_response(group)

    def list_groups(
        self,
        org_id: str,
        teacher_id: Optional[str] = None,
        class_level: Optional[int] = None,
        *,
        actor_id: Optional[str] = None,
        is_org_admin: bool = False,
    ) -> list[GroupResponse]:
        # No status filter here — the frontend has an "Архив" tab that needs
        # archived groups back too; it filters by status client-side.
        query = self.db.query(Group).filter(Group.org_id == org_id)
        if not is_org_admin:
            if not actor_id:
                raise PermissionError("Нет доступа к группам")
            if teacher_id and str(teacher_id) != str(actor_id):
                raise PermissionError("Нет доступа к группам другого преподавателя")
            query = query.filter(Group.teacher_id == actor_id)
        if teacher_id:
            query = query.filter(Group.teacher_id == teacher_id)
        if class_level:
            query = query.filter(Group.class_level == class_level)

        groups = query.order_by(Group.name).all()
        return [self._build_group_response(g) for g in groups]

    def get_group_detail(
        self,
        org_id: str,
        group_id: str,
        *,
        actor_id: Optional[str] = None,
        is_org_admin: bool = False,
    ) -> dict:
        group = self._require_group_access(
            org_id,
            group_id,
            actor_id=actor_id,
            is_org_admin=is_org_admin,
        )
        group_uuid = group.id

        memberships = (
            self.db.query(StudentGroup)
            .filter(StudentGroup.group_id == group_uuid, StudentGroup.is_active)
            .all()
        )
        student_ids = [m.student_id for m in memberships]
        students_by_id = {
            s.id: s
            for s in self.db.query(Student).filter(Student.id.in_(student_ids)).all()
        } if student_ids else {}
        latest_results = self._latest_diagnostic_results(student_ids)

        students_out = []
        needs_commit = False
        for m in memberships:
            s = students_by_id.get(m.student_id)
            if not s:
                continue
            if not s.display_id:
                assign_display_id(self.db, org_id, s)
                needs_commit = True
            r = latest_results.get(s.id)
            theta = float(r.final_theta) if r and r.final_theta is not None else None
            lvl = level_fields(r) if r else level_fields(None)
            students_out.append({
                "id": str(s.id),
                "first_name": s.first_name,
                "last_name": s.last_name,
                "full_name": f"{s.last_name} {s.first_name}",
                "class_level": s.class_level,
                "status": s.status,
                "display_id": s.display_id,
                "student_phone": s.student_phone,
                "joined_at": m.joined_at.isoformat() if m.joined_at else None,
                "last_theta": theta,
                "last_level": lvl["recommended_level"],
                "last_level_status": lvl["level_status"],
                "last_class_equivalent": lvl["class_equivalent"],
                "last_level_provisional": lvl["level_provisional"],
                "last_diagnosed_at": r.diagnosed_at.isoformat() if r and r.diagnosed_at else None,
            })

        if needs_commit:
            self.db.commit()

        result = self._build_group_response(group).model_dump()
        result["students"] = students_out
        return result

    def get_org_groups_overview(self, org_id: str) -> dict:
        """Per-group diagnosed level for the owner dashboard, in three queries.

        ``avg_theta`` is the mean of the active students' latest ``final_theta``
        (the same rule as ``get_group_stats``); ``None`` when nobody of the group
        has a result.  ``diagnosed_count`` lets the caller weight the groups.

        The centre-wide ``avg_theta`` / ``diagnosed_count`` / ``student_count`` count
        every student ONCE: a student who belongs to two groups must not weigh double,
        which averaging the per-group figures would do.
        """
        groups = (
            self.db.query(Group)
            .filter(Group.org_id == org_id, Group.status == "active")
            .order_by(Group.name)
            .all()
        )
        group_ids = [g.id for g in groups]
        members: Dict[Any, list] = {}
        if group_ids:
            for group_id, student_id in (
                self.db.query(StudentGroup.group_id, StudentGroup.student_id)
                .filter(StudentGroup.group_id.in_(group_ids), StudentGroup.is_active.is_(True))
                .all()
            ):
                members.setdefault(group_id, []).append(student_id)
        student_ids = {sid for ids in members.values() for sid in ids}
        latest: Dict[Any, Any] = {}
        if student_ids:
            # Columns only: the result rows carry large JSON that is not needed here.
            for student_id, final_theta in (
                self.db.query(
                    StudentDiagnosticResult.student_id, StudentDiagnosticResult.final_theta
                )
                .filter(
                    StudentDiagnosticResult.org_id == org_id,
                    StudentDiagnosticResult.student_id.in_(student_ids),
                )
                .order_by(StudentDiagnosticResult.diagnosed_at.desc())
                .all()
            ):
                latest.setdefault(student_id, final_theta)

        rows = []
        for group in groups:
            ids = members.get(group.id, [])
            thetas = [float(latest[sid]) for sid in ids if latest.get(sid) is not None]
            rows.append({
                "group_id": str(group.id),
                "name": group.name,
                "class_level": group.class_level,
                "student_count": len(ids),
                "diagnosed_count": len(thetas),
                "avg_theta": (sum(thetas) / len(thetas)) if thetas else None,
            })
        org_thetas = [float(latest[sid]) for sid in student_ids if latest.get(sid) is not None]
        return {
            "groups": rows,
            "student_count": len(student_ids),
            "diagnosed_count": len(org_thetas),
            "avg_theta": (sum(org_thetas) / len(org_thetas)) if org_thetas else None,
        }

    def get_group_stats(
        self,
        org_id: str,
        group_id: str,
        *,
        actor_id: Optional[str] = None,
        is_org_admin: bool = False,
    ) -> dict:
        group = self._require_group_access(
            org_id,
            group_id,
            actor_id=actor_id,
            is_org_admin=is_org_admin,
        )
        group_uuid = group.id

        student_ids = [
            m.student_id
            for m in self.db.query(StudentGroup)
            .filter(StudentGroup.group_id == group_uuid, StudentGroup.is_active)
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
        for r in latest_results.values():
            if r.final_theta is None:
                continue
            level = level_fields(r)["recommended_level"]
            if level in dist:
                dist[level] += 1

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

    def verify_group_student(
        self,
        group_id: str,
        query: Optional[str] = None,
        student_id: Optional[str] = None,
        student_name: Optional[str] = None,
    ) -> dict:
        """Проверить принадлежность ученика к группе по ID/ФИО с валидацией обоих полей при их наличии."""
        group = self.db.query(Group).filter(Group.id == group_id).first()
        if not group:
            return {
                "enrolled": False,
                "group_name": "",
                "message": "Группа не найдена",
            }

        memberships = (
            self.db.query(StudentGroup)
            .filter(StudentGroup.group_id == group_id, StudentGroup.is_active)
            .all()
        )
        # Only the *current* roster: a former member (inactive membership) or
        # a stale primary-group pointer must not pass as enrolled.
        student_ids = {m.student_id for m in memberships}

        if not student_ids:
            return {
                "enrolled": False,
                "group_name": group.name,
                "message": f"В группе «{group.name}» нет зарегистрированных учеников. Обратитесь к преподавателю.",
            }

        students = (
            self.db.query(Student)
            .filter(Student.id.in_(list(student_ids)), Student.status != "archived")
            .all()
        )

        s_id_input = (student_id or "").strip()
        s_name_input = (student_name or "").strip()

        # Only a free-form query: an exact ID (or a bare number) is an ID,
        # anything else is read as a name.
        if not s_id_input and not s_name_input and query and query.strip():
            q_clean = query.strip()
            q_norm = student_identity.normalize_id(q_clean)
            if student_identity.has_exact_id(students, q_clean) or student_identity.is_digits_only(q_norm):
                s_id_input = q_clean
            else:
                s_name_input = q_clean

        def enrolled(student: Student) -> dict:
            return {
                "enrolled": True,
                "group_name": group.name,
                "student": {
                    "id": str(student.id),
                    "first_name": student.first_name,
                    "last_name": student.last_name,
                    "full_name": f"{student.last_name} {student.first_name}".strip(),
                    "display_id": student.display_id,
                    "class_level": student.class_level,
                },
            }

        def refused(message: str) -> dict:
            return {"enrolled": False, "group_name": group.name, "message": message}

        # CASE 1: Student ID is provided (and optionally the name of its owner)
        if s_id_input:
            match = student_identity.match_by_id(students, s_id_input)
            if match.student is None:
                return refused(
                    match.message
                    or f"Ученик с ID «{s_id_input}» не найден в группе «{group.name}». Пожалуйста, перепроверьте данные или обратитесь к преподавателю."
                )
            if s_name_input and not student_identity.name_matches_owner(match.student, s_name_input):
                return refused(student_identity.MSG_NAME_MISMATCH)
            return enrolled(match.student)

        # CASE 2: Only the name: both first and last name as whole words, exactly one student
        if s_name_input:
            match = student_identity.match_by_name(students, s_name_input)
            if match.student is None:
                return refused(
                    match.message
                    or student_identity.MSG_NAME_NOT_FOUND.format(name=s_name_input, group=group.name)
                )
            return enrolled(match.student)

        return refused("Пожалуйста, укажите имя (ФИО) или Student ID ученика.")

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

    def _diagnostic_results_by_invite(self, invite_ids: list) -> dict:
        """Return the result generated by each exact diagnostic invite.

        A student-level latest result is useful on the profile timeline, but
        is invalid for a registry row or a report opened from a concrete
        invite: those screens must preserve attempt identity.
        """
        if not invite_ids:
            return {}
        rows = (
            self.db.query(StudentDiagnosticResult)
            .filter(StudentDiagnosticResult.invite_id.in_(invite_ids))
            .order_by(StudentDiagnosticResult.diagnosed_at.desc())
            .all()
        )
        by_invite: dict = {}
        for row in rows:
            if row.invite_id not in by_invite:
                by_invite[row.invite_id] = row
        return by_invite

    def bulk_add_students(
        self,
        org_id: str,
        group_id: str,
        student_ids: list[str],
        *,
        mode: str = "transfer",
        monthly_fee: Optional[int] = None,
        actor_id: Optional[str] = None,
        payments_detail: bool = False,
    ) -> dict:
        added = 0
        skipped_details: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        for student_id in student_ids:
            try:
                result = self.add_student_to_group(
                    org_id, group_id, student_id,
                    mode=mode, monthly_fee=monthly_fee, actor_id=actor_id,
                    payments_detail=payments_detail,
                )
                added += 1
                for warning in result.get("warnings", []):
                    warnings.append({"student_id": str(student_id), **warning})
            except ValueError as exc:
                # Release the row locks taken before the refusal.
                self.db.rollback()
                skipped_details.append({"student_id": str(student_id), "reason": str(exc)})
        # ``skipped`` stays a number for older clients; the reasons are new.
        return {
            "group_id": str(group_id),
            "added": added,
            "skipped": len(skipped_details),
            "skipped_details": skipped_details,
            "warnings": warnings,
        }

    def add_student_to_group(
        self,
        org_id: str,
        group_id: str,
        student_id: str,
        *,
        mode: str = "transfer",
        monthly_fee: Optional[int] = None,
        actor_id: Optional[str] = None,
        payments_detail: bool = False,
    ) -> dict:
        """Enrol a student in a group.

        ``transfer`` (default): every other active membership is closed and the
        group becomes the primary one.  ``add``: other memberships stay, the
        primary group changes only if the student had none.  ``monthly_fee`` is
        written to the membership; the caller must have checked the right to
        manage payments.  Warnings (never refusals) are returned in ``warnings``.

        A ``transfer`` also carries the payments in force over to the new group (and copies the
        fee when it has none) - see ``PaymentService.carry_over_on_transfer``; ``payments_detail``
        lets the note in ``warnings`` name dates / lessons (callers who may see payments).
        """
        if mode not in MEMBERSHIP_MODES:
            raise ValueError("Неизвестный способ добавления в группу")
        # Serialize every membership change for this student.  Without this
        # row lock, two concurrent admin requests could both observe the old
        # group and create two active memberships despite the service rule.
        student = (
            self.db.query(Student)
            .filter(Student.id == student_id, Student.org_id == org_id)
            .with_for_update()
            .first()
        )
        if not student:
            raise ValueError("Ученик не найден")
        if student.status == "archived":
            raise ValueError("Архивного ученика нельзя перевести в группу")

        # Resolve the target first. All involved groups are locked below in a
        # stable ID order, so two opposite transfers cannot deadlock.
        group = (
            self.db.query(Group)
            .filter(Group.id == group_id, Group.org_id == org_id)
            .first()
        )
        if not group:
            raise ValueError("Группа не найдена")
        if group.status != "active":
            raise ValueError("Нельзя зачислить ученика в архивную группу")

        existing = (
            self.db.query(StudentGroup)
            .filter(
                StudentGroup.student_id == student_id,
                StudentGroup.group_id == group_id,
            )
            .with_for_update()
            .first()
        )
        other_active_memberships = (
            self.db.query(StudentGroup)
            .filter(
                StudentGroup.student_id == student_id,
                StudentGroup.group_id != group_id,
                StudentGroup.is_active.is_(True),
            )
            .with_for_update()
            .all()
        )
        already_member = bool(existing and existing.is_active)

        if already_member and mode == "add":
            raise MembershipConflict("Ученик уже состоит в этой группе")
        # A transfer to the group the student already belongs to (as the only
        # and primary one) is an explicit no-op: the client uses this state to
        # avoid a false "successfully transferred" confirmation.
        if (
            already_member
            and mode == "transfer"
            and not other_active_memberships
            and str(student.group_id) == str(group.id)
        ):
            if monthly_fee is not None:
                # Nothing moves, but an authorised amount is still the admin's intent.
                existing.monthly_fee = int(monthly_fee)
                existing.monthly_fee_updated_by = UUID(str(actor_id)) if actor_id else None
                existing.monthly_fee_updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                self.db.commit()
            return {
                "student_id": str(student_id),
                "group_id": str(group_id),
                "status": "unchanged",
                "reason": "already_in_target_group",
                "display_id": student.display_id,
                "mode": mode,
                "membership_id": str(existing.id),
                "warnings": [],
            }

        now_dt = datetime.now(timezone.utc)

        # Only a transfer closes the previous memberships.  The roster of
        # every changed group must not rewrite an already started lesson.
        closing = other_active_memberships if mode == "transfer" else []
        affected_group_ids = [group.id, *(membership.group_id for membership in closing)]
        self._lock_groups_for_membership_change(org_id, affected_group_ids)
        self._assert_no_open_attendance_for_groups(org_id, affected_group_ids)
        for om in closing:
            om.is_active = False
            om.left_at = now_dt

        # Target membership is either a historical inactive record that will
        # be reactivated (its payment history stays attached), or a new one.
        if not already_member:
            active_count = (
                self.db.query(StudentGroup)
                .filter(
                    StudentGroup.group_id == group_id,
                    StudentGroup.is_active.is_(True),
                )
                .count()
            )
            if group.max_students is not None and active_count >= group.max_students:
                raise ValueError("В группе достигнут лимит учеников")

        if existing:
            if not already_member:
                existing.is_active = True
                # Returning to a historical group starts a new membership
                # period; recorded attendance stays tied to past lessons.
                existing.joined_at = now_dt
                existing.left_at = None
            if closing:
                # A transfer into a group the student already belongs to keeps
                # its old joined_at (past rosters need it); this instant lets
                # the journal recognise the transfer.
                existing.transferred_at = now_dt
            membership = existing
        else:
            membership = StudentGroup(
                student_id=student_id, group_id=group_id, is_active=True, joined_at=now_dt
            )
            self.db.add(membership)

        if monthly_fee is not None:
            membership.monthly_fee = int(monthly_fee)
            membership.monthly_fee_updated_by = UUID(str(actor_id)) if actor_id else None
            membership.monthly_fee_updated_at = now_dt.replace(tzinfo=None)

        # N13 P2.1: a transfer carries the payments in force over to the new group and copies
        # the fee when the new group has none.  Before the primary group moves: the donor of
        # the fee is the group the student is leaving.
        carried: list[dict[str, str]] = []
        if closing:
            from .payments_service import PaymentService  # lazy: pulls in the status maths

            self.db.flush()
            carried = PaymentService(self.db).carry_over_on_transfer(
                org_id, actor_id, closing, membership,
                primary_group_id=student.group_id, detailed=payments_detail, moment=now_dt,
            )

        # The primary group: a transfer always moves it, "add" only fills a gap.
        if mode == "transfer" or student.group_id is None:
            student.group_id = group_id

        if student.status in ("new", "diagnosed"):
            student.status = "active"

        # Момент фактического зачисления → выдаём публичный Display-ID, если его
        # ещё нет. Это тот самый вызов переиспользуемого сервиса из «assign to
        # group» flow (реализация будущего контроллера AssignToGroup).
        if not student.display_id:
            assign_display_id(self.db, org_id, student)

        self.db.flush()
        warnings = self._membership_warnings(student, group)
        warnings.extend(carried)
        self.db.commit()
        return {
            "student_id": str(student_id),
            "group_id": str(group_id),
            "status": "added",
            "display_id": student.display_id,
            "mode": mode,
            "membership_id": str(membership.id),
            "warnings": warnings,
        }

    @staticmethod
    def _membership_event_at(membership: StudentGroup) -> Optional[datetime]:
        """When the membership was opened or made primary by a transfer (the later one)."""
        moments = [m for m in (membership.joined_at, membership.transferred_at) if m]
        return max(moments) if moments else None

    def _membership_warnings(self, student: Student, group: Group) -> list[dict[str, str]]:
        """Soft notes after enrolment: shown to the administrator, never a refusal."""
        warnings: list[dict[str, str]] = []
        active_total = (
            self.db.query(func.count(StudentGroup.id))
            .filter(StudentGroup.student_id == student.id, StudentGroup.is_active.is_(True))
            .scalar()
            or 0
        )
        if active_total >= 3:
            warnings.append({
                "code": "third_group",
                "message": f"Ученик состоит в {active_total} группах",
            })
        if group.class_level is not None and student.class_level != group.class_level:
            warnings.append({
                "code": "class_mismatch",
                "message": (
                    f"Класс ученика ({student.class_level}) не совпадает "
                    f"с классом группы ({group.class_level})"
                ),
            })
        return warnings

    def remove_student_from_group(
        self, org_id: str, group_id: str, student_id: str
    ) -> bool:
        # Resolve both primary entities through the caller's organization
        # before touching membership.  IDs are global UUIDs, so a membership
        # lookup alone is not a tenant boundary.
        student = (
            self.db.query(Student)
            .filter(Student.id == student_id, Student.org_id == org_id)
            .with_for_update()
            .first()
        )
        group = (
            self.db.query(Group)
            .filter(Group.id == group_id, Group.org_id == org_id)
            .with_for_update()
            .first()
        )
        if not student or not group:
            raise ValueError("Ученик или группа не найдены")
        sg = (
            self.db.query(StudentGroup)
            .filter(
                StudentGroup.student_id == student.id,
                StudentGroup.group_id == group.id,
                StudentGroup.is_active.is_(True),
            )
            .with_for_update()
            .first()
        )
        if not sg:
            raise ValueError("Ученик не в этой группе")
        self._lock_groups_for_membership_change(org_id, [group.id])
        self._assert_no_open_attendance_for_groups(org_id, [group.id])
        sg.is_active = False
        sg.left_at = datetime.now(timezone.utc)
        self.db.flush()

        if str(student.group_id or "") == str(group.id):
            # The primary group is gone: the earliest remaining active
            # membership (in a live group first) takes over, else none.
            successor = (
                self.db.query(StudentGroup)
                .join(Group, Group.id == StudentGroup.group_id)
                .filter(
                    StudentGroup.student_id == student.id,
                    StudentGroup.is_active.is_(True),
                    Group.org_id == org_id,
                )
                .order_by(
                    (Group.status != "active"),
                    StudentGroup.joined_at,
                    StudentGroup.id,
                )
                .first()
            )
            student.group_id = successor.group_id if successor else None

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
            description=group.description,
            schedule=group.schedule or [],
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
        self._expire_due_diagnostic_invites(org_id)
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

    # ═══════════════════════════════════════════════════════════════════
    # 10. SCHEDULE & ATTENDANCE
    # ═══════════════════════════════════════════════════════════════════

    def get_schedule(
        self, org_id: str, teacher_id: Optional[str] = None
    ) -> List[ScheduleLessonResponse]:
        """Вернуть только уже настроенное расписание.

        Read endpoint не имеет права создавать демо-слоты: иначе посещаемость
        начинает ссылаться на уроки, которых учебный центр не утверждал.
        """
        query = self.db.query(Group).filter(
            Group.org_id == org_id, Group.status == "active"
        )
        if teacher_id:
            query = query.filter(Group.teacher_id == teacher_id)

        groups = query.order_by(Group.name).all()
        lessons_out: List[ScheduleLessonResponse] = []
        today = datetime.now(BUSINESS_TIMEZONE).date()

        for g in groups:
            recurring, _, recurring_ends = self._schedule_overrides_for_group(g, until=today)
            slots = list(g.schedule or [])
            for s in slots:
                if not isinstance(s, dict):
                    continue
                lesson_id = self._lesson_id_for_slot(g, s)
                ended_at = recurring_ends.get(str(lesson_id))
                if ended_at and ended_at.effective_date <= today.isoformat():
                    # A stopped recurrence is deliberately omitted from the
                    # current schedule. Its base JSON slot stays intact so
                    # historical attendance keeps a valid schedule context.
                    continue
                payload = self._slot_payload(s)
                for revision in recurring.get(str(lesson_id), []):
                    if revision.effective_date <= today.isoformat():
                        payload = dict(revision.payload or payload)
                lessons_out.append(
                    ScheduleLessonResponse(
                        id=str(lesson_id),
                        groupId=str(g.id),
                        groupName=g.name,
                        classLevel=g.class_level or 5,
                        **payload,
                    )
                )

        return lessons_out

    @staticmethod
    def _attendance_teacher_snapshot(group: Group) -> Optional[UUID]:
        """Teacher to record on a new lesson row.

        Groups keep no teacher history, so every created ``GroupAttendance``
        freezes the teacher the group has *now*; a later reassignment of the
        group never rewrites the lessons already taught.
        """
        return group.teacher_id

    def _require_group_access(
        self,
        org_id: str,
        group_id: str,
        *,
        actor_id: Optional[str],
        is_org_admin: bool,
    ) -> Group:
        """Вернуть группу, если actor имеет право работать с её журналом/расписанием."""
        group = (
            self.db.query(Group)
            .filter(Group.id == group_id, Group.org_id == org_id)
            .first()
        )
        if not group:
            raise ValueError("Группа не найдена")
        if is_org_admin:
            return group
        if not actor_id or not group.teacher_id or str(group.teacher_id) != str(actor_id):
            raise PermissionError("Нет доступа к этой группе")
        return group

    @staticmethod
    def _lesson_time_minutes(value: Any, field_name: str) -> int:
        """Parse strict HH:MM time used by the recurring schedule."""
        if not isinstance(value, str):
            raise ValueError(f"{field_name} должен иметь формат HH:MM")
        try:
            parsed = datetime.strptime(value, "%H:%M")
        except ValueError as exc:
            raise ValueError(f"{field_name} должен иметь формат HH:MM") from exc
        if parsed.strftime("%H:%M") != value:
            raise ValueError(f"{field_name} должен иметь формат HH:MM")
        return parsed.hour * 60 + parsed.minute

    @classmethod
    def _validate_lesson_time_range(cls, start_time: Any, end_time: Any) -> tuple[int, int]:
        start_minutes = cls._lesson_time_minutes(start_time, "Время начала")
        end_minutes = cls._lesson_time_minutes(end_time, "Время окончания")
        if end_minutes <= start_minutes:
            raise ValueError("Время окончания должно быть позже времени начала")
        return start_minutes, end_minutes

    @staticmethod
    def _slot_payload(slot: Dict[str, Any]) -> Dict[str, Any]:
        """Нормализовать шаблон занятия к единому формату override-таблицы."""
        return {
            "dayOfWeek": int(slot.get("dayOfWeek") or slot.get("day_of_week") or 1),
            "startTime": slot.get("startTime") or slot.get("start_time") or "10:00",
            "endTime": slot.get("endTime") or slot.get("end_time") or "11:30",
            "subject": slot.get("subject") or "Математика",
            "room": slot.get("room") or "Кабинет 204",
            "format": slot.get("format") or "offline",
            "color": slot.get("color") or "blue",
            "topic": slot.get("topic"),
        }

    @staticmethod
    def _lesson_id_for_slot(group: Group, slot: Dict[str, Any]) -> str:
        """Stable ID also for imported legacy slots that predate ``id``."""
        return str(
            slot.get("id")
            or f"lesson-{group.id}-{slot.get('day_of_week') or slot.get('dayOfWeek', 1)}-"
            f"{slot.get('start_time') or slot.get('startTime', '10:00')}"
        )

    @classmethod
    def _validate_schedule_payload(cls, payload: Dict[str, Any]) -> None:
        day = payload.get("dayOfWeek")
        if not isinstance(day, int) or not 1 <= day <= 7:
            raise ValueError("День недели должен быть от 1 до 7")
        cls._validate_lesson_time_range(payload.get("startTime"), payload.get("endTime"))
        if payload.get("format") not in {"offline", "online"}:
            raise ValueError("Формат урока должен быть offline или online")

    @staticmethod
    def _apply_schedule_patch(payload: Dict[str, Any], request: Any) -> Dict[str, Any]:
        fields = (
            "dayOfWeek", "startTime", "endTime", "subject",
            "room", "format", "color", "topic",
        )
        result = {field: payload.get(field) for field in fields}
        for field in fields:
            value = getattr(request, field, None)
            if value is not None:
                result[field] = value
        return result

    def get_diagnostic_invite(
        self,
        org_id: str,
        diagnostic_id: str,
        *,
        actor_id: Optional[str],
        is_org_admin: bool,
    ) -> dict:
        """Return one diagnostic invite after student-level visibility checks."""
        invite = (
            self.db.query(DiagnosticInvite)
            .options(joinedload(DiagnosticInvite.student))
            .filter(DiagnosticInvite.id == diagnostic_id, DiagnosticInvite.org_id == org_id)
            .first()
        )
        if not invite or not invite.student_id:
            raise ValueError("Диагностика не найдена")
        self._expire_diagnostic_invite_if_due(invite)
        student = self._require_student_visibility(
            org_id,
            str(invite.student_id),
            actor_id=actor_id,
            is_org_admin=is_org_admin,
        )
        exact_result = self._diagnostic_results_by_invite([invite.id]).get(invite.id)
        result = None
        if exact_result:
            theta = float(exact_result.final_theta) if exact_result.final_theta is not None else None
            result = {
                "theta": theta,
                "accuracy": float(exact_result.accuracy) if exact_result.accuracy is not None else None,
                **level_fields(exact_result),
                "total_questions": exact_result.total_tasks,
                "correct_answers": exact_result.correct_answers,
            }
        return {
            "id": str(invite.id),
            "code": invite.code,
            "student_id": str(invite.student_id),
            "student_name": f"{student.first_name} {student.last_name}",
            "class_level": invite.class_level,
            "diagnostic_type": invite.diagnostic_type,
            "status": invite.status,
            "session_id": str(exact_result.session_id or invite.cat_session_id)
            if exact_result and (exact_result.session_id or invite.cat_session_id)
            else str(invite.cat_session_id) if invite.cat_session_id else None,
            "student_status": student.status,
            "result": result,
            "created_at": invite.created_at.isoformat() if invite.created_at else None,
            "started_at": invite.started_at.isoformat() if invite.started_at else None,
            "completed_at": invite.completed_at.isoformat() if invite.completed_at else None,
        }

    def acknowledge_initial_diagnostic_report(
        self,
        org_id: str,
        diagnostic_id: str,
        *,
        actor_id: str,
        is_org_admin: bool,
    ) -> dict:
        """Record that the current group teacher has seen an initial report.

        The acknowledgement is stored on the active group membership, so it
        cannot remove the notice after a later transfer to another teacher.
        It is intentionally idempotent because the report page may reload.
        """
        invite = (
            self.db.query(DiagnosticInvite)
            .filter(DiagnosticInvite.id == diagnostic_id, DiagnosticInvite.org_id == org_id)
            .first()
        )
        if not invite or not invite.student_id:
            raise ValueError("Диагностика не найдена")
        self._require_student_visibility(
            org_id,
            str(invite.student_id),
            actor_id=actor_id,
            is_org_admin=is_org_admin,
        )
        if invite.diagnostic_type != "initial" or invite.status != "completed":
            return {"acknowledged": False}

        membership = (
            self.db.query(StudentGroup)
            .join(Group, Group.id == StudentGroup.group_id)
            .filter(
                StudentGroup.student_id == invite.student_id,
                StudentGroup.is_active.is_(True),
                StudentGroup.left_at.is_(None),
                Group.org_id == org_id,
                Group.teacher_id == actor_id,
            )
            .with_for_update()
            .first()
        )
        if not membership:
            # An administrator can read reports but cannot acknowledge one
            # on behalf of the teacher; a report of an unassigned student has
            # no group-level notice to clear.
            return {"acknowledged": False}

        membership.initial_diagnostic_reviewed_invite_id = invite.id
        membership.initial_diagnostic_reviewed_at = datetime.now(timezone.utc)
        membership.initial_diagnostic_reviewed_by = UUID(actor_id)
        self.db.commit()
        return {"acknowledged": True}

    def _find_schedule_slot(self, group: Group, lesson_id: str) -> Dict[str, Any]:
        for slot in group.schedule or []:
            if isinstance(slot, dict) and self._lesson_id_for_slot(group, slot) == lesson_id:
                return slot
        raise ValueError("Урок не принадлежит расписанию этой группы")

    def _schedule_overrides_for_group(
        self,
        group: Group,
        *,
        until: date,
        extra_lesson_ids: Optional[List[str]] = None,
        preloaded_overrides: Optional[List[GroupScheduleOverride]] = None,
    ) -> tuple[
        Dict[str, List[GroupScheduleOverride]],
        Dict[tuple[str, str], GroupScheduleOverride],
        Dict[str, GroupScheduleOverride],
    ]:
        lesson_ids = [
            self._lesson_id_for_slot(group, slot)
            for slot in (group.schedule or [])
            if isinstance(slot, dict)
        ]
        lesson_ids.extend(extra_lesson_ids or [])
        lesson_ids = list(dict.fromkeys(lesson_ids))
        if not lesson_ids:
            return {}, {}, {}
        if preloaded_overrides is not None:
            # Same filter and order as the query below, applied to rows that a
            # batch loader already fetched for many groups at once.
            wanted = set(lesson_ids)
            overrides = [
                item for item in preloaded_overrides
                if item.lesson_id in wanted and item.effective_date <= until.isoformat()
            ]
        else:
            overrides = (
                self.db.query(GroupScheduleOverride)
                .filter(
                    GroupScheduleOverride.group_id == group.id,
                    GroupScheduleOverride.lesson_id.in_(lesson_ids),
                    GroupScheduleOverride.effective_date <= until.isoformat(),
                )
                .order_by(GroupScheduleOverride.effective_date, GroupScheduleOverride.created_at)
                .all()
            )
        recurring: Dict[str, List[GroupScheduleOverride]] = {}
        occurrences: Dict[tuple[str, str], GroupScheduleOverride] = {}
        recurring_ends: Dict[str, GroupScheduleOverride] = {}
        for override in overrides:
            if override.scope == "recurring":
                recurring.setdefault(override.lesson_id, []).append(override)
            elif override.scope == "occurrence":
                occurrences[(override.lesson_id, override.effective_date)] = override
            elif override.scope == "recurring_end":
                previous = recurring_ends.get(override.lesson_id)
                # The table migration enforces one end marker per recurrence,
                # but choose the earliest one defensively for legacy data.
                if not previous or override.effective_date < previous.effective_date:
                    recurring_ends[override.lesson_id] = override
        return recurring, occurrences, recurring_ends

    def _configured_schedule_payload_as_of(
        self, group: Group, lesson_id: str, at_date: date
    ) -> Dict[str, Any]:
        """Return the recurring configuration effective on a date.

        Unlike an occurrence resolver, ``at_date`` does not need to be the
        lesson's weekday. This is essential when a teacher says "from Tuesday
        the Monday lesson moves to Thursday".
        """
        payload = self._slot_payload(self._find_schedule_slot(group, lesson_id))
        recurring, _, _ = self._schedule_overrides_for_group(group, until=at_date)
        for revision in recurring.get(lesson_id, []):
            if revision.effective_date <= at_date.isoformat():
                payload = dict(revision.payload or payload)
        return payload

    def _preload_schedule_data(
        self, group_ids: List[Any], *, date_from: date, date_to: date
    ) -> "_SchedulePreload":
        """One-off lessons and schedule overrides of many groups: two queries in total."""
        preload = _SchedulePreload()
        ids = list(dict.fromkeys(group_ids))
        if not ids:
            return preload
        for lesson in (
            self.db.query(GroupScheduleOneOffLesson)
            .filter(
                GroupScheduleOneOffLesson.group_id.in_(ids),
                GroupScheduleOneOffLesson.lesson_date >= date_from.isoformat(),
                GroupScheduleOneOffLesson.lesson_date <= date_to.isoformat(),
            )
            .all()
        ):
            preload.one_offs.setdefault(lesson.group_id, []).append(lesson)
        for override in (
            self.db.query(GroupScheduleOverride)
            .filter(
                GroupScheduleOverride.group_id.in_(ids),
                GroupScheduleOverride.effective_date <= date_to.isoformat(),
            )
            .order_by(GroupScheduleOverride.effective_date, GroupScheduleOverride.created_at)
            .all()
        ):
            preload.overrides.setdefault(override.group_id, []).append(override)
        return preload

    def _resolved_occurrences_for_group(
        self,
        group: Group,
        *,
        date_from: date,
        date_to: date,
        preloaded: Optional["_SchedulePreload"] = None,
    ) -> List[Dict[str, Any]]:
        """Развернуть шаблон расписания в реальные занятия заданного периода.

        ``preloaded`` (see ``_preload_schedule_data``) replaces the two per-group
        queries when many groups are resolved in one pass; its rows must cover
        the requested period.
        """
        if preloaded is not None:
            one_off_lessons = [
                lesson for lesson in preloaded.one_offs.get(group.id, [])
                if date_from.isoformat() <= lesson.lesson_date <= date_to.isoformat()
            ]
        else:
            one_off_lessons = (
                self.db.query(GroupScheduleOneOffLesson)
                .filter(
                    GroupScheduleOneOffLesson.group_id == group.id,
                    GroupScheduleOneOffLesson.lesson_date >= date_from.isoformat(),
                    GroupScheduleOneOffLesson.lesson_date <= date_to.isoformat(),
                )
                .all()
            )
        recurring, occurrence_overrides, recurring_ends = self._schedule_overrides_for_group(
            group,
            until=date_to,
            extra_lesson_ids=[lesson.id for lesson in one_off_lessons],
            preloaded_overrides=preloaded.overrides.get(group.id, []) if preloaded is not None else None,
        )
        slots = [slot for slot in (group.schedule or []) if isinstance(slot, dict)]
        result: List[Dict[str, Any]] = []
        current_day = date_from
        while current_day <= date_to:
            current_iso = current_day.isoformat()
            for slot in slots:
                lesson_id = self._lesson_id_for_slot(group, slot)
                ended_at = recurring_ends.get(lesson_id)
                if ended_at and ended_at.effective_date <= current_iso:
                    # Do not generate artificial future lesson instances once
                    # the recurrence was explicitly stopped. One-off lessons
                    # remain independent and are handled below.
                    continue
                base_payload = self._slot_payload(slot)
                configured_payload = dict(base_payload)
                for revision in recurring.get(lesson_id, []):
                    if revision.effective_date <= current_iso:
                        configured_payload = dict(revision.payload or configured_payload)

                if configured_payload["dayOfWeek"] != current_day.isoweekday():
                    continue

                occurrence_override = occurrence_overrides.get((lesson_id, current_iso))
                payload = (
                    dict(occurrence_override.payload or configured_payload)
                    if occurrence_override
                    else configured_payload
                )
                result.append(
                    {
                        "id": lesson_id,
                        "groupId": str(group.id),
                        "groupName": group.name,
                        "classLevel": group.class_level or 5,
                        **payload,
                        "occurrenceDate": current_iso,
                        "isRescheduled": bool(occurrence_override),
                        "originalStartTime": configured_payload["startTime"] if occurrence_override else None,
                        "originalEndTime": configured_payload["endTime"] if occurrence_override else None,
                        "originalRoom": configured_payload["room"] if occurrence_override else None,
                        "rescheduleReason": occurrence_override.reason if occurrence_override else None,
                    }
                )
            current_day += timedelta(days=1)

        for one_off in one_off_lessons:
            occurrence_override = occurrence_overrides.get((one_off.id, one_off.lesson_date))
            configured_payload = self._slot_payload(dict(one_off.payload or {}))
            payload = (
                dict(occurrence_override.payload or configured_payload)
                if occurrence_override
                else configured_payload
            )
            result.append(
                {
                    "id": one_off.id,
                    "groupId": str(group.id),
                    "groupName": group.name,
                    "classLevel": group.class_level or 5,
                    **payload,
                    "occurrenceDate": one_off.lesson_date,
                    "isRescheduled": bool(occurrence_override),
                    "originalStartTime": configured_payload["startTime"] if occurrence_override else None,
                    "originalEndTime": configured_payload["endTime"] if occurrence_override else None,
                    "originalRoom": configured_payload["room"] if occurrence_override else None,
                    "rescheduleReason": occurrence_override.reason if occurrence_override else None,
                    "isOneOff": True,
                }
            )
        return sorted(result, key=lambda item: (item["occurrenceDate"], item["startTime"], item["groupName"]))

    def _resolved_schedule_payload_for_date(
        self, group: Group, lesson_id: str, occurrence_date: date
    ) -> Dict[str, Any]:
        occurrences = self._resolved_occurrences_for_group(
            group, date_from=occurrence_date, date_to=occurrence_date
        )
        for occurrence in occurrences:
            if occurrence["id"] == lesson_id:
                return occurrence
        raise ValueError("На указанную дату этот урок не запланирован")

    def _assert_teacher_time_is_available(
        self,
        *,
        group: Group,
        lesson_id: str,
        occurrence_date: date,
        payload: Dict[str, Any],
    ) -> None:
        """Reject a transfer/revision that overlaps another lesson of its teacher.

        The browser check is only a convenience. This server-side check uses
        resolved occurrences, so one-off transfers and already versioned
        recurring schedules are both considered.
        """
        if not group.teacher_id:
            return
        candidate_start, candidate_end = self._validate_lesson_time_range(
            payload["startTime"], payload["endTime"]
        )
        peers = (
            self.db.query(Group)
            .filter(
                Group.org_id == group.org_id,
                Group.status == "active",
                Group.teacher_id == group.teacher_id,
            )
            .all()
        )
        for peer in peers:
            for occurrence in self._resolved_occurrences_for_group(
                peer, date_from=occurrence_date, date_to=occurrence_date
            ):
                if str(peer.id) == str(group.id) and occurrence["id"] == lesson_id:
                    continue
                peer_start, peer_end = self._validate_lesson_time_range(
                    occurrence["startTime"], occurrence["endTime"]
                )
                if candidate_start < peer_end and candidate_end > peer_start:
                    raise ValueError(
                        f"У преподавателя уже есть урок «{occurrence['groupName']}» "
                        f"{occurrence['startTime']}–{occurrence['endTime']} в это время"
                    )

    def _upsert_schedule_override(
        self,
        *,
        group: Group,
        lesson_id: str,
        scope: str,
        effective_date: date,
        payload: Dict[str, Any],
        reason: str,
        actor_id: str,
        baseline_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        effective_iso = effective_date.isoformat()
        override = (
            self.db.query(GroupScheduleOverride)
            .filter(
                GroupScheduleOverride.group_id == group.id,
                GroupScheduleOverride.lesson_id == lesson_id,
                GroupScheduleOverride.scope == scope,
                GroupScheduleOverride.effective_date == effective_iso,
            )
            .first()
        )
        before_payload = (
            dict(override.payload or {})
            if override
            else dict(baseline_payload or {})
        )
        if override:
            override.payload = payload
            flag_modified(override, "payload")
            override.reason = reason
            override.changed_by = actor_id
        else:
            override = GroupScheduleOverride(
                org_id=group.org_id,
                group_id=group.id,
                lesson_id=lesson_id,
                scope=scope,
                effective_date=effective_iso,
                payload=payload,
                reason=reason,
                changed_by=actor_id,
            )
            self.db.add(override)
            self.db.flush()

        self.db.add(
            GroupScheduleAudit(
                override_id=override.id,
                org_id=group.org_id,
                group_id=group.id,
                lesson_id=lesson_id,
                scope=scope,
                effective_date=effective_iso,
                actor_id=actor_id,
                before_payload=before_payload,
                after_payload=payload,
                reason=reason,
            )
        )

    def get_schedule_occurrences(
        self,
        org_id: str,
        *,
        date_from: date,
        date_to: date,
        teacher_id: Optional[str] = None,
    ) -> List[ScheduleOccurrenceResponse]:
        if date_to < date_from:
            raise ValueError("date_to не может быть раньше date_from")
        if (date_to - date_from).days > 62:
            raise ValueError("Период расписания не может превышать 63 дня")
        query = self.db.query(Group).filter(Group.org_id == org_id, Group.status == "active")
        if teacher_id:
            query = query.filter(Group.teacher_id == teacher_id)
        occurrences: List[ScheduleOccurrenceResponse] = []
        for group in query.order_by(Group.name).all():
            occurrences.extend(
                ScheduleOccurrenceResponse(**item)
                for item in self._resolved_occurrences_for_group(
                    group, date_from=date_from, date_to=date_to
                )
            )
        return sorted(occurrences, key=lambda item: (item.occurrenceDate, item.startTime, item.groupName))

    def reschedule_lesson_occurrence(
        self,
        org_id: str,
        lesson_id: str,
        req: RescheduleLessonRequest,
        *,
        actor_id: Optional[str],
        is_org_admin: bool,
    ) -> ScheduleOccurrenceResponse:
        if not actor_id:
            raise PermissionError("Не удалось определить автора переноса")
        one_off = (
            self.db.query(GroupScheduleOneOffLesson)
            .filter(
                GroupScheduleOneOffLesson.org_id == org_id,
                GroupScheduleOneOffLesson.id == lesson_id,
            )
            .first()
        )
        group = None
        if one_off:
            group = (
                self.db.query(Group)
                .filter(Group.id == one_off.group_id, Group.org_id == org_id)
                .first()
            )
        if not group:
            groups = self.db.query(Group).filter(Group.org_id == org_id).all()
            group = next(
                (
                    item
                    for item in groups
                    if any(
                        isinstance(slot, dict) and self._lesson_id_for_slot(item, slot) == lesson_id
                        for slot in (item.schedule or [])
                    )
                ),
                None,
            )
        if not group:
            raise ValueError("Урок не найден")
        self._require_group_access(org_id, str(group.id), actor_id=actor_id, is_org_admin=is_org_admin)

        now = datetime.now(BUSINESS_TIMEZONE)
        if req.lessonDate != now.date():
            raise ValueError("Перенос доступен только для сегодняшнего урока")
        occurrence = self._resolved_schedule_payload_for_date(group, lesson_id, req.lessonDate)
        original_start, _ = self._validate_lesson_time_range(
            occurrence["startTime"], occurrence["endTime"]
        )
        current_minutes = now.hour * 60 + now.minute
        if current_minutes >= original_start - 60:
            raise ValueError("Перенести урок можно не позднее чем за 60 минут до начала")

        active_attendance = (
            self.db.query(GroupAttendance)
            .filter(
                GroupAttendance.group_id == group.id,
                GroupAttendance.lesson_id == lesson_id,
                GroupAttendance.lesson_date == req.lessonDate.isoformat(),
            )
            .first()
        )
        if active_attendance:
            raise ValueError("Нельзя переносить урок после начала, отмены или завершения")

        updated_payload = self._apply_schedule_patch(occurrence, req)
        self._validate_schedule_payload(updated_payload)
        new_start_minutes, _ = self._validate_lesson_time_range(
            updated_payload["startTime"], updated_payload["endTime"]
        )
        if new_start_minutes <= current_minutes:
            raise ValueError("Новое время начала должно быть позже текущего времени")
        if all(updated_payload.get(key) == occurrence.get(key) for key in ("startTime", "endTime", "room", "format", "topic")):
            raise ValueError("Укажите новое время, кабинет, формат или тему урока")
        self._assert_teacher_time_is_available(
            group=group,
            lesson_id=lesson_id,
            occurrence_date=req.lessonDate,
            payload=updated_payload,
        )

        self._upsert_schedule_override(
            group=group,
            lesson_id=lesson_id,
            scope="occurrence",
            effective_date=req.lessonDate,
            payload=updated_payload,
            reason=req.reason.strip(),
            actor_id=actor_id,
            baseline_payload=occurrence,
        )
        # The exam remains attached to this same occurrence.  We only append
        # an immutable audit entry so the administrator can later see both
        # the old and the new schedule; no exam state or student access code
        # is changed by a transfer.
        self._sync_exam_lesson_reschedule(
            org_id=org_id,
            group_id=str(group.id),
            group_name=group.name,
            lesson_id=lesson_id,
            lesson_date=req.lessonDate.isoformat(),
            actor_id=actor_id,
            reason=req.reason.strip(),
            previous={
                "startTime": occurrence.get("startTime"),
                "endTime": occurrence.get("endTime"),
                "room": occurrence.get("room"),
                "format": occurrence.get("format"),
                "topic": occurrence.get("topic"),
            },
            current={
                "startTime": updated_payload.get("startTime"),
                "endTime": updated_payload.get("endTime"),
                "room": updated_payload.get("room"),
                "format": updated_payload.get("format"),
                "topic": updated_payload.get("topic"),
            },
        )
        self.db.commit()
        return ScheduleOccurrenceResponse(
            **{
                **updated_payload,
                "id": lesson_id,
                "groupId": str(group.id),
                "groupName": group.name,
                "classLevel": group.class_level or 5,
                "occurrenceDate": req.lessonDate.isoformat(),
                "isRescheduled": True,
                "originalStartTime": occurrence["startTime"],
                "originalEndTime": occurrence["endTime"],
                "originalRoom": occurrence["room"],
                "rescheduleReason": req.reason.strip(),
                # Keep the occurrence's scope in the immediate response too.
                # Otherwise the client could temporarily offer a permanent
                # schedule edit for a one-off lesson before the next reload.
                "isOneOff": bool(one_off),
            }
        )

    def revise_recurring_lesson(
        self,
        org_id: str,
        lesson_id: str,
        req: ReviseRecurringLessonRequest,
        *,
        actor_id: Optional[str],
        is_org_admin: bool,
    ) -> ScheduleLessonResponse:
        if not actor_id:
            raise PermissionError("Не удалось определить автора изменения")
        groups = self.db.query(Group).filter(Group.org_id == org_id).all()
        group = next(
            (
                item
                for item in groups
                if any(
                    isinstance(slot, dict) and self._lesson_id_for_slot(item, slot) == lesson_id
                    for slot in (item.schedule or [])
                )
            ),
            None,
        )
        if not group:
            raise ValueError("Урок не найден")
        self._require_group_access(org_id, str(group.id), actor_id=actor_id, is_org_admin=is_org_admin)
        today = datetime.now(BUSINESS_TIMEZONE).date()
        if req.effectiveFrom <= today:
            raise ValueError("Постоянное расписание можно менять только начиная с будущей даты")

        # A dated end marker wins over later recurring revisions.  Without
        # this guard an edit whose effective date is after the end would be
        # accepted, recorded, and then never become visible to the teacher.
        # Re-open/recreate the rule explicitly instead of creating a silent
        # dead configuration.
        recurring_end = self._recurring_end_override(group, lesson_id)
        if recurring_end and recurring_end.effective_date <= req.effectiveFrom.isoformat():
            raise ValueError(
                "Нельзя изменить расписание после даты его прекращения. Создайте новое регулярное занятие."
            )

        current_payload = self._configured_schedule_payload_as_of(
            group, lesson_id, req.effectiveFrom
        )
        updated_payload = self._apply_schedule_patch(current_payload, req)
        self._validate_schedule_payload(updated_payload)
        if all(updated_payload.get(key) == current_payload.get(key) for key in ("dayOfWeek", "startTime", "endTime", "subject", "room", "format", "color", "topic")):
            raise ValueError("Укажите хотя бы одно изменение постоянного расписания")

        # Inspect the first two weeks after the effective date. That covers
        # every weekday once and verifies conflicts against other versioned
        # schedules without fabricating or modifying future attendance rows.
        for offset in range(14):
            candidate_date = req.effectiveFrom + timedelta(days=offset)
            if candidate_date.isoweekday() == updated_payload["dayOfWeek"]:
                self._assert_teacher_time_is_available(
                    group=group,
                    lesson_id=lesson_id,
                    occurrence_date=candidate_date,
                    payload=updated_payload,
                )

        self._upsert_schedule_override(
            group=group,
            lesson_id=lesson_id,
            scope="recurring",
            effective_date=req.effectiveFrom,
            payload=updated_payload,
            reason=req.reason.strip(),
            actor_id=actor_id,
            baseline_payload=current_payload,
        )
        self.db.commit()
        return ScheduleLessonResponse(
            id=lesson_id,
            groupId=str(group.id),
            groupName=group.name,
            classLevel=group.class_level or 5,
            **updated_payload,
        )

    def _require_recurring_lesson(
        self,
        org_id: str,
        lesson_id: str,
        *,
        actor_id: Optional[str],
        is_org_admin: bool,
    ) -> Group:
        """Resolve a base recurring slot and check the actor's group scope.

        A one-off lesson intentionally has no entry in ``Group.schedule`` and
        therefore cannot be ended through this flow. It needs the existing
        single-occurrence cancellation path instead.
        """
        groups = self.db.query(Group).filter(Group.org_id == org_id).all()
        group = next(
            (
                candidate
                for candidate in groups
                if any(
                    isinstance(slot, dict)
                    and self._lesson_id_for_slot(candidate, slot) == lesson_id
                    for slot in (candidate.schedule or [])
                )
            ),
            None,
        )
        if not group:
            raise ValueError("Регулярное занятие не найдено")
        self._require_group_access(
            org_id,
            str(group.id),
            actor_id=actor_id,
            is_org_admin=is_org_admin,
        )
        return group

    def _recurring_end_override(
        self,
        group: Group,
        lesson_id: str,
        *,
        lock: bool = False,
    ) -> Optional[GroupScheduleOverride]:
        query = self.db.query(GroupScheduleOverride).filter(
            GroupScheduleOverride.group_id == group.id,
            GroupScheduleOverride.lesson_id == lesson_id,
            GroupScheduleOverride.scope == "recurring_end",
        )
        if lock:
            query = query.with_for_update()
        # A partial unique index keeps this to one row after the migration.
        # Ordering keeps a deterministic fallback for a manually repaired old
        # database that happens to contain more than one marker.
        return query.order_by(GroupScheduleOverride.effective_date).first()

    def _assert_recurring_end_is_allowed(
        self,
        group: Group,
        lesson_id: str,
        *,
        effective_from: date,
        lock: bool = False,
    ) -> Optional[GroupScheduleOverride]:
        today = datetime.now(BUSINESS_TIMEZONE).date()
        if effective_from <= today:
            raise ValueError("Регулярные занятия можно прекратить только начиная с будущей даты")
        existing_end = self._recurring_end_override(group, lesson_id, lock=lock)
        if existing_end and existing_end.effective_date <= effective_from.isoformat():
            raise ValueError(
                f"Регулярные занятия уже прекращены с {existing_end.effective_date}"
            )
        return existing_end

    @staticmethod
    def _recurring_end_response(
        *,
        group: Group,
        lesson_id: str,
        effective_from: date,
        payload: Dict[str, Any],
        ended: bool = False,
        released_exam_ids: Optional[List[str]] = None,
    ) -> dict[str, Any]:
        exams = list(payload.get("exams") or [])
        return {
            "groupId": str(group.id),
            "lessonId": lesson_id,
            "effectiveFrom": effective_from,
            "affectedExamCount": int(payload.get("affected_exam_count") or len(exams)),
            "releasableExamCount": int(payload.get("releasable_exam_count") or 0),
            "blockedExamCount": int(payload.get("blocked_exam_count") or 0),
            "canEnd": bool(payload.get("can_end")),
            "exams": [
                {
                    "examId": str(item.get("exam_id") or ""),
                    "title": str(item.get("title") or "Экзамен"),
                    "lessonDate": str(item.get("lesson_date") or ""),
                    "deliveryMode": str(item.get("delivery_mode") or "online"),
                    "status": str(item.get("status") or "draft"),
                    "canRelease": bool(item.get("can_release")),
                    "blockerReason": item.get("blocker_reason"),
                }
                for item in exams
            ],
            "ended": ended,
            "releasedExamIds": released_exam_ids or [],
        }

    def get_recurring_lesson_end_impact(
        self,
        org_id: str,
        lesson_id: str,
        *,
        effective_from: date,
        actor_id: Optional[str],
        is_org_admin: bool,
    ) -> RecurringLessonEndImpactResponse:
        """Return a fail-closed preview before a teacher stops a recurrence."""
        group = self._require_recurring_lesson(
            org_id,
            lesson_id,
            actor_id=actor_id,
            is_org_admin=is_org_admin,
        )
        self._assert_recurring_end_is_allowed(
            group,
            lesson_id,
            effective_from=effective_from,
        )
        impact = self._fetch_exam_recurring_end_impact(
            org_id=org_id,
            group_id=str(group.id),
            lesson_id=lesson_id,
            effective_from=effective_from.isoformat(),
        )
        return RecurringLessonEndImpactResponse(
            **self._recurring_end_response(
                group=group,
                lesson_id=lesson_id,
                effective_from=effective_from,
                payload=impact,
            )
        )

    def end_recurring_lesson(
        self,
        org_id: str,
        lesson_id: str,
        req: EndRecurringLessonRequest,
        *,
        actor_id: Optional[str],
        is_org_admin: bool,
    ) -> EndRecurringLessonResponse:
        """End one recurring rule without erasing facts or orphaning exams.

        The remote release is deliberately rechecked under the exam-service
        lock immediately before it mutates any access code/package. Therefore
        a student joining after the UI preview still prevents the operation.
        """
        if not actor_id:
            raise PermissionError("Не удалось определить автора прекращения расписания")
        group = self._require_recurring_lesson(
            org_id,
            lesson_id,
            actor_id=actor_id,
            is_org_admin=is_org_admin,
        )
        existing_end = self._assert_recurring_end_is_allowed(
            group,
            lesson_id,
            effective_from=req.effectiveFrom,
            lock=True,
        )
        impact = self._fetch_exam_recurring_end_impact(
            org_id=org_id,
            group_id=str(group.id),
            lesson_id=lesson_id,
            effective_from=req.effectiveFrom.isoformat(),
        )
        blocked_count = int(impact.get("blocked_exam_count") or 0)
        affected_count = int(impact.get("affected_exam_count") or 0)
        if blocked_count:
            blocked_titles = [
                str(item.get("title") or "Экзамен")
                for item in (impact.get("exams") or [])
                if not item.get("can_release")
            ]
            suffix = f": {', '.join(f'«{title}»' for title in blocked_titles[:2])}" if blocked_titles else ""
            raise ValueError(
                "Нельзя прекратить регулярные занятия: есть активный экзамен или попытки учеников" + suffix
            )
        if affected_count and not req.releaseAffectedExams:
            raise ValueError(
                "Подтвердите возврат затронутых экзаменов в черновики или сначала перенесите их на другие занятия"
            )

        effective_iso = req.effectiveFrom.isoformat()
        before_payload = (
            {
                "state": "recurring_end",
                "effectiveFrom": existing_end.effective_date,
                "reason": existing_end.reason,
            }
            if existing_end
            else self._configured_schedule_payload_as_of(group, lesson_id, req.effectiveFrom)
        )
        end_payload = {
            "state": "recurring_end",
            "effectiveFrom": effective_iso,
            "affectedExamCount": affected_count,
        }
        if existing_end:
            existing_end.effective_date = effective_iso
            existing_end.payload = end_payload
            existing_end.reason = req.reason.strip()
            existing_end.changed_by = UUID(str(actor_id))
            flag_modified(existing_end, "payload")
            end_override = existing_end
        else:
            end_override = GroupScheduleOverride(
                org_id=group.org_id,
                group_id=group.id,
                lesson_id=lesson_id,
                scope="recurring_end",
                effective_date=effective_iso,
                payload=end_payload,
                reason=req.reason.strip(),
                changed_by=UUID(str(actor_id)),
            )
            self.db.add(end_override)
            self.db.flush()

        self.db.add(
            GroupScheduleAudit(
                override_id=end_override.id,
                org_id=group.org_id,
                group_id=group.id,
                lesson_id=lesson_id,
                scope="recurring_end",
                effective_date=effective_iso,
                actor_id=UUID(str(actor_id)),
                before_payload=before_payload,
                after_payload=end_payload,
                reason=req.reason.strip(),
            )
        )
        self.db.flush()

        released_exam_ids: List[str] = []
        if affected_count:
            try:
                released_exam_ids = self._release_exam_recurring_end(
                    org_id=org_id,
                    group_id=str(group.id),
                    group_name=group.name,
                    lesson_id=lesson_id,
                    effective_from=effective_iso,
                    actor_id=str(actor_id),
                    reason=req.reason.strip(),
                )
            except Exception:
                # No schedule tombstone or audit survives a refused/failed
                # remote release. The remote endpoint itself is atomic over
                # the affected exam rows and checks sessions under a row lock.
                self.db.rollback()
                raise

        self.db.commit()
        return EndRecurringLessonResponse(
            **self._recurring_end_response(
                group=group,
                lesson_id=lesson_id,
                effective_from=req.effectiveFrom,
                payload=impact,
                ended=True,
                released_exam_ids=released_exam_ids,
            )
        )

    def create_lesson(
        self,
        org_id: str,
        req: CreateLessonRequest,
        *,
        actor_id: Optional[str] = None,
        is_org_admin: bool = False,
    ) -> ScheduleLessonResponse:
        group = self._require_group_access(
            org_id, req.groupId, actor_id=actor_id, is_org_admin=is_org_admin
        )

        start_minutes, _ = self._validate_lesson_time_range(req.startTime, req.endTime)

        # Validate that lesson is not in the past if scheduled for today
        now = datetime.now(BUSINESS_TIMEZONE)
        today_dow = now.weekday() + 1
        if req.dayOfWeek == today_dow:
            current_minutes = now.hour * 60 + now.minute
            if start_minutes < current_minutes:
                raise ValueError(
                    f"Нельзя запланировать урок на прошедшее время сегодняшнего дня (текущее время: {now.strftime('%H:%M')})"
                )

        current_slots = list(group.schedule or [])
        new_id = f"lesson-{uuid4().hex[:10]}"
        new_slot = {
            "id": new_id,
            "dayOfWeek": req.dayOfWeek,
            "startTime": req.startTime,
            "endTime": req.endTime,
            "subject": req.subject or "Математика",
            "room": req.room or "Кабинет 204",
            "format": req.format or "offline",
            "color": req.color or "blue",
            "topic": req.topic,
        }
        # A new recurring lesson must use the same server-side conflict guard
        # as a transfer or a future recurrence edit.  Checking its next
        # occurrence is sufficient: every peer's recurring weekday is present
        # in that resolved date window, including one-off overrides.
        days_until_occurrence = (req.dayOfWeek - now.isoweekday()) % 7
        next_occurrence_date = now.date() + timedelta(days=days_until_occurrence)
        self._assert_teacher_time_is_available(
            group=group,
            lesson_id=new_id,
            occurrence_date=next_occurrence_date,
            payload=new_slot,
        )
        current_slots.append(new_slot)
        group.schedule = current_slots
        flag_modified(group, "schedule")
        self.db.commit()

        return ScheduleLessonResponse(
            id=new_id,
            groupId=str(group.id),
            groupName=group.name,
            classLevel=group.class_level or 5,
            subject=new_slot["subject"],
            dayOfWeek=new_slot["dayOfWeek"],
            startTime=new_slot["startTime"],
            endTime=new_slot["endTime"],
            room=new_slot["room"],
            format=new_slot["format"],
            color=new_slot["color"],
            topic=new_slot["topic"],
        )

    def create_one_off_lesson(
        self,
        org_id: str,
        req: CreateOneOffLessonRequest,
        *,
        actor_id: Optional[str] = None,
        is_org_admin: bool = False,
    ) -> ScheduleOccurrenceResponse:
        """Create a lesson for one date without altering a recurring rule."""
        if not actor_id:
            raise PermissionError("Не удалось определить автора занятия")
        group = self._require_group_access(
            org_id, req.groupId, actor_id=actor_id, is_org_admin=is_org_admin
        )
        now = datetime.now(BUSINESS_TIMEZONE)
        if req.lessonDate < now.date():
            raise ValueError("Нельзя добавить разовый урок на прошедшую дату")

        payload = {
            "dayOfWeek": req.lessonDate.isoweekday(),
            "startTime": req.startTime,
            "endTime": req.endTime,
            "subject": "Математика",
            "room": req.room or "Кабинет 204",
            "format": req.format or "offline",
            "color": req.color or "blue",
            "topic": req.topic,
        }
        start_minutes, _ = self._validate_lesson_time_range(
            payload["startTime"], payload["endTime"]
        )
        self._validate_schedule_payload(payload)
        if req.lessonDate == now.date() and start_minutes < now.hour * 60 + now.minute:
            raise ValueError(
                "Нельзя добавить разовый урок на прошедшее время сегодняшнего дня"
            )

        lesson_id = f"one-off-{uuid4().hex[:16]}"
        self._assert_teacher_time_is_available(
            group=group,
            lesson_id=lesson_id,
            occurrence_date=req.lessonDate,
            payload=payload,
        )
        self.db.add(
            GroupScheduleOneOffLesson(
                id=lesson_id,
                org_id=group.org_id,
                group_id=group.id,
                lesson_date=req.lessonDate.isoformat(),
                payload=payload,
                created_by=UUID(actor_id),
            )
        )
        self.db.commit()
        return ScheduleOccurrenceResponse(
            id=lesson_id,
            groupId=str(group.id),
            groupName=group.name,
            classLevel=group.class_level or 5,
            **payload,
            occurrenceDate=req.lessonDate.isoformat(),
            isOneOff=True,
        )

    def update_lesson(
        self,
        org_id: str,
        lesson_id: str,
        req: UpdateLessonRequest,
        *,
        actor_id: Optional[str] = None,
        is_org_admin: bool = False,
    ) -> ScheduleLessonResponse:
        # A blind template rewrite changes the interpretation of historical
        # lessons. Clients must explicitly choose either a one-off transfer or
        # a future recurring revision, both of which are audited.
        raise ValueError(
            "Прямое редактирование расписания отключено: используйте перенос "
            "сегодняшнего урока или изменение постоянного расписания с датой начала"
        )

        groups = self.db.query(Group).filter(Group.org_id == org_id).all()
        target_group = None
        target_slot = None
        slot_idx = -1

        for g in groups:
            slots = list(g.schedule or [])
            for idx, s in enumerate(slots):
                if isinstance(s, dict) and s.get("id") == lesson_id:
                    target_group = g
                    target_slot = s
                    slot_idx = idx
                    break
            if target_group:
                break

        if not target_group or not target_slot:
            raise ValueError("Урок не найден")
        self._require_group_access(
            org_id,
            str(target_group.id),
            actor_id=actor_id,
            is_org_admin=is_org_admin,
        )

        # Validate that lesson is not moved to past time if on today
        target_dow = req.dayOfWeek if req.dayOfWeek is not None else int(target_slot.get("dayOfWeek", 1))
        target_start = req.startTime if req.startTime is not None else target_slot.get("startTime", "10:00")
        target_end = req.endTime if req.endTime is not None else target_slot.get("endTime", "11:30")
        target_start_minutes, _ = self._validate_lesson_time_range(target_start, target_end)
        now = datetime.now(BUSINESS_TIMEZONE)
        today_dow = now.weekday() + 1
        if target_dow == today_dow:
            current_minutes = now.hour * 60 + now.minute
            if target_start_minutes < current_minutes:
                raise ValueError(
                    f"Нельзя изменять уже начавшийся урок (текущее время: {now.strftime('%H:%M')})"
                )

        # If group changed
        if req.groupId and req.groupId != str(target_group.id):
            new_group = (
                self.db.query(Group)
                .filter(Group.id == req.groupId, Group.org_id == org_id)
                .first()
            )
            if not new_group:
                raise ValueError("Новая группа не найдена")
            self._require_group_access(
                org_id,
                str(new_group.id),
                actor_id=actor_id,
                is_org_admin=is_org_admin,
            )

            old_slots = [
                s for s in (target_group.schedule or [])
                if isinstance(s, dict) and s.get("id") != lesson_id
            ]
            target_group.schedule = old_slots
            flag_modified(target_group, "schedule")

            updated_slot = dict(target_slot)
            if req.dayOfWeek is not None: updated_slot["dayOfWeek"] = req.dayOfWeek
            if req.startTime is not None: updated_slot["startTime"] = req.startTime
            if req.endTime is not None: updated_slot["endTime"] = req.endTime
            if req.subject is not None: updated_slot["subject"] = req.subject
            if req.room is not None: updated_slot["room"] = req.room
            if req.format is not None: updated_slot["format"] = req.format
            if req.color is not None: updated_slot["color"] = req.color
            if req.topic is not None: updated_slot["topic"] = req.topic

            new_slots = list(new_group.schedule or [])
            new_slots.append(updated_slot)
            new_group.schedule = new_slots
            flag_modified(new_group, "schedule")
            self.db.commit()

            return ScheduleLessonResponse(
                id=lesson_id,
                groupId=str(new_group.id),
                groupName=new_group.name,
                classLevel=new_group.class_level or 5,
                subject=updated_slot.get("subject", "Математика"),
                dayOfWeek=int(updated_slot.get("dayOfWeek", 1)),
                startTime=updated_slot.get("startTime", "10:00"),
                endTime=updated_slot.get("endTime", "11:30"),
                room=updated_slot.get("room", "Кабинет 204"),
                format=updated_slot.get("format", "offline"),
                color=updated_slot.get("color", "blue"),
                topic=updated_slot.get("topic"),
            )

        # Same group
        updated_slot = dict(target_slot)
        if req.dayOfWeek is not None: updated_slot["dayOfWeek"] = req.dayOfWeek
        if req.startTime is not None: updated_slot["startTime"] = req.startTime
        if req.endTime is not None: updated_slot["endTime"] = req.endTime
        if req.subject is not None: updated_slot["subject"] = req.subject
        if req.room is not None: updated_slot["room"] = req.room
        if req.format is not None: updated_slot["format"] = req.format
        if req.color is not None: updated_slot["color"] = req.color
        if req.topic is not None: updated_slot["topic"] = req.topic

        slots = list(target_group.schedule or [])
        slots[slot_idx] = updated_slot
        target_group.schedule = slots
        flag_modified(target_group, "schedule")
        self.db.commit()

        return ScheduleLessonResponse(
            id=lesson_id,
            groupId=str(target_group.id),
            groupName=target_group.name,
            classLevel=target_group.class_level or 5,
            subject=updated_slot.get("subject", "Математика"),
            dayOfWeek=int(updated_slot.get("dayOfWeek", 1)),
            startTime=updated_slot.get("startTime", "10:00"),
            endTime=updated_slot.get("endTime", "11:30"),
            room=updated_slot.get("room", "Кабинет 204"),
            format=updated_slot.get("format", "offline"),
            color=updated_slot.get("color", "blue"),
            topic=updated_slot.get("topic"),
        )

    def delete_lesson(
        self,
        org_id: str,
        lesson_id: str,
        *,
        actor_id: Optional[str] = None,
        is_org_admin: bool = False,
    ) -> bool:
        # Do not leave a callable backdoor for physical deletion. A base JSON
        # slot is historical context for attendance, audit entries and exam
        # bindings; removing it can produce an orphaned delivery. Keep this
        # method as a defensive compatibility boundary for direct callers.
        del org_id, lesson_id, actor_id, is_org_admin
        raise ValueError(
            "Физическое удаление расписания отключено. Используйте отмену экземпляра или прекращение регулярных занятий с даты."
        )

    @staticmethod
    def _roster_ids_from_snapshot(snapshot: Any) -> set[str]:
        """Extract stable student IDs from an attendance roster snapshot."""
        if not isinstance(snapshot, list):
            return set()
        return {
            str(item.get("student_id"))
            for item in snapshot
            if isinstance(item, dict) and item.get("student_id")
        }

    def _active_attendance_roster(self, group_id: UUID) -> list[dict[str, Any]]:
        """Freeze the current active roster as a serialisable lesson fact."""
        memberships = (
            self.db.query(StudentGroup)
            .filter(
                StudentGroup.group_id == group_id,
                StudentGroup.is_active.is_(True),
                StudentGroup.left_at.is_(None),
            )
            .order_by(StudentGroup.joined_at, StudentGroup.id)
            .all()
        )
        student_ids = [membership.student_id for membership in memberships]
        students_by_id = {
            student.id: student
            for student in self.db.query(Student).filter(Student.id.in_(student_ids)).all()
        } if student_ids else {}

        roster: list[dict[str, Any]] = []
        for membership in memberships:
            student = students_by_id.get(membership.student_id)
            if not student:
                continue
            full_name = f"{student.last_name or ''} {student.first_name or ''}".strip() or "Ученик"
            roster.append({
                "student_id": str(student.id),
                "first_name": student.first_name,
                "last_name": student.last_name,
                "full_name": full_name,
                "class_level": student.class_level,
                "display_id": student.display_id,
            })
        return roster

    def _attendance_roster_at(self, group_id: UUID, occurred_at: datetime) -> list[dict[str, Any]]:
        """Freeze the roster that existed when a historical lesson was scheduled.

        Reconciliation happens later, so the current roster is not evidence of
        who could have attended. Memberships are evaluated at the scheduled
        start time; a transfer after that moment cannot rewrite the journal.
        """
        memberships = (
            self.db.query(StudentGroup)
            .filter(
                StudentGroup.group_id == group_id,
                or_(StudentGroup.joined_at.is_(None), StudentGroup.joined_at <= occurred_at),
                or_(StudentGroup.left_at.is_(None), StudentGroup.left_at > occurred_at),
            )
            .order_by(StudentGroup.joined_at, StudentGroup.id)
            .all()
        )
        student_ids = [membership.student_id for membership in memberships]
        students_by_id = {
            student.id: student
            for student in self.db.query(Student).filter(Student.id.in_(student_ids)).all()
        } if student_ids else {}

        roster: list[dict[str, Any]] = []
        for membership in memberships:
            student = students_by_id.get(membership.student_id)
            if not student:
                continue
            full_name = f"{student.last_name or ''} {student.first_name or ''}".strip() or "Ученик"
            roster.append({
                "student_id": str(student.id),
                "first_name": student.first_name,
                "last_name": student.last_name,
                "full_name": full_name,
                "class_level": student.class_level,
                "display_id": student.display_id,
            })
        return roster

    def _resolve_reconcilable_missed_occurrence(
        self,
        group: Group,
        *,
        lesson_date: date,
        lesson_id: str,
    ) -> Dict[str, Any]:
        """Return a past schedule occurrence that can be reconciled explicitly."""
        current_business_time = datetime.now(BUSINESS_TIMEZONE)
        today = current_business_time.date()
        if lesson_date > today:
            raise ValueError("Нельзя разбирать будущий урок")

        occurrence = next(
            (
                item
                for item in self._resolved_occurrences_for_group(
                    group, date_from=lesson_date, date_to=lesson_date
                )
                if item["id"] == lesson_id
            ),
            None,
        )
        if not occurrence:
            raise ValueError("Урок не принадлежит расписанию этой группы")
        if int(occurrence.get("dayOfWeek") or 0) != lesson_date.isoweekday():
            raise ValueError("Дата не соответствует дню урока в расписании")

        _, end_minutes = self._validate_lesson_time_range(
            occurrence.get("startTime"), occurrence.get("endTime")
        )
        if lesson_date == today:
            current_minutes = current_business_time.hour * 60 + current_business_time.minute
            if current_minutes <= end_minutes:
                raise ValueError("Разобрать можно только урок, время которого уже закончилось")
        return occurrence

    def _lock_groups_for_membership_change(self, org_id: str, group_ids: list[UUID]) -> None:
        """Serialize a transfer against starting attendance in either group."""
        unique_group_ids = sorted({str(group_id) for group_id in group_ids})
        if not unique_group_ids:
            return
        # The stable order prevents two opposing transfers from deadlocking.
        self.db.query(Group.id).filter(
            Group.org_id == org_id,
            Group.id.in_(unique_group_ids),
        ).order_by(Group.id).with_for_update().all()

    def _assert_no_open_attendance_for_groups(self, org_id: str, group_ids: list[UUID]) -> None:
        """A roster cannot change while any of its actual lessons is open."""
        if not group_ids:
            return
        active_lesson = (
            self.db.query(GroupAttendance)
            .filter(
                GroupAttendance.org_id == org_id,
                GroupAttendance.group_id.in_(group_ids),
                GroupAttendance.status == LessonAttendanceStatus.IN_PROGRESS.value,
            )
            .with_for_update()
            .first()
        )
        if active_lesson:
            raise ValueError(
                "Нельзя изменить состав группы, пока идёт урок. "
                "Сначала завершите или отмените начатый урок."
            )

    def get_open_attendance_actions(
        self,
        org_id: str,
        *,
        actor_id: Optional[str],
        is_org_admin: bool,
        now: Optional[datetime] = None,
    ) -> list[OpenAttendanceActionResponse]:
        """Return unfinished and never-started past lessons for follow-up.

        A started instance remains the source of truth for an open journal.
        A missed occurrence has no attendance row at all, so the server must
        derive it from the resolved schedule and present it as a separate,
        explicit reconciliation action. It is never converted to absences.
        """
        now = now or datetime.now(BUSINESS_TIMEZONE)
        today_date = now.date()
        today = today_date.isoformat()
        query = (
            self.db.query(GroupAttendance, Group)
            .join(Group, Group.id == GroupAttendance.group_id)
            .filter(
                GroupAttendance.org_id == org_id,
                GroupAttendance.status == LessonAttendanceStatus.IN_PROGRESS.value,
                GroupAttendance.lesson_date <= today,
            )
        )
        if not is_org_admin:
            if not actor_id:
                raise PermissionError("Нет доступа к открытым урокам")
            query = query.filter(Group.teacher_id == actor_id)
        open_rows = query.order_by(
            GroupAttendance.lesson_date.asc(),
            GroupAttendance.started_at.asc(),
            GroupAttendance.created_at.asc(),
        ).all()

        # The missed-lesson candidates are known up front, so the schedule
        # data of every group involved is loaded once (two queries) instead of
        # per group: the cost of this call must not grow with the group count.
        reconciliation_from = missed_lesson_window_start(today_date)
        groups_query = self.db.query(Group).filter(
            Group.org_id == org_id,
            Group.status == "active",
        )
        if not is_org_admin:
            if not actor_id:
                raise PermissionError("Нет доступа к пропущенным урокам")
            groups_query = groups_query.filter(Group.teacher_id == actor_id)
        actionable_groups = groups_query.order_by(Group.name).all()
        group_ids = [group.id for group in actionable_groups]
        open_dates = [row[0].lesson_date for row in open_rows if row[0].lesson_date]
        preload_from = min([reconciliation_from.isoformat(), *open_dates])
        try:
            preload_from_date = date.fromisoformat(preload_from)
        except ValueError:
            preload_from_date = reconciliation_from
        schedule = self._preload_schedule_data(
            [*(row[1].id for row in open_rows), *group_ids],
            date_from=preload_from_date,
            date_to=today_date,
        )
        # A never-started lesson of a group nobody belonged to at that moment
        # is not a lesson that was missed (the group had no students yet).
        # One query for every candidate group, not one per group.
        memberships_by_group: Dict[Any, list[tuple[Optional[datetime], Optional[datetime]]]] = {}
        if group_ids:
            window_start_utc = lesson_start_as_naive_utc(reconciliation_from, 0)
            for member_group_id, joined_at, left_at in (
                self.db.query(StudentGroup.group_id, StudentGroup.joined_at, StudentGroup.left_at)
                .filter(
                    StudentGroup.group_id.in_(group_ids),
                    or_(StudentGroup.left_at.is_(None), StudentGroup.left_at > window_start_utc),
                )
                .all()
            ):
                memberships_by_group.setdefault(member_group_id, []).append((joined_at, left_at))

        actions: list[OpenAttendanceActionResponse] = []
        for attendance, group in open_rows:
            try:
                lesson_date = date.fromisoformat(attendance.lesson_date)
            except (TypeError, ValueError):
                logger.error("Ignoring open attendance row with invalid date %s", attendance.id)
                continue

            occurrence = next(
                (
                    item
                    for item in self._resolved_occurrences_for_group(
                        group, date_from=lesson_date, date_to=lesson_date, preloaded=schedule
                    )
                    if item["id"] == attendance.lesson_id
                ),
                None,
            )
            # A deleted legacy schedule item must not make the unfinished
            # attendance invisible. Preserve the operational task and provide
            # a safe neutral fallback for its presentation.
            occurrence = occurrence or {}
            roster_ids = self._roster_ids_from_snapshot(attendance.roster_snapshot)
            records = dict(attendance.records or {})
            if not roster_ids:
                roster_ids = set(records)
            roster_count = len(roster_ids)
            marked_count = len(set(records) & roster_ids) if roster_ids else len(records)

            actions.append(
                OpenAttendanceActionResponse(
                    groupId=str(group.id),
                    groupName=group.name,
                    classLevel=group.class_level or 5,
                    actionType="in_progress",
                    teacherId=self._open_action_teacher_id(attendance, group),
                    lessonId=attendance.lesson_id,
                    lessonDate=attendance.lesson_date,
                    subject=occurrence.get("subject") or "Занятие",
                    startTime=occurrence.get("startTime") or "—",
                    endTime=occurrence.get("endTime") or "—",
                    room=occurrence.get("room"),
                    format=occurrence.get("format") or "offline",
                    topic=attendance.topic or occurrence.get("topic"),
                    markedCount=marked_count,
                    rosterCount=roster_count,
                    remainingCount=max(roster_count - marked_count, 0),
                )
            )

        # A missed occurrence is intentionally bounded (see missed_lesson_window_start).
        # Without it, an old recurring schedule would create an unbounded and
        # unusable operational queue. Completed/cancelled/in-progress rows
        # are all terminal for this discovery pass and therefore excluded.
        recorded_keys = {
            (str(group_id), lesson_date, lesson_id)
            # Only the three key columns: the JSON records / roster snapshots are not needed here.
            for group_id, lesson_date, lesson_id in (
                self.db.query(
                    GroupAttendance.group_id,
                    GroupAttendance.lesson_date,
                    GroupAttendance.lesson_id,
                )
                .filter(
                    GroupAttendance.org_id == org_id,
                    GroupAttendance.group_id.in_(group_ids),
                    GroupAttendance.lesson_date >= reconciliation_from.isoformat(),
                    GroupAttendance.lesson_date <= today,
                )
                .all()
                if group_ids
                else []
            )
        }

        for group in actionable_groups:
            for occurrence in self._resolved_occurrences_for_group(
                group, date_from=reconciliation_from, date_to=today_date, preloaded=schedule
            ):
                occurrence_date = occurrence["occurrenceDate"]
                key = (str(group.id), occurrence_date, occurrence["id"])
                if key in recorded_keys:
                    continue
                try:
                    start_minutes, end_minutes = self._validate_lesson_time_range(
                        occurrence.get("startTime"), occurrence.get("endTime")
                    )
                except ValueError:
                    logger.error(
                        "Ignoring missed lesson with invalid time for group %s, lesson %s",
                        group.id,
                        occurrence.get("id"),
                    )
                    continue
                is_past = occurrence_date < today or (
                    occurrence_date == today
                    and (now.hour * 60 + now.minute) > end_minutes
                )
                if not is_past:
                    continue
                lesson_start = lesson_start_as_naive_utc(
                    date.fromisoformat(occurrence_date), start_minutes
                )
                if not any(
                    membership_covers_instant(joined_at, left_at, lesson_start)
                    for joined_at, left_at in memberships_by_group.get(group.id, ())
                ):
                    continue  # empty roster at the lesson start: nothing to reconcile
                actions.append(
                    OpenAttendanceActionResponse(
                        actionType="missed",
                        teacherId=str(group.teacher_id) if group.teacher_id else None,
                        groupId=str(group.id),
                        groupName=group.name,
                        classLevel=group.class_level or 5,
                        lessonId=occurrence["id"],
                        lessonDate=occurrence_date,
                        subject=occurrence.get("subject") or "Занятие",
                        startTime=occurrence.get("startTime") or "—",
                        endTime=occurrence.get("endTime") or "—",
                        room=occurrence.get("room"),
                        format=occurrence.get("format") or "offline",
                        topic=occurrence.get("topic"),
                        markedCount=0,
                        rosterCount=0,
                        remainingCount=0,
                    )
                )

        # Teacher names in one batched query (no per-row lookups).
        teacher_ids = {UUID(action.teacherId) for action in actions if action.teacherId}
        if teacher_ids:
            names = {
                str(user.id): f"{user.first_name} {user.last_name}".strip() or None
                for user in self.db.query(User).filter(User.id.in_(teacher_ids)).all()
            }
            for action in actions:
                action.teacherName = names.get(action.teacherId) if action.teacherId else None

        return sorted(
            actions,
            key=lambda action: (
                action.lessonDate,
                action.startTime,
                0 if action.actionType == "missed" else 1,
                action.groupName,
            ),
        )

    @staticmethod
    def _open_action_teacher_id(attendance: GroupAttendance, group: Group) -> Optional[str]:
        """Who taught the lesson: the lesson snapshot, else the group's teacher."""
        teacher_id = attendance.teacher_id or group.teacher_id
        return str(teacher_id) if teacher_id else None

    def get_teacher_dashboard_summary(
        self,
        org_id: str,
        *,
        actor_id: str,
        business_date: date,
    ) -> TeacherDashboardSummaryResponse:
        """Build the teacher's dashboard state in one database-backed read.

        The previous client implementation read one attendance journal for
        every lesson shown today.  Apart from the N+1 latency, a failed
        journal request made an already-started lesson look scheduled.  This
        endpoint derives occurrences and their attendance rows together, so a
        successful response is a consistent snapshot for the same teacher and
        business date.
        """
        if not actor_id:
            raise PermissionError("Не удалось определить преподавателя")

        groups = (
            self.db.query(Group)
            .filter(
                Group.org_id == org_id,
                Group.status == "active",
                Group.teacher_id == actor_id,
            )
            .order_by(Group.name)
            .all()
        )

        # The dashboard uses a short retrospective window to attach an
        # operational exam to its concrete lesson, and six days ahead for the
        # group-focus cards.  It is well within the public 63-day limit.
        window_start = business_date - timedelta(days=30)
        window_end = business_date + timedelta(days=6)
        schedule_window: list[ScheduleOccurrenceResponse] = []
        for group in groups:
            schedule_window.extend(
                ScheduleOccurrenceResponse(**item)
                for item in self._resolved_occurrences_for_group(
                    group,
                    date_from=window_start,
                    date_to=window_end,
                )
            )
        schedule_window.sort(key=lambda item: (item.occurrenceDate, item.startTime, item.groupName))
        today_lessons = [
            occurrence
            for occurrence in schedule_window
            if occurrence.occurrenceDate == business_date.isoformat()
        ]

        group_ids = [group.id for group in groups]
        attendance_by_instance: dict[tuple[str, str], GroupAttendance] = {}
        if group_ids:
            attendance_rows = (
                self.db.query(GroupAttendance)
                .filter(
                    GroupAttendance.org_id == org_id,
                    GroupAttendance.group_id.in_(group_ids),
                    GroupAttendance.lesson_date == business_date.isoformat(),
                )
                .all()
            )
            attendance_by_instance = {
                (str(row.group_id), row.lesson_id): row
                for row in attendance_rows
                if row.lesson_id
            }

        attendance_states = []
        for lesson in today_lessons:
            attendance = attendance_by_instance.get((lesson.groupId, lesson.id))
            attendance_states.append(
                TeacherDashboardLessonAttendanceStateResponse(
                    lessonId=lesson.id,
                    groupId=lesson.groupId,
                    status=attendance.status if attendance else None,
                    hasSavedRecords=bool(attendance and attendance.records),
                )
            )

        # "My students" counts people, not seats: a student who sits in two of
        # the teacher's groups is one student.  The per-group rosters keep
        # their own counts (groups[].student_count).
        distinct_student_count = 0
        if group_ids:
            distinct_student_count = (
                self.db.query(func.count(func.distinct(StudentGroup.student_id)))
                .filter(
                    StudentGroup.group_id.in_(group_ids),
                    StudentGroup.is_active.is_(True),
                )
                .scalar()
            ) or 0

        return TeacherDashboardSummaryResponse(
            businessDate=business_date.isoformat(),
            groups=[self._build_group_response(group) for group in groups],
            studentCount=distinct_student_count,
            todayLessons=today_lessons,
            scheduleWindow=schedule_window,
            openAttendanceActions=self.get_open_attendance_actions(
                org_id,
                actor_id=actor_id,
                is_org_admin=False,
            ),
            attendanceStates=attendance_states,
        )

    def get_group_attendance(
        self,
        org_id: str,
        group_id: str,
        month: Optional[str] = None,
        *,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        actor_id: Optional[str] = None,
        is_org_admin: bool = False,
        is_org_manager: bool = False,
    ) -> GroupAttendanceResponse:
        """Получить экземпляры уроков и отметки за строго заданный период."""
        group = self._require_group_access(
            org_id, group_id, actor_id=actor_id, is_org_admin=is_org_admin
        )

        def parse_iso_date(value: str, field: str) -> date:
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError as exc:
                raise ValueError(f"{field} должен иметь формат YYYY-MM-DD") from exc
            if parsed.isoformat() != value:
                raise ValueError(f"{field} должен иметь формат YYYY-MM-DD")
            return parsed

        if bool(date_from) != bool(date_to):
            raise ValueError("date_from и date_to передаются вместе")
        if date_from and date_to:
            period_start = parse_iso_date(date_from, "date_from")
            period_end = parse_iso_date(date_to, "date_to")
            if period_end < period_start:
                raise ValueError("date_to не может быть раньше date_from")
            if (period_end - period_start).days > 62:
                raise ValueError("Период журнала не может превышать 63 дня")
        else:
            selected_month = month or datetime.now(BUSINESS_TIMEZONE).strftime("%Y-%m")
            try:
                month_start = datetime.strptime(selected_month + "-01", "%Y-%m-%d").date()
            except ValueError as exc:
                raise ValueError("month должен иметь формат YYYY-MM") from exc
            if month_start.strftime("%Y-%m") != selected_month:
                raise ValueError("month должен иметь формат YYYY-MM")
            next_month = (
                date(month_start.year + 1, 1, 1)
                if month_start.month == 12
                else date(month_start.year, month_start.month + 1, 1)
            )
            period_start = month_start
            period_end = next_month - timedelta(days=1)
            month = selected_month

        query = self.db.query(GroupAttendance).filter(
            GroupAttendance.group_id == group_id,
            GroupAttendance.org_id == org_id,
            GroupAttendance.lesson_date >= period_start.isoformat(),
            GroupAttendance.lesson_date <= period_end.isoformat(),
        )
        att_records = query.order_by(GroupAttendance.lesson_date).all()
        records_map: Dict[str, Dict[str, str]] = {}
        statuses_map: Dict[str, str] = {}
        cancellation_reasons_map: Dict[str, str] = {}
        roster_student_ids_map: Dict[str, List[str]] = {}
        lessons_map: Dict[str, Dict[str, Any]] = {}
        attendance_keys: Dict[UUID, str] = {}  # attendance row id -> journal lesson key
        historical_candidate_ids: set[str] = set()
        business_today = datetime.now(BUSINESS_TIMEZONE).date().isoformat()

        for att in att_records:
            # A recurring slot is the source of truth for a future lesson. An
            # attendance row for a future date can only be a legacy/corrupt
            # write (except an explicit cancellation, which is allowed in
            # advance). An open row without an author is likewise not valid
            # attendance. Do not let either state reach a client while the
            # migration quarantines historic records.
            is_invalid_future_row = (
                att.lesson_date > business_today
                and att.status != LessonAttendanceStatus.CANCELLED.value
            )
            is_unattributable_open_row = (
                att.status == LessonAttendanceStatus.IN_PROGRESS.value
                and att.marked_by is None
            )
            if is_invalid_future_row or is_unattributable_open_row:
                logger.error(
                    "Ignoring invalid attendance row %s for group %s, lesson %s",
                    att.id,
                    group.id,
                    att.lesson_id,
                )
                continue
            lesson_key = att.lesson_id or f"lesson_{att.lesson_date}"
            instance_key = f"{att.lesson_date}_{lesson_key}"
            attendance_keys[att.id] = instance_key
            records = dict(att.records or {})
            records_map[instance_key] = records
            statuses_map[instance_key] = att.status or LessonAttendanceStatus.COMPLETED.value
            # New records have a frozen roster. For legacy records, the
            # immutable keys already present in the attendance snapshot are
            # the only honest fallback.
            # ``[]`` is a real frozen roster (for example, a group that had
            # no members on the historical lesson date). Only NULL denotes a
            # legacy row without this evidence and may fall back to records.
            has_frozen_roster = isinstance(att.roster_snapshot, list)
            roster_ids = self._roster_ids_from_snapshot(att.roster_snapshot)
            if not has_frozen_roster:
                roster_ids = {str(student_id) for student_id in records}
            if has_frozen_roster or roster_ids:
                roster_student_ids_map[instance_key] = sorted(roster_ids)
                historical_candidate_ids.update(roster_ids)
            if att.status == LessonAttendanceStatus.CANCELLED.value and att.cancellation_reason:
                cancellation_reasons_map[instance_key] = att.cancellation_reason
            lessons_map[instance_key] = {
                "id": lesson_key,
                "instanceKey": instance_key,
                "date": att.lesson_date,
                "topic": att.topic or "Занятие",
                "status": att.status or LessonAttendanceStatus.COMPLETED.value,
            }

        for occurrence in self._resolved_occurrences_for_group(
            group, date_from=period_start, date_to=period_end
        ):
            date_str = occurrence["occurrenceDate"]
            slot_id = occurrence["id"]
            instance_key = f"{date_str}_{slot_id}"
            topic = occurrence.get("topic") or occurrence.get("subject") or "Занятие"
            if instance_key in lessons_map:
                lessons_map[instance_key]["startTime"] = occurrence["startTime"]
                lessons_map[instance_key]["endTime"] = occurrence["endTime"]
                lessons_map[instance_key]["topic"] = lessons_map[instance_key].get("topic") or topic
                lessons_map[instance_key]["room"] = occurrence.get("room")
                lessons_map[instance_key]["format"] = occurrence.get("format")
                lessons_map[instance_key]["isRescheduled"] = occurrence["isRescheduled"]
                lessons_map[instance_key]["originalStartTime"] = occurrence["originalStartTime"]
                lessons_map[instance_key]["originalEndTime"] = occurrence["originalEndTime"]
                lessons_map[instance_key]["originalRoom"] = occurrence["originalRoom"]
                lessons_map[instance_key]["rescheduleReason"] = occurrence["rescheduleReason"]
            else:
                lessons_map[instance_key] = {
                    "id": slot_id,
                    "instanceKey": instance_key,
                    "date": date_str,
                    "startTime": occurrence["startTime"],
                    "endTime": occurrence["endTime"],
                    "topic": topic,
                    "room": occurrence.get("room"),
                    "format": occurrence.get("format"),
                    "status": "scheduled",
                    "isRescheduled": occurrence["isRescheduled"],
                    "originalStartTime": occurrence["originalStartTime"],
                    "originalEndTime": occurrence["originalEndTime"],
                    "originalRoom": occurrence["originalRoom"],
                    "rescheduleReason": occurrence["rescheduleReason"],
                }

        sorted_lessons = sorted(
            lessons_map.values(),
            key=lambda item: (item.get("date", ""), item.get("startTime", "00:00")),
        )

        active_memberships = (
            self.db.query(StudentGroup)
            .filter(
                StudentGroup.group_id == group.id,
                StudentGroup.is_active.is_(True),
                StudentGroup.left_at.is_(None),
            )
            .all()
        )
        active_student_ids = {
            str(membership.student_id) for membership in active_memberships
        }

        # A transfer is a factual membership event and is useful only in the
        # selected period. Ordinary enrolment is deliberately *not* exposed
        # as a journal event: it must not visually change existing students.
        recent_active_memberships = [
            membership
            for membership in active_memberships
            if self._membership_event_at(membership)
            and period_start <= self._membership_event_at(membership).date() <= period_end
        ]
        membership_events: list[dict[str, Any]] = []
        if recent_active_memberships:
            recent_student_ids = [membership.student_id for membership in recent_active_memberships]
            source_rows = (
                self.db.query(StudentGroup, Group)
                .join(Group, Group.id == StudentGroup.group_id)
                .filter(
                    StudentGroup.student_id.in_(recent_student_ids),
                    StudentGroup.group_id != group.id,
                    StudentGroup.is_active.is_(False),
                    StudentGroup.left_at.isnot(None),
                    Group.org_id == org_id,
                )
                .all()
            )
            sources_by_student_id: dict[UUID, list[tuple[StudentGroup, Group]]] = {}
            for source_membership, source_group in source_rows:
                sources_by_student_id.setdefault(source_membership.student_id, []).append(
                    (source_membership, source_group)
                )

            for membership in recent_active_memberships:
                event_at = self._membership_event_at(membership)
                candidates = sources_by_student_id.get(membership.student_id, [])
                # A transfer closes one membership and opens the next one in
                # the same transaction, so only a closure that coincides with
                # this opening is a transfer.  A student who was removed from
                # another group earlier, or who was *added* to this group
                # while staying in the others, is never labelled "transferred".
                source = min(
                    (
                        item
                        for item in candidates
                        if event_at
                        and item[0].left_at
                        and abs((event_at - item[0].left_at).total_seconds())
                        <= TRANSFER_WINDOW_SECONDS
                    ),
                    key=lambda item: abs((event_at - item[0].left_at).total_seconds()),
                    default=None,
                )
                if source:
                    membership_events.append({
                        "student_id": str(membership.student_id),
                        "kind": "transferred",
                        "joined_at": event_at.isoformat() if event_at else None,
                        "source_group_name": source[1].name,
                    })

        # "New after diagnostic" is a separate operational signal. It is
        # shown only in a student's *primary* group, and only while that is
        # the student's first group after a completed *initial* diagnostic;
        # it disappears after the current group teacher opens that exact
        # report. Transfers (and a second group added later) never receive it.
        diagnostic_report_notices: list[dict[str, Any]] = []
        if group.teacher_id and active_memberships:
            active_ids = [membership.student_id for membership in active_memberships]
            primary_ids = {
                row[0]
                for row in self.db.query(Student.id)
                .filter(Student.id.in_(active_ids), Student.group_id == group.id)
                .all()
            }
            other_memberships = (
                self.db.query(StudentGroup.student_id, StudentGroup.joined_at)
                .filter(
                    StudentGroup.student_id.in_(active_ids),
                    StudentGroup.group_id != group.id,
                )
                .all()
            )
            other_joined_by_student: dict[UUID, list[Optional[datetime]]] = {}
            for other_student_id, other_joined_at in other_memberships:
                other_joined_by_student.setdefault(other_student_id, []).append(other_joined_at)
            initial_results = (
                self.db.query(StudentDiagnosticResult)
                .filter(
                    StudentDiagnosticResult.org_id == org_id,
                    StudentDiagnosticResult.student_id.in_(active_ids),
                    StudentDiagnosticResult.diagnostic_type == "initial",
                    StudentDiagnosticResult.invite_id.isnot(None),
                )
                .order_by(StudentDiagnosticResult.diagnosed_at.desc())
                .all()
            )
            results_by_student_id: dict[UUID, list[StudentDiagnosticResult]] = {}
            for result in initial_results:
                results_by_student_id.setdefault(result.student_id, []).append(result)

            for membership in active_memberships:
                if membership.student_id not in primary_ids or not membership.joined_at:
                    continue
                # Not the first group: the student already belonged to another
                # one before (unknown join time counts as earlier).
                if any(
                    other is None or other < membership.joined_at
                    for other in other_joined_by_student.get(membership.student_id, [])
                ):
                    continue
                result = next(
                    (
                        candidate
                        for candidate in results_by_student_id.get(membership.student_id, [])
                        if candidate.diagnosed_at and candidate.diagnosed_at <= membership.joined_at
                    ),
                    None,
                )
                if not result or membership.initial_diagnostic_reviewed_invite_id == result.invite_id:
                    continue
                diagnostic_report_notices.append({
                    "student_id": str(membership.student_id),
                    "invite_id": str(result.invite_id),
                    "completed_at": result.diagnosed_at.isoformat() if result.diagnosed_at else None,
                })

        former_student_ids = historical_candidate_ids - active_student_ids
        historical_students: list[dict[str, Any]] = []
        if former_student_ids:
            former_uuid_ids = []
            for raw_id in former_student_ids:
                try:
                    former_uuid_ids.append(UUID(raw_id))
                except (TypeError, ValueError):
                    logger.warning("Ignoring malformed historical attendance student id %s", raw_id)
            memberships = (
                self.db.query(StudentGroup)
                .filter(
                    StudentGroup.group_id == group.id,
                    StudentGroup.student_id.in_(former_uuid_ids),
                )
                .all()
            ) if former_uuid_ids else []
            memberships_by_student_id = {membership.student_id: membership for membership in memberships}
            destination_rows = (
                self.db.query(StudentGroup, Group)
                .join(Group, Group.id == StudentGroup.group_id)
                .filter(
                    StudentGroup.student_id.in_(former_uuid_ids),
                    StudentGroup.is_active.is_(True),
                    StudentGroup.left_at.is_(None),
                    Group.org_id == org_id,
                )
                .all()
            ) if former_uuid_ids else []
            # "Transferred to ..." only when that membership was opened by the
            # same transfer that closed this one; a student who simply left
            # this group while staying in others has no destination.
            destinations_by_student_id: dict[UUID, str] = {}
            for destination, destination_group in destination_rows:
                closed = memberships_by_student_id.get(destination.student_id)
                destination_at = self._membership_event_at(destination)
                if not (closed and closed.left_at and destination_at):
                    continue
                if abs((destination_at - closed.left_at).total_seconds()) <= TRANSFER_WINDOW_SECONDS:
                    destinations_by_student_id[destination.student_id] = destination_group.name
            students_by_id = {
                student.id: student
                for student in self.db.query(Student).filter(Student.id.in_(former_uuid_ids)).all()
            } if former_uuid_ids else {}
            for student_uuid in former_uuid_ids:
                student = students_by_id.get(student_uuid)
                if not student:
                    continue
                membership = memberships_by_student_id.get(student_uuid)
                full_name = f"{student.last_name or ''} {student.first_name or ''}".strip() or "Ученик"
                historical_students.append({
                    "id": str(student.id),
                    "first_name": student.first_name,
                    "last_name": student.last_name,
                    "full_name": full_name,
                    "class_level": student.class_level,
                    "display_id": student.display_id,
                    "left_at": membership.left_at.isoformat() if membership and membership.left_at else None,
                    "transferred_to_group_name": destinations_by_student_id.get(student.id),
                })
            historical_students.sort(key=lambda item: (item.get("left_at") or "", item["full_name"]))

        excuses_map, corrections_map = AttendanceExcuseService(self.db).journal_payload(
            org_id, attendance_keys, include_review_note=is_org_manager
        )

        return GroupAttendanceResponse(
            groupId=str(group.id),
            month=month,
            lessons=sorted_lessons,
            records=records_map,
            statuses=statuses_map,
            cancellationReasons=cancellation_reasons_map,
            rosterStudentIds=roster_student_ids_map,
            historicalStudents=historical_students,
            membershipEvents=membership_events,
            diagnosticReportNotices=diagnostic_report_notices,
            excuses=excuses_map,
            corrections=corrections_map,
        )

    def reconcile_missed_group_attendance(
        self,
        org_id: str,
        group_id: str,
        req: StartAttendanceRequest,
        *,
        reconciled_by: Optional[str],
        is_org_admin: bool,
    ) -> bool:
        """Open a historical journal without claiming that it was started now.

        This is a reconciliation decision, not a delayed "start lesson"
        operation: ``started_at`` stays NULL and the audit records who opened
        the historical journal and when.
        """
        if not reconciled_by:
            raise ValueError("Не удалось определить автора разбора урока")
        group = self._require_group_access(
            org_id, group_id, actor_id=reconciled_by, is_org_admin=is_org_admin
        )
        locked_group = (
            self.db.query(Group)
            .filter(Group.id == group.id, Group.org_id == org_id)
            .with_for_update()
            .first()
        )
        if not locked_group:
            raise ValueError("Группа не найдена")
        group = locked_group
        occurrence = self._resolve_reconcilable_missed_occurrence(
            group, lesson_date=req.lessonDate, lesson_id=req.lessonId
        )

        attendance = (
            self.db.query(GroupAttendance)
            .filter(
                GroupAttendance.group_id == group_id,
                GroupAttendance.org_id == org_id,
                GroupAttendance.lesson_date == req.lessonDate.isoformat(),
                GroupAttendance.lesson_id == req.lessonId,
            )
            .with_for_update()
            .first()
        )
        if attendance:
            if attendance.status == LessonAttendanceStatus.IN_PROGRESS.value:
                return True  # Idempotent double click: preserve the original audit row.
            if attendance.status == LessonAttendanceStatus.CANCELLED.value:
                raise ValueError("Пропущенный урок уже отменён")
            raise ValueError("Пропущенный урок уже завершён")

        start_minutes, _ = self._validate_lesson_time_range(
            occurrence.get("startTime"), occurrence.get("endTime")
        )
        scheduled_start = lesson_start_as_naive_utc(req.lessonDate, start_minutes)
        roster_snapshot = self._attendance_roster_at(group.id, scheduled_start)
        if not roster_snapshot:
            # The same rule that keeps such a lesson out of the open-lessons list
            # (get_open_attendance_actions): nobody belonged to the group at its start.
            raise ValueError(
                "На дату урока в группе не было учеников — урок нечего отмечать. "
                "Отметьте «Урока не было», если нужно"
            )
        attendance = GroupAttendance(
            org_id=org_id,
            group_id=group_id,
            teacher_id=self._attendance_teacher_snapshot(group),
            lesson_date=req.lessonDate.isoformat(),
            lesson_id=req.lessonId,
            topic=occurrence.get("topic") or req.topic or occurrence.get("subject"),
            status=LessonAttendanceStatus.IN_PROGRESS.value,
            records={},
            roster_snapshot=roster_snapshot,
            marked_by=reconciled_by,
            # Do not fabricate an actual start timestamp for a lesson that
            # was not started in the system at the time.
            started_at=None,
        )
        self.db.add(attendance)
        self.db.flush()
        self.db.add(
            GroupAttendanceAudit(
                attendance_id=attendance.id,
                org_id=org_id,
                group_id=group_id,
                actor_id=reconciled_by,
                action="reconciliation_started",
                before_status=None,
                after_status=LessonAttendanceStatus.IN_PROGRESS.value,
                before_records=None,
                after_records={},
            )
        )
        self.db.commit()
        return True

    def cancel_missed_group_attendance(
        self,
        org_id: str,
        group_id: str,
        req: ReconcileMissedLessonRequest,
        *,
        cancelled_by: Optional[str],
        is_org_admin: bool,
    ) -> bool:
        """Record an explicit, reasoned cancellation for a missed lesson."""
        if not cancelled_by:
            raise ValueError("Не удалось определить автора отмены урока")
        group = self._require_group_access(
            org_id, group_id, actor_id=cancelled_by, is_org_admin=is_org_admin
        )
        locked_group = (
            self.db.query(Group)
            .filter(Group.id == group.id, Group.org_id == org_id)
            .with_for_update()
            .first()
        )
        if not locked_group:
            raise ValueError("Группа не найдена")
        group = locked_group
        occurrence = self._resolve_reconcilable_missed_occurrence(
            group, lesson_date=req.lessonDate, lesson_id=req.lessonId
        )
        attendance = (
            self.db.query(GroupAttendance)
            .filter(
                GroupAttendance.group_id == group_id,
                GroupAttendance.org_id == org_id,
                GroupAttendance.lesson_date == req.lessonDate.isoformat(),
                GroupAttendance.lesson_id == req.lessonId,
            )
            .with_for_update()
            .first()
        )
        if attendance:
            if attendance.status == LessonAttendanceStatus.CANCELLED.value:
                return True  # The same cancellation was already committed.
            raise ValueError("Нельзя отменить урок, который уже начали или завершили")

        self._sync_exam_lesson_cancellation(
            phase="cancellation-check",
            org_id=org_id,
            group_id=group_id,
            lesson_id=req.lessonId,
            lesson_date=req.lessonDate.isoformat(),
            reason=req.cancellationReason,
        )
        start_minutes, _ = self._validate_lesson_time_range(
            occurrence.get("startTime"), occurrence.get("endTime")
        )
        scheduled_start = lesson_start_as_naive_utc(req.lessonDate, start_minutes)
        attendance = GroupAttendance(
            org_id=org_id,
            group_id=group_id,
            teacher_id=self._attendance_teacher_snapshot(group),
            lesson_date=req.lessonDate.isoformat(),
            lesson_id=req.lessonId,
            topic=occurrence.get("topic") or occurrence.get("subject"),
            status=LessonAttendanceStatus.CANCELLED.value,
            records={},
            roster_snapshot=self._attendance_roster_at(group.id, scheduled_start),
            marked_by=cancelled_by,
            started_at=None,
            cancellation_reason=req.cancellationReason,
        )
        self.db.add(attendance)
        self.db.flush()
        self.db.add(
            GroupAttendanceAudit(
                attendance_id=attendance.id,
                org_id=org_id,
                group_id=group_id,
                actor_id=cancelled_by,
                action="reconciliation_cancelled",
                before_status=None,
                after_status=LessonAttendanceStatus.CANCELLED.value,
                before_records=None,
                after_records={},
                cancellation_reason=req.cancellationReason,
            )
        )
        try:
            self._sync_exam_lesson_cancellation(
                phase="release",
                org_id=org_id,
                group_id=group_id,
                lesson_id=req.lessonId,
                lesson_date=req.lessonDate.isoformat(),
                reason=req.cancellationReason,
            )
        except Exception:
            self.db.rollback()
            raise
        self.db.commit()
        return True

    def start_group_attendance(
        self,
        org_id: str,
        group_id: str,
        req: StartAttendanceRequest,
        *,
        started_by: Optional[str],
        is_org_admin: bool,
    ) -> bool:
        """Create the only valid transition from scheduled to in_progress."""
        if not started_by:
            raise ValueError("Не удалось определить преподавателя, начавшего урок")
        group = self._require_group_access(
            org_id, group_id, actor_id=started_by, is_org_admin=is_org_admin
        )
        # The same group lock is acquired by a membership transfer. It makes
        # "start lesson" and "move student" mutually exclusive operations.
        locked_group = (
            self.db.query(Group)
            .filter(Group.id == group.id, Group.org_id == org_id)
            .with_for_update()
            .first()
        )
        if not locked_group:
            raise ValueError("Группа не найдена")
        group = locked_group
        lesson_date = req.lessonDate
        today = datetime.now(BUSINESS_TIMEZONE).date()
        if lesson_date != today:
            raise ValueError("Начать урок можно только в день его проведения")

        matching_slot = next(
            (
                occurrence
                for occurrence in self._resolved_occurrences_for_group(
                    group, date_from=lesson_date, date_to=lesson_date
                )
                if occurrence["id"] == req.lessonId
            ),
            None,
        )
        if not matching_slot:
            raise ValueError("Урок не принадлежит расписанию этой группы")
        if int(matching_slot.get("dayOfWeek") or 0) != lesson_date.isoweekday():
            raise ValueError("Дата не соответствует дню урока в расписании")

        start_minutes, end_minutes = self._validate_lesson_time_range(
            matching_slot.get("startTime"), matching_slot.get("endTime")
        )
        current_business_time = datetime.now(BUSINESS_TIMEZONE)
        current_minutes = current_business_time.hour * 60 + current_business_time.minute
        if current_minutes < start_minutes:
            raise ValueError("Нельзя начать урок до времени по расписанию")

        # Serialize starts for the teacher who is assigned to this group.  It
        # prevents two browser tabs (or two groups) from creating overlapping
        # active lessons between the check below and the insert.
        lesson_teacher_id = group.teacher_id or started_by
        self.db.query(User.id).filter(User.id == lesson_teacher_id).with_for_update().first()

        att = (
            self.db.query(GroupAttendance)
            .filter(
                GroupAttendance.group_id == group_id,
                GroupAttendance.org_id == org_id,
                GroupAttendance.lesson_date == lesson_date.isoformat(),
                GroupAttendance.lesson_id == req.lessonId,
            )
            .first()
        )
        if att:
            if att.status == LessonAttendanceStatus.CANCELLED.value:
                raise ValueError("Отменённый урок нельзя начать")
            if att.status == LessonAttendanceStatus.COMPLETED.value:
                raise ValueError("Завершённый урок нельзя начать повторно")
            # Idempotency protects a double click/retry; it does not alter the
            # original actual start timestamp or audit trail.
            return True

        # A real teacher cannot conduct two lessons at the same time.  The
        # previous occurrence must first be completed or cancelled, otherwise
        # attendance and future worked-hours analytics would contain two
        # simultaneous sessions for one teacher.
        active_lesson_query = (
            self.db.query(GroupAttendance, Group)
            .join(Group, Group.id == GroupAttendance.group_id)
            .filter(
                GroupAttendance.org_id == org_id,
                GroupAttendance.lesson_date == lesson_date.isoformat(),
                GroupAttendance.status == LessonAttendanceStatus.IN_PROGRESS.value,
            )
        )
        if group.teacher_id:
            active_lesson_query = active_lesson_query.filter(
                Group.teacher_id == group.teacher_id
            )
        else:
            # Unassigned legacy groups have no designated instructor.  The
            # actor who started the lesson is the only reliable identity.
            active_lesson_query = active_lesson_query.filter(
                GroupAttendance.marked_by == started_by
            )

        active_lesson = active_lesson_query.first()
        if active_lesson:
            active_attendance, active_group = active_lesson
            active_label = active_attendance.topic or active_group.name
            raise ValueError(
                f"Сначала завершите или отмените начатый урок «{active_label}» "
                f"в группе «{active_group.name}». Один преподаватель не может вести два урока одновременно."
            )

        # A teacher may recover a forgotten lesson after its scheduled end,
        # but only until a later lesson has actually been started. Once that
        # happened, starting the older occurrence would fabricate a parallel
        # or retroactive lesson and corrupt the attendance chronology.
        planned_end_at = current_business_time.replace(
            hour=end_minutes // 60,
            minute=end_minutes % 60,
            second=0,
            microsecond=0,
            tzinfo=None,
        )
        subsequent_lesson_query = (
            self.db.query(GroupAttendance, Group)
            .join(Group, Group.id == GroupAttendance.group_id)
            .filter(
                GroupAttendance.org_id == org_id,
                GroupAttendance.lesson_date == lesson_date.isoformat(),
                GroupAttendance.started_at.isnot(None),
                GroupAttendance.started_at >= planned_end_at,
            )
        )
        if group.teacher_id:
            subsequent_lesson_query = subsequent_lesson_query.filter(
                Group.teacher_id == group.teacher_id
            )
        else:
            subsequent_lesson_query = subsequent_lesson_query.filter(
                GroupAttendance.marked_by == started_by
            )
        subsequent_lesson = subsequent_lesson_query.order_by(
            GroupAttendance.started_at.asc()
        ).first()
        if subsequent_lesson:
            subsequent_attendance, subsequent_group = subsequent_lesson
            subsequent_label = subsequent_attendance.topic or subsequent_group.name
            raise ValueError(
                f"Нельзя начать этот урок: после его планового окончания уже был начат "
                f"урок «{subsequent_label}» в группе «{subsequent_group.name}». "
                "Отмените пропущенное занятие с причиной."
            )

        now = current_business_time.replace(tzinfo=None)
        roster_snapshot = self._active_attendance_roster(group.id)
        att = GroupAttendance(
            org_id=org_id,
            group_id=group_id,
            teacher_id=self._attendance_teacher_snapshot(group),
            lesson_date=lesson_date.isoformat(),
            lesson_id=req.lessonId,
            topic=req.topic or matching_slot.get("topic"),
            status=LessonAttendanceStatus.IN_PROGRESS.value,
            records={},
            roster_snapshot=roster_snapshot,
            marked_by=started_by,
            started_at=now,
        )
        self.db.add(att)
        self.db.flush()
        self.db.add(
            GroupAttendanceAudit(
                attendance_id=att.id,
                org_id=org_id,
                group_id=group_id,
                actor_id=started_by,
                action="started",
                before_status=None,
                after_status=LessonAttendanceStatus.IN_PROGRESS.value,
                before_records=None,
                after_records={},
            )
        )
        self.db.commit()
        return True

    def save_group_attendance(
        self,
        org_id: str,
        group_id: str,
        req: SaveAttendanceRequest,
        *,
        marked_by: Optional[str],
        is_org_admin: bool,
        is_org_manager: bool = False,
    ) -> bool:
        """Сохранить полный проверенный снимок отметок одного экземпляра урока."""
        if not marked_by:
            raise ValueError("Не удалось определить автора отметки")
        group = self._require_group_access(
            org_id, group_id, actor_id=marked_by, is_org_admin=is_org_admin
        )
        lesson_date = req.lessonDate
        today = datetime.now(BUSINESS_TIMEZONE).date()
        if (
            lesson_date > today
            and req.status.value != LessonAttendanceStatus.CANCELLED.value
        ):
            raise ValueError("Нельзя отмечать посещаемость будущего урока")

        if not req.lessonId:
            raise ValueError("Для посещаемости необходим идентификатор урока из расписания")
        target_lesson_id = req.lessonId or f"lesson_{lesson_date.isoformat()}"
        requested_status = req.status.value
        is_cancellation = requested_status == LessonAttendanceStatus.CANCELLED.value
        att = (
            self.db.query(GroupAttendance)
            .filter(
                GroupAttendance.group_id == group_id,
                GroupAttendance.org_id == org_id,
                GroupAttendance.lesson_date == lesson_date.isoformat(),
                GroupAttendance.lesson_id == target_lesson_id,
            )
            .with_for_update()
            .first()
        )

        matching_slot = next(
            (
                occurrence
                for occurrence in self._resolved_occurrences_for_group(
                    group, date_from=lesson_date, date_to=lesson_date
                )
                if occurrence["id"] == target_lesson_id
            ),
            None,
        )
        if not att:
            if not matching_slot:
                raise ValueError("Урок не принадлежит расписанию этой группы")
            slot_day = int(matching_slot.get("dayOfWeek") or 0)
            if slot_day != lesson_date.isoweekday():
                raise ValueError("Дата не соответствует дню урока в расписании")

        if not is_cancellation and not att:
            raise ValueError("Сначала начните урок, затем отмечайте посещаемость")
        # An administrator may correct a completed lesson (a reason is required
        # below); anyone else only edits a lesson that is in progress.
        is_correction_save = bool(
            att
            and not is_cancellation
            and is_org_manager
            and att.status == LessonAttendanceStatus.COMPLETED.value
        )
        if (
            att
            and not is_cancellation
            and att.status == LessonAttendanceStatus.COMPLETED.value
            and is_org_admin
            and not is_org_manager
        ):
            # Only the organisation's owner/admin corrects completed lessons;
            # a global "admin" role without that membership does not.
            raise PermissionError("Корректировать завершённый урок может только owner или admin")
        if (
            not is_cancellation
            and att.status != LessonAttendanceStatus.IN_PROGRESS.value
            and not is_correction_save
        ):
            raise ValueError("Отметки можно изменять только у начатого урока")

        if matching_slot and lesson_date == today and not is_cancellation and not is_correction_save:
            start_time = matching_slot.get("startTime")
            end_time = matching_slot.get("endTime")
            start_minutes, end_minutes = self._validate_lesson_time_range(start_time, end_time)
            current_business_time = datetime.now(BUSINESS_TIMEZONE)
            current_minutes = current_business_time.hour * 60 + current_business_time.minute
            if current_minutes < start_minutes:
                raise ValueError("Нельзя начать или отмечать посещаемость до времени урока")
            if (
                requested_status == LessonAttendanceStatus.COMPLETED.value
                and current_minutes < end_minutes - 15
            ):
                raise ValueError("Урок можно завершить не ранее чем за 15 минут до окончания")

        # A lesson owns the roster that existed at its explicit start. Never
        # derive it from today's group membership: an administrative transfer
        # after the lesson must not rewrite yesterday's attendance evidence.
        if (
            att
            and not is_cancellation
            and att.status == LessonAttendanceStatus.IN_PROGRESS.value
            and att.roster_snapshot is None
        ):
            # Safe compatibility path for lessons started before the roster
            # snapshot migration. New lessons always receive this at start.
            att.roster_snapshot = self._active_attendance_roster(group.id)
            flag_modified(att, "roster_snapshot")
        roster_student_ids = set()
        if att and not is_cancellation:
            roster_student_ids = self._roster_ids_from_snapshot(att.roster_snapshot)
            if not roster_student_ids:
                # A completed legacy lesson cannot be rebuilt from the current
                # membership: the student may have been transferred since it
                # happened. Its saved record keys are the only durable roster.
                roster_student_ids = {str(student_id) for student_id in (att.records or {})}
            if (
                not roster_student_ids
                and att.status == LessonAttendanceStatus.IN_PROGRESS.value
                and att.roster_snapshot is None
            ):
                roster_student_ids = {
                    entry["student_id"]
                    for entry in self._active_attendance_roster(group.id)
                }
        records = {str(student_id): state.value for student_id, state in req.records.items()}
        unknown_student_ids = set(records) - roster_student_ids
        if unknown_student_ids:
            raise ValueError("В журнал передан ученик, которого не было в составе начатого урока")

        if is_cancellation:
            if records:
                raise ValueError("У отменённого урока не может быть отметок посещаемости")
            if not req.cancellationReason:
                raise ValueError("Для отмены урока укажите причину")
        elif req.cancellationReason:
            raise ValueError("Причина отмены передаётся только для отменённого урока")
        if requested_status == LessonAttendanceStatus.COMPLETED.value:
            missing_student_ids = roster_student_ids - set(records)
            if missing_student_ids:
                raise ValueError("Перед завершением урока заполните отметки всех учеников из состава урока")

        # Reasons for new "excused" marks are validated before any write.
        excuse_service = AttendanceExcuseService(self.db)
        excuse_plan = excuse_service.plan_save(att, records, req.excuseReasons)

        now = datetime.now(BUSINESS_TIMEZONE).replace(tzinfo=None)
        before_status = att.status if att else None
        before_records = dict(att.records or {}) if att else None
        is_completed_correction = bool(
            att
            and att.status == LessonAttendanceStatus.COMPLETED.value
            and (records != before_records or requested_status != before_status)
        )
        if is_completed_correction:
            if not is_org_manager:
                raise PermissionError("Корректировать завершённый урок может только owner или admin")
            if not req.correctionReason:
                raise ValueError("Для корректировки завершённого урока укажите причину")

        if att and att.status == LessonAttendanceStatus.COMPLETED.value and requested_status != before_status:
            raise ValueError("Завершённый урок нельзя вернуть в другой статус")
        if att and att.status == LessonAttendanceStatus.CANCELLED.value and requested_status != before_status:
            raise ValueError("Отменённый урок нельзя повторно открыть")

        # Reconciliation starts a historical missed lesson without fabricating a
        # real start timestamp. Preserve that distinct workflow in the audit if
        # the teacher later decides it was opened by mistake and cancels it.
        is_reconciliation_cancellation = False
        if is_cancellation and att and att.status == LessonAttendanceStatus.IN_PROGRESS.value:
            is_reconciliation_cancellation = (
                self.db.query(GroupAttendanceAudit.id)
                .filter(
                    GroupAttendanceAudit.attendance_id == att.id,
                    GroupAttendanceAudit.action == "reconciliation_started",
                )
                .first()
                is not None
            )

        # A cancelled lesson must not silently invalidate a live exam or erase
        # a student waiting in its lobby.  Exam-service is the owner of those
        # rows, so it is consulted before this service persists the
        # cancellation.  This protects direct API callers as well as the UI.
        if is_cancellation:
            self._sync_exam_lesson_cancellation(
                phase="cancellation-check",
                org_id=org_id,
                group_id=group_id,
                lesson_id=target_lesson_id,
                lesson_date=lesson_date.isoformat(),
                reason=req.cancellationReason,
            )

        if not att:
            att = GroupAttendance(
                org_id=org_id,
                group_id=group_id,
                teacher_id=self._attendance_teacher_snapshot(group),
                lesson_date=lesson_date.isoformat(),
                lesson_id=target_lesson_id,
                topic=req.topic,
                status=requested_status,
                records=records,
                marked_by=marked_by,
                started_at=None,
                completed_at=now if requested_status == LessonAttendanceStatus.COMPLETED.value else None,
                cancellation_reason=req.cancellationReason if is_cancellation else None,
            )
            self.db.add(att)
            action = "cancelled" if is_cancellation else "created"
        else:
            att.records = records
            flag_modified(att, "records")
            att.status = requested_status
            if req.topic is not None:
                att.topic = req.topic
            if not is_correction_save:
                att.marked_by = marked_by
            if requested_status == LessonAttendanceStatus.COMPLETED.value:
                att.completed_at = att.completed_at or now
            else:
                att.completed_at = None
            att.cancellation_reason = req.cancellationReason if is_cancellation else None
            action = (
                "reconciliation_cancelled"
                if is_cancellation and is_reconciliation_cancellation
                else "cancelled"
                if is_cancellation
                else "corrected"
                if is_completed_correction
                else "updated"
            )

        self.db.flush()
        excuse_service.apply_save(att, excuse_plan, marked_by, now)
        self.db.add(
            GroupAttendanceAudit(
                attendance_id=att.id,
                org_id=org_id,
                group_id=group_id,
                actor_id=marked_by,
                action=action,
                before_status=before_status,
                after_status=requested_status,
                before_records=before_records,
                after_records=records,
                correction_reason=req.correctionReason if is_completed_correction else None,
                cancellation_reason=req.cancellationReason if is_cancellation else None,
            )
        )
        self.db.flush()
        if is_cancellation:
            try:
                # The request is deliberately made before the local commit:
                # if exam-service rejects a race (a student joined just now),
                # both the attendance write and audit record are rolled back.
                self._sync_exam_lesson_cancellation(
                    phase="release",
                    org_id=org_id,
                    group_id=group_id,
                    lesson_id=target_lesson_id,
                    lesson_date=lesson_date.isoformat(),
                    reason=req.cancellationReason,
                )
            except Exception:
                self.db.rollback()
                raise
        self.db.commit()
        return True

    def _sync_exam_lesson_cancellation(
        self,
        *,
        phase: str,
        org_id: str,
        group_id: str,
        lesson_id: str,
        lesson_date: str,
        reason: Optional[str],
    ) -> None:
        """Synchronise one lesson cancellation with exam-service.

        Auth owns the cancellation, exam-service owns attempts and access
        codes.  The two short internal calls form a small guarded saga: first
        check that no active work exists, then revoke an untouched lobby while
        this attendance transaction is still uncommitted.
        """
        settings = get_settings()
        if not settings.internal_service_token:
            raise RuntimeError("Не настроена внутренняя связка отмены урока и экзаменов")
        endpoint = f"{settings.exam_service_url.rstrip('/')}/internal/lesson-bindings/{phase}"
        payload = {
            "org_id": str(org_id),
            "group_id": str(group_id),
            "lesson_id": lesson_id,
            "lesson_date": lesson_date,
            "reason": reason,
        }
        try:
            response = httpx.post(
                endpoint,
                json=payload,
                headers={"X-Internal-Service-Token": settings.internal_service_token},
                timeout=5.0,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError("Сервис экзаменов временно недоступен; урок не был отменён") from exc

        if response.status_code == 409:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            raise ValueError(detail or "Нельзя отменить урок с активным экзаменом")
        if response.status_code != 200:
            logger.error(
                "Exam lesson cancellation sync failed: phase=%s status=%s body=%s",
                phase,
                response.status_code,
                response.text[:500],
            )
            raise RuntimeError("Не удалось синхронизировать отмену урока с экзаменом")

    def _fetch_exam_recurring_end_impact(
        self,
        *,
        org_id: str,
        group_id: str,
        lesson_id: str,
        effective_from: str,
    ) -> Dict[str, Any]:
        """Read the canonical future-exam impact from exam-service.

        Auth owns the schedule but must not infer whether an exam has a
        session. That data belongs to exam-service, so a missing/failed
        internal connection blocks the operation rather than reporting a
        misleading zero-impact preview.
        """
        settings = get_settings()
        if not settings.internal_service_token:
            raise RuntimeError("Не настроена внутренняя связка расписания и экзаменов")
        endpoint = f"{settings.exam_service_url.rstrip('/')}/internal/lesson-bindings/recurring-end-impact"
        try:
            response = httpx.post(
                endpoint,
                json={
                    "org_id": str(org_id),
                    "group_id": str(group_id),
                    "lesson_id": lesson_id,
                    "effective_from": effective_from,
                },
                headers={"X-Internal-Service-Token": settings.internal_service_token},
                timeout=5.0,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError("Сервис экзаменов временно недоступен; остановка расписания не выполнена") from exc
        if response.status_code != 200:
            logger.error(
                "Exam recurring-end impact failed: status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            raise RuntimeError("Не удалось проверить связанные экзамены")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("Сервис экзаменов вернул некорректный ответ") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Сервис экзаменов вернул некорректный ответ")
        return payload

    def _release_exam_recurring_end(
        self,
        *,
        org_id: str,
        group_id: str,
        group_name: str,
        lesson_id: str,
        effective_from: str,
        actor_id: str,
        reason: str,
    ) -> List[str]:
        """Atomically release only the future deliveries approved in preview."""
        settings = get_settings()
        if not settings.internal_service_token:
            raise RuntimeError("Не настроена внутренняя связка расписания и экзаменов")
        endpoint = f"{settings.exam_service_url.rstrip('/')}/internal/lesson-bindings/recurring-end-release"
        try:
            response = httpx.post(
                endpoint,
                json={
                    "org_id": str(org_id),
                    "group_id": str(group_id),
                    "group_name": group_name,
                    "lesson_id": lesson_id,
                    "effective_from": effective_from,
                    "actor_id": actor_id,
                    "reason": reason,
                },
                headers={"X-Internal-Service-Token": settings.internal_service_token},
                timeout=5.0,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError("Сервис экзаменов временно недоступен; регулярное расписание не прекращено") from exc
        if response.status_code == 409:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            raise ValueError(detail or "Нельзя прекратить расписание с активным экзаменом")
        if response.status_code != 200:
            logger.error(
                "Exam recurring-end release failed: status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            raise RuntimeError("Не удалось безопасно вернуть экзамены в черновики")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("Сервис экзаменов вернул некорректный ответ") from exc
        released = payload.get("released_exam_ids") if isinstance(payload, dict) else None
        if not isinstance(released, list):
            raise RuntimeError("Сервис экзаменов не подтвердил возврат экзаменов")
        return [str(exam_id) for exam_id in released]

    def _sync_exam_lesson_reschedule(
        self,
        *,
        org_id: str,
        group_id: str,
        group_name: Optional[str],
        lesson_id: str,
        lesson_date: str,
        actor_id: str,
        reason: Optional[str],
        previous: Dict[str, Any],
        current: Dict[str, Any],
    ) -> None:
        """Persist a schedule transfer in exam-service's audit timeline.

        The concrete lesson identity does not change, therefore this is not a
        new exam assignment and cannot change a public code, attempts or the
        lesson itself.  Failing closed prevents a successful reschedule from
        disappearing from the operational history.
        """
        settings = get_settings()
        if not settings.internal_service_token:
            raise RuntimeError("Не настроена внутренняя связка расписания и экзаменов")
        endpoint = f"{settings.exam_service_url.rstrip('/')}/internal/lesson-bindings/rescheduled"
        payload = {
            "org_id": str(org_id),
            "group_id": str(group_id),
            "group_name": group_name,
            "lesson_id": lesson_id,
            "lesson_date": lesson_date,
            "actor_id": actor_id,
            "reason": reason,
            "previous": previous,
            "current": current,
        }
        try:
            response = httpx.post(
                endpoint,
                json=payload,
                headers={"X-Internal-Service-Token": settings.internal_service_token},
                timeout=5.0,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError("Сервис экзаменов временно недоступен; урок не был перенесён") from exc
        if response.status_code != 200:
            logger.error(
                "Exam lesson reschedule audit failed: status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            raise RuntimeError("Не удалось зафиксировать перенос занятия для экзамена")
