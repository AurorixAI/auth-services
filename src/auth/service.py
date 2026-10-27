"""
ALGO V1 - Auth Service
B2B: Organizations → Members (owner/admin/teacher) → Students (no login)
JWT, bcrypt, org CRUD, invite flow, student management, diagnostic invites
"""

import os
import hashlib
import hmac
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

from . import payment_status as ps
from . import student_identity
from . import user_sessions
from .student_id_service import assign_display_id
from .attendance_excuses import AttendanceExcuseService
from .diagnostic_level import level_fields, normalize_level_payload
from .membership_pauses import not_paused_at, not_paused_now
from .leave_service import on_membership_closed, on_membership_returned
from .membership_periods import (
    close_period,
    group_period_spans,
    member_at,
    open_period,
    periods_by_membership,
    periods_payload,
)
from .student_archive import (
    ARCHIVED,
    ARCHIVED_STUDENT_INVITE_MESSAGE,
    INVITE_CANCEL_REASON,
    INVITE_CANCELLED_MESSAGE,
    ArchiveConflict,
    archive_stamp,
    archived_groups_payload,
    attach_archived_groups,
    guard_generic_status_update,
    student_not_archived,
)
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
    DEFAULT_CANCELLATION_KIND,
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
from src.core.exceptions import InvalidCredentialsError

# LOGIC-B: the answers that grew (room may be null, validFrom, teacher of a lesson, ...) are the subclasses of ops_schemas, used under the old names.
from .ops_schemas import (  # noqa: E402
    GroupAttendanceResponseB as GroupAttendanceResponse,
    EndRecurringLessonResponseB as EndRecurringLessonResponse,
    GroupResponseB as GroupResponse,
    OpenAttendanceActionResponseB as OpenAttendanceActionResponse,
    ScheduleLessonResponseB as ScheduleLessonResponse,
    ScheduleLessonRevisedB,
    ScheduleOccurrenceResponseB as ScheduleOccurrenceResponse,
    StudentResponseB as StudentResponse,
    TeacherDashboardSummaryResponseB as TeacherDashboardSummaryResponse,
)
from . import attendance_rules, business_clock, invites, lesson_substitutions, lesson_teachers, ops_rules, schedule_rules  # noqa: E402
from . import staff_lifecycle, student_views  # noqa: E402

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
# SV-01: APP_ENV production/staging refuses an empty, default or short (<32) signing key (src/core/startup_guard.py)
from src.core.startup_guard import diagnostic_base_url, load_signing_secrets
SECRET_KEY, JWT_SECRET = load_signing_secrets()
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_TTL_MINUTES", "60"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("JWT_REFRESH_TTL_DAYS", "30"))
DIAGNOSTIC_CODE_LENGTH = 8
INVITE_CODE_LENGTH = 8
BASE_DIAGNOSTIC_URL = diagnostic_base_url()  # SV-27: logs an error in production/staging when DIAGNOSTIC_BASE_URL is missing
# "cancelled" is deliberately absent: a cancelled invite never turns into "expired" when its time passes.
_EXPIRABLE_DIAGNOSTIC_STATUSES = ("pending", "confirmed", "in_progress")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# A real bcrypt hash (cost 12, same as pwd_context makes) of a random string nobody knows. login() verifies
# against it when the e-mail is unknown, so an unknown account costs the same time as a known one.
_DUMMY_PASSWORD_HASH = "$2b$12$wFh2ZCojJmLqU5ozLAz99OChtLuiQ.w.YYKc03f534RzPqJUpavxa"


# ============================================================================
# PASSWORD / TOKEN UTILS
# ============================================================================


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except ValueError:
        if "\x00" not in plain_password:
            raise
        # bcrypt refuses a NUL byte outright (and fast), and no stored hash can come from such a password:
        # a plain "wrong password". One dummy verification keeps the cost of the answer the same.
        pwd_context.verify("not the password", _DUMMY_PASSWORD_HASH)
        return False


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
    def __init__(self, db: Session, client: Optional[user_sessions.SessionClient] = None):
        self.db = db
        # Where the request came from (trusted address + user agent), recorded on the sessions this service creates.
        self.client = client

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

    def _issue_token_pair(self, user: User) -> TokenPair:
        """A fresh access + refresh JWT. Records nothing: the refresh token belongs to a session row, see below."""
        org_id, org_role, _ = self._get_user_org_info(user)
        access = create_access_token(str(user.id), user.role, org_id, org_role)
        refresh = create_refresh_token(str(user.id))
        return TokenPair(
            access_token=access,
            refresh_token=refresh,
            expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

    def _create_tokens(self, user: User) -> TokenPair:
        """A sign-in: a token pair and a NEW session of this user (one row per signed-in device); the user's other
        sessions are not touched (#101)."""
        tokens = self._issue_token_pair(user)
        user_sessions.issue(
            self.db,
            user.id,
            hash_token(tokens.refresh_token),
            timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
            self.client,
        )
        return tokens

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
                owner_name=f"{user.first_name} {user.last_name}".strip() or user.full_name,
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
        if org.blocked_at is not None:  # a centre blocked from the console is not activated by an old link
            raise ValueError("Доступ центра приостановлен. Свяжитесь с ALGO")

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
        if owner.platform_blocked_at is not None:
            raise ValueError("Пользователь заблокирован командой ALGO")
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
        # One answer, one cost: an unknown e-mail, a deactivated account and a wrong password all end in the same
        # InvalidCredentialsError after the same bcrypt verification (against a dummy hash when there is no
        # user), so neither the message nor the response time says whether the account exists.
        user = self.db.query(User).filter(User.email == request.email).first()
        password_ok = verify_password(request.password, user.password_hash if user else _DUMMY_PASSWORD_HASH)
        if not user or not user.is_active or not password_ok:
            raise InvalidCredentialsError()
        from .platform_blocks import ensure_login_allowed  # a console account / a blocked centre never gets a session
        ensure_login_allowed(self.db, user)

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

        # The session of THIS device (found by the hash of the token, rows locked); the user's other devices keep
        # their own sessions (#101, user_sessions.py). A replayed old token revokes its session, and that
        # revocation must be committed before the refusal, which rolls the request back.
        ttl = timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
        claim = user_sessions.claim(self.db, user.id, hash_token(refresh_token))
        if claim is None:
            self.db.commit()
            raise ValueError("Refresh token отозван")

        tokens = self._issue_token_pair(user)
        user_sessions.rotate(self.db, claim, hash_token(tokens.refresh_token), ttl, self.client)
        self.db.commit()
        return tokens

    def logout(self, user_id: str) -> None:
        """Sign-out from an ACCESS token alone (no refresh token at hand): revokes nothing.

        An access JWT names the user, not the device, and a user may be signed in on several devices; revoking
        "all of them" would sign out the other devices (the defect #101 fixed). The caller clears the browser's
        cookies, and the access token itself dies within its lifetime. A request that carries the refresh cookie
        is handled by ``logout_by_refresh_token``, which does revoke that device's session.
        """
        logger.debug("logout without a refresh token: user %s, no session identified, none revoked", user_id)

    def logout_by_refresh_token(self, refresh_token: str) -> bool:
        """Sign out the session that *refresh_token* belongs to.

        A refresh JWT that is merely correctly signed proves nothing about the live session: it must be a LIVE token
        of a session, or one superseded at most 60 s ago (the browser raced a refresh and holds the older cookie).
        An older superseded token must not sign the user out of the session that replaced it.
        Only that one session is revoked (reason ``logout``); the user's other devices stay signed in.
        Returns whether a session was revoked; a stale token only gets its cookies removed by the caller.
        """
        payload = verify_refresh_token(refresh_token)
        if not payload or not payload.get("sub"):
            return False
        if not user_sessions.revoke_by_token(
            self.db, payload["sub"], hash_token(refresh_token), user_sessions.REVOKE_LOGOUT
        ):
            return False
        self.db.commit()
        return True

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
        # Every device signs in again with the new password.
        user_sessions.revoke_all(self.db, user.id, user_sessions.REVOKE_PASSWORD_CHANGED)
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
        self, org_id: str, creator_id: str, request: InviteMemberRequest, *, creator_role: Optional[str] = None
    ) -> dict:
        """Создать приглашение (owner/admin приглашает teacher, admin приглашает только владелец) - ``invites.Invites``."""
        return invites.Invites(self).create(org_id, creator_id, request, creator_role=creator_role)

    def accept_invitation(self, request: AcceptInviteRequest) -> AuthResponse:
        """Принять приглашение - создать аккаунт (или вернуть удалённого сотрудника) и привязать к организации - ``invites.Invites``."""
        return invites.Invites(self).accept(request)

    def list_invitations(self, org_id: str) -> list:
        return invites.Invites(self).list(org_id)

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
        return invites.Invites(self).info(code)

    def list_org_members(self, org_id: str, status: str = "active") -> list:
        return staff_lifecycle.StaffLifecycle(self).list_members(org_id, status)

    def update_member_role(self, org_id: str, member_id: str, new_role: str, actor_id: Optional[str] = None) -> dict:
        return staff_lifecycle.StaffLifecycle(self).change_role(org_id, member_id, new_role, actor_id=actor_id)

    def remove_member(self, org_id: str, member_id: str, actor_id: Optional[str] = None) -> dict:
        """Удалить сотрудника: отказ, пока у него группы или идущий урок; будущие замены снимаются (``staff_lifecycle``)."""
        return staff_lifecycle.StaffLifecycle(self).remove(org_id, member_id, actor_id=actor_id)

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
        self, org_id: str, student_id: str, request: StudentUpdateRequest,
        *, actor_id: Optional[str] = None, actor_role: Optional[str] = None,
    ) -> StudentResponse:
        student = (
            self.db.query(Student)
            .filter(Student.id == student_id, Student.org_id == org_id)
            .with_for_update(key_share=True)  # FOR NO KEY UPDATE: waits for an archiving of this student
            .populate_existing()
            .first()
        )
        if not student:
            raise ValueError("Ученик не найден")
        guard_generic_status_update(student, request.status)

        from . import student_edit  # lazy
        update_data = student_edit.update_data(request)  # an omitted field stays; an explicit null / "" clears the phone and the note
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
        before = student_edit.snapshot(student)
        for key, value in update_data.items():
            if hasattr(student, key):
                setattr(student, key, value)
        student_edit.journal(self.db, student, before, actor_id, actor_role)  # name / class / phone / note changed = one journal row

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
        if student.status == "archived":
            # The archive is for the owner and the administrator; a stale membership changes nothing.
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
        return attach_archived_groups(
            self.db, org_id, student, self._build_student_response(student)
        )

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
                "cancelled_at": inv.cancelled_at.isoformat() if inv.cancelled_at else None,
                "cancel_reason": inv.cancel_reason,
                "revived_at": inv.revived_at.isoformat() if inv.revived_at else None,
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
        # Every stay in every group (a student who came back has several), one query for all the memberships.
        period_map = periods_by_membership(self.db, [m.id for m in memberships])
        group_history = []
        for m in memberships:
            group_history.append({
                "group_id": str(m.group_id),
                "group_name": m.group.name if m.group else "—",
                "class_level": m.group.class_level if m.group else None,
                "is_active": m.is_active,
                "joined_at": m.joined_at.isoformat() if m.joined_at else None,
                "left_at": m.left_at.isoformat() if m.left_at else None,
                "periods": periods_payload(period_map.get(m.id, [])),
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
                "periods": periods_payload(period_map.get(m.id, [])),
            }
            for m, g, t in group_rows
        ]
        groups_list.sort(key=lambda item: not item["is_primary"])  # stable: primary first

        return {
            "student": self._build_student_response(student).model_dump(),
            "archived_groups": archived_groups_payload(self.db, org_id, student),
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
        teacher_id: Optional[str] = None,
    ):
        """Filtered student query, or ``None`` when the actor can see nobody."""
        if status == "no_group":
            # O15: "no group" = status "active" (was enrolled) and no live membership
            status, has_group = "active", False
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
            # "My students" never include the archive, whatever a stale membership says.
            query = query.filter(Student.status != "archived")

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
                        student_not_archived(),
                    )
                )
            )
        if teacher_id:
            # IA fix pack: the students of the teacher's ACTIVE groups (distinct: an IN over memberships), this organisation only
            query = query.filter(
                Student.id.in_(
                    self.db.query(StudentGroup.student_id)
                    .join(Group, Group.id == StudentGroup.group_id)
                    .filter(
                        Group.org_id == org_id,
                        Group.teacher_id == teacher_id,
                        Group.status == "active",
                        StudentGroup.is_active.is_(True),
                        student_not_archived(),
                    )
                )
            )
        if has_primary_group is True:
            query = query.filter(Student.group_id.isnot(None))
        if has_group is not None:
            # "In a group" = an active membership in a live (not archived) group of this
            # organisation. A stale students.group_id or a membership in an archived group
            # does not count: such a student can be enrolled again. Correlated EXISTS, so
            # the scan never leaves the student's own row.
            in_a_group = (
                self.db.query(StudentGroup.id)
                .join(Group, Group.id == StudentGroup.group_id)
                .filter(
                    StudentGroup.student_id == Student.id,
                    StudentGroup.is_active.is_(True),
                    Group.org_id == org_id,
                    Group.status == "active",
                )
                .exists()
            )
            query = query.filter(in_a_group if has_group else ~in_a_group)
        if status:
            query = query.filter(Student.status == status)
        if exclude_status:
            query = query.filter(Student.status != exclude_status)
        if search:
            # Every word must match the first or last name, so a full-name
            # query such as "Иван Петров" finds the student too.
            for word in search.split():
                # "%" and "_" typed by the user are plain characters, not wildcards.
                escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                pattern = f"%{escaped}%"
                matches = (Student.first_name.ilike(pattern, escape="\\")) | (
                    Student.last_name.ilike(pattern, escape="\\")
                )
                if any(ch.isdigit() for ch in word):
                    # A word with a digit may be the public ID ("AL15-001"); names have
                    # none, so a plain word never matches every ID of the centre.
                    matches = matches | Student.display_id.ilike(pattern, escape="\\")
                query = query.filter(matches)
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
        teacher_id: Optional[str] = None,
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
            teacher_id=teacher_id,
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
        memberships: dict = {}
        if students:
            # One query for the whole page: which live groups each student is in, never money.
            for student_id, member_group_id in (
                self.db.query(StudentGroup.student_id, StudentGroup.group_id)
                .join(Group, Group.id == StudentGroup.group_id)
                .filter(
                    StudentGroup.student_id.in_([s.id for s in students]),
                    StudentGroup.is_active.is_(True),
                    student_not_archived(),
                    Group.org_id == org_id,
                    Group.status == "active",
                )
                .order_by(StudentGroup.joined_at, StudentGroup.id)
                .all()
            ):
                memberships.setdefault(student_id, []).append(member_group_id)
        items = []
        for s in students:
            groups_of = memberships.get(s.id, [])
            item = self._build_student_response(s, has_group=bool(groups_of))
            item.groups_count = len(groups_of)
            # The group the student "is in now": the primary one while it is a live membership,
            # else the earliest live membership (students.group_id may be empty or stale).
            primary_is_live = s.group_id is not None and s.group_id in groups_of
            item.current_group_id = (
                str(s.group_id) if primary_is_live else (str(groups_of[0]) if groups_of else None)
            )
            items.append(item)
        return items, total

    def _build_student_response(self, student: Student, *, has_group: Optional[bool] = None) -> StudentResponse:
        if has_group is None:
            has_group = student.id in student_views.students_with_live_group(self.db, student.org_id, [student.id])
        return StudentResponse(
            id=str(student.id),
            org_id=str(student.org_id),
            first_name=student.first_name,
            last_name=student.last_name,
            full_name=student.full_name.strip(),
            class_level=student.class_level,
            group_id=str(student.group_id) if student.group_id else None,
            student_number=student.student_number,
            display_id=student.display_id,
            sequence_number=student.sequence_number,
            student_phone=student.student_phone,
            comment=student.comment,
            status=student.status or "new",
            created_at=student.created_at.isoformat() if student.created_at else None,
            has_group=bool(has_group),
            status_view=ops_rules.student_status_view(student.status, bool(has_group)),
            **archive_stamp(student),
        )

    def create_diagnostic_invite(
        self, org_id: str, creator_id: str, request: DiagnosticCreateRequest
    ) -> DiagnosticInviteResponse:
        """Создать диагностику для существующего ученика"""
        student = (
            self.db.query(Student)
            .filter(Student.id == request.student_id, Student.org_id == org_id)
            .with_for_update(read=True)  # FOR SHARE: waits for an archiving (FOR NO KEY UPDATE) and re-reads the status
            .first()
        )
        if not student:
            raise ValueError("Ученик не найден")
        if student.status == ARCHIVED:
            self.db.rollback()
            raise ArchiveConflict(ARCHIVED_STUDENT_INVITE_MESSAGE)

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
            student_name=student.full_name.strip(),
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

    def _raise_if_invite_cancelled(self, code: str) -> None:
        """A cancelled invite is refused with its own text, like an expired one - not as «not found»."""
        cancelled = (
            self.db.query(DiagnosticInvite.id)
            .filter(DiagnosticInvite.code == code, DiagnosticInvite.status == "cancelled")
            .first()
        )
        if cancelled:
            raise ValueError(INVITE_CANCELLED_MESSAGE)

    @staticmethod
    def _verified_session_start(invite: DiagnosticInvite, raw_started_at: Any) -> datetime:
        """The moment the engine's session started (aware UTC), checked against the invite's own time bounds:
        after it was confirmed, not in the future, before it expired.  Anything else is not a start we can trust."""
        try:
            started_at = datetime.fromisoformat(str(raw_started_at or "").replace("Z", "+00:00"))
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
        return started_at

    def _revive_cancelled_invite(
        self, code: str, session_id: UUID, raw_started_at: Any, *, status: str
    ) -> Optional[DiagnosticInvite]:
        """«Начатую диагностику дать закончить»: a CANCELLED invite (the student was archived) takes the callbacks
        of a session that really started BEFORE the cancellation - the invite only said ``confirmed`` because the
        start callback came late.  All of these must hold, or ``None`` is returned and the cancellation stands:

        * ``cancel_reason == 'student_archived'``;
        * the session start passes ``_verified_session_start`` (after ``confirmed_at``, before ``expires_at``);
        * it is strictly BEFORE ``cancelled_at`` (a session that started at or after it never counts);
        * the invite is not bound to another session.

        The invite becomes ``status`` (``in_progress`` for the start callback, ``confirmed`` for the completion
        callback, which then completes it), ``cancelled_at`` / ``cancel_reason`` are cleared, ``revived_at`` says
        when (the trail that outlives the log line) and the change is logged.  The invite row is locked; the
        caller commits.
        """
        invite = (
            self.db.query(DiagnosticInvite)
            .filter(DiagnosticInvite.code == code, DiagnosticInvite.status == "cancelled")
            .with_for_update()
            .first()
        )
        if invite is None or invite.cancel_reason != INVITE_CANCEL_REASON or invite.cancelled_at is None:
            return None
        if invite.cat_session_id is not None and invite.cat_session_id != session_id:
            return None
        try:
            started_at = self._verified_session_start(invite, raw_started_at)
        except ValueError:
            return None
        cancelled_at = invite.cancelled_at
        if cancelled_at.tzinfo is None:
            cancelled_at = cancelled_at.replace(tzinfo=timezone.utc)
        if started_at >= cancelled_at:
            return None
        logger.info(
            "diagnostic_invite_transition invite_id=%s session_id=%s status=%s "
            "un_cancelled=session_predates_archive cancelled_at=%s session_started_at=%s",
            invite.id, session_id, status, cancelled_at.isoformat(), started_at.isoformat(),
        )
        invite.status = status
        invite.cancelled_at = None
        invite.cancel_reason = None
        invite.revived_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if status == "in_progress":
            invite.started_at = started_at
        return invite

    def get_diagnostic_by_code(self, code: str, *, include_cancel_details: bool = False) -> Optional[dict]:
        """Получить диагностику по коду (публичный, без JWT).

        ``include_cancel_details`` is for the internal caller only (a valid X-Internal-Service-Token): the public
        answer for a cancelled invite is ``status`` + ``message`` and nothing that says WHY (that the student was
        archived is not for whoever holds the link)."""
        invite = (
            self.db.query(DiagnosticInvite)
            .filter(DiagnosticInvite.code == code)
            .first()
        )
        if not invite:
            return None

        self._expire_diagnostic_invite_if_due(invite)

        student = self.db.query(Student).filter(Student.id == invite.student_id).first()

        info = {
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
        if invite.status == "cancelled":
            info["message"] = INVITE_CANCELLED_MESSAGE
            if include_cancel_details:
                # diagnostic-service lets a session that started BEFORE this moment finish (see _revive_cancelled_invite).
                info["cancelled_at"] = invite.cancelled_at.isoformat() if invite.cancelled_at else None
                info["cancel_reason"] = invite.cancel_reason
        return info

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
            .with_for_update()  # an archiving that cancels the invite meanwhile is not overwritten by this confirm
            .first()
        )
        if not invite:
            self._raise_if_invite_cancelled(code)
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

    def start_diagnostic(
        self, code: str, cat_session_id: Optional[str] = None, session_started_at: Optional[str] = None
    ) -> dict:
        """Atomically claim an invite for exactly one diagnostic session.

        Retrying the same callback is safe.  A different session may never
        replace an already bound ``cat_session_id`` — that would make one
        invite point at two attempts and corrupt the report/placement flow.

        ``session_started_at`` (when the engine created the session) lets a LATE callback reach an invite that
        was cancelled meanwhile: only when the session started before the cancellation (see
        ``_revive_cancelled_invite``).
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
            invite = self._revive_cancelled_invite(
                code, requested_session_id, session_started_at, status="in_progress"
            )
        if not invite:
            self._raise_if_invite_cancelled(code)
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

        # Lock order, the same everywhere: student -> membership -> group -> invite.  The archiving holds the student
        # and then updates the invite rows; this method may change the student's status below, so it takes the
        # student BEFORE the invite - the other order could deadlock with an archiving.  (start / confirm never touch
        # the student, create takes it FOR SHARE first.)
        owner_id = self.db.query(DiagnosticInvite.student_id).filter(DiagnosticInvite.code == code).scalar()
        if owner_id is not None:
            self.db.query(Student).filter(Student.id == owner_id).with_for_update(key_share=True).first()

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
            # A cancelled invite whose session started BEFORE the cancellation is completed like a confirmed one
            # whose start callback was late (the repair below binds the session and re-checks its start).
            invite = self._revive_cancelled_invite(
                code, completed_session_id, result_data.get("session_started_at"), status="confirmed"
            )
        if not invite:
            self._raise_if_invite_cancelled(code)
            raise ValueError("Диагностика не найдена или не начата")

        if invite.cat_session_id is None and invite.status in {"confirmed", "expired"}:
            # The engine can have durably started before expiry while the
            # start callback was unavailable. Only the authenticated internal
            # completion callback may repair that projection, using its
            # database creation time and the confirmed invite's time bounds.
            started_at = self._verified_session_start(invite, result_data.get("session_started_at"))
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

        live_group_students = student_views.students_with_live_group(
            self.db, org_id, [inv.student_id for inv in invites if inv.student_id]
        )
        result = []
        for inv in invites:
            student = inv.student
            group = student.primary_group if student else None
            group_id = str(student.group_id) if (student and student.group_id) else None
            group_name = group.name if group else None
            # O15: "assigned" = in a live group now (a stale primary pointer or the status "active" alone does not count)
            is_assigned = bool(student and student.id in live_group_students)

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
                    "student_name": student.full_name.strip()
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
                    "cancelled_at": inv.cancelled_at.isoformat()
                    if inv.cancelled_at
                    else None,
                    "cancel_reason": inv.cancel_reason,
                    "revived_at": inv.revived_at.isoformat()
                    if inv.revived_at
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
            # The kind of lessons decides the teacher's payroll percent: a teacher cannot choose it.
            if request.lesson_type != "group":
                raise PermissionError("Тип занятий задаёт администратор центра")
            teacher_uuid = actor_uuid

        group = Group(
            org_id=org_id,
            name=request.name,
            class_level=request.class_level,
            teacher_id=teacher_uuid,
            description=request.description,
            max_students=request.max_students,
            lesson_type=request.lesson_type,
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
                    "lesson_type": group.lesson_type,
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
                OrganizationMember.org_role.in_(lesson_teachers.TEACHING_ROLES),  # lead decision: anybody who can teach (teacher / owner / admin)
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
        changes = request.model_dump(exclude_none=True)
        if "status" in changes:
            # O4: archiving is an operation with preconditions (POST .../archive, .../restore), never a free string
            raise ops_rules.unprocessable("STATUS_USE_ARCHIVE_ACTION", ops_rules.ERROR_CATALOGUE["STATUS_USE_ARCHIVE_ACTION"][1])
        # The group is locked BEFORE it is read and compared: the diff and the audit are those of the row we hold (a concurrent change of
        # the teacher by another administrator is seen, not overwritten).
        group = (
            self.db.query(Group)
            .filter(Group.id == group_id, Group.org_id == org_id)
            .with_for_update()
            .populate_existing()
            .first()
        )
        if not group:
            raise ValueError("Группа не найдена")
        if group.status != "active":
            raise ops_rules.group_archived()  # O4: an archived group is read-only
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
                # O5: the new teacher must be free for the lessons that pass to him (56 days ahead; his own lessons and substitutions count)
                lesson_teachers.LessonTeachers(self).check_new_group_teacher(org_id, {group.id: (group, new_teacher_id)})

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

        actor_role = actor_role or self._org_role_of(org_id, actor_id)      # W1 S15: the role is the one of the live membership, never a default

        if "lesson_type" in diff:  # LOGIC-A B1: a new type is a history row effective today (+ audit), not a bare write
            from .lesson_types import LessonTypeService
            LessonTypeService(self.db).patch_type(org_id, group, diff["lesson_type"][0], diff["lesson_type"][1], actor_id, actor_role)

        if diff:
            self.db.add(
                GroupAudit(
                    org_id=org_id,
                    group_id=group.id,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    action="teacher_changed" if teacher_changed else "updated",
                    assigned_teacher_id=group.teacher_id,
                    snapshot=diff,
                    created_at=business_clock.utc_now(),
                )
            )
        if teacher_changed:
            lesson_teachers.LessonTeachers(self).release_equal_to_group_teacher(group, actor_id)
            lesson_substitutions.on_group_teacher_changed(self, group, actor_id, trigger="update_group")  # LOGIC-B2: requests of the old teacher are closed
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
            # LOGIC-B2: «Мои группы» - only the groups he teaches (a substitute has the lesson in the schedule, not the group)
            query = query.filter(Group.teacher_id == actor_id)
        elif teacher_id:
            query = query.filter(Group.teacher_id == teacher_id)
        if class_level:
            query = query.filter(Group.class_level == class_level)

        groups = query.order_by(Group.name).all()
        if not is_org_admin:
            return [self._with_substitution_flag(g, "own") for g in groups]
        return [self._build_group_response(g) for g in groups]

    def get_group_detail(
        self,
        org_id: str,
        group_id: str,
        *,
        actor_id: Optional[str] = None,
        is_org_admin: bool = False,
    ) -> dict:
        # LOGIC-B2: the group page (roster with phones and diagnostics) is for its teacher and the managers; a substitute / a former teacher read only the
        # lessons that are theirs (journal + start / marks), see ``attendance_rules.restrict_journal``
        group = self._require_group_access(
            org_id,
            group_id,
            actor_id=actor_id,
            is_org_admin=is_org_admin,
        )
        group_uuid = group.id

        memberships = (
            self.db.query(StudentGroup)
            .filter(
                StudentGroup.group_id == group_uuid,
                StudentGroup.is_active,
                student_not_archived(),
            )
            .all()
        )
        student_ids = [m.student_id for m in memberships]
        students_by_id = {
            s.id: s
            for s in self.db.query(Student).filter(Student.id.in_(student_ids)).all()
        } if student_ids else {}
        period_map = periods_by_membership(self.db, [m.id for m in memberships])
        latest_results = self._latest_diagnostic_results(student_ids)

        from .membership_pauses import paused_until_map  # LOGIC-A: who is on a pause today (the lesson panel hides them before the start)
        on_pause = paused_until_map(self.db, group_uuid, student_ids, ps.business_today())
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
                "periods": periods_payload(period_map.get(m.id, [])),
                "last_theta": theta,
                "last_level": lvl["recommended_level"],
                "last_level_status": lvl["level_status"],
                "last_class_equivalent": lvl["class_equivalent"],
                "last_level_provisional": lvl["level_provisional"],
                "last_diagnosed_at": r.diagnosed_at.isoformat() if r and r.diagnosed_at else None,
                "on_pause": str(s.id) in on_pause,
                "paused_until": on_pause[str(s.id)].isoformat() if str(s.id) in on_pause else None,
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
                .filter(
                    StudentGroup.group_id.in_(group_ids),
                    StudentGroup.is_active.is_(True),
                    student_not_archived(),
                )
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
            .filter(
                StudentGroup.group_id == group_uuid,
                StudentGroup.is_active,
                student_not_archived(),
            )
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
        as_of: Optional[datetime] = None,
    ) -> dict:
        """Проверить принадлежность ученика к группе по ID/ФИО с валидацией обоих полей при их наличии."""
        group = self.db.query(Group).filter(Group.id == group_id).first()
        if not group:
            return {
                "enrolled": False,
                "group_name": "",
                "message": "Группа не найдена",
            }

        # O17: ``as_of`` (naive UTC) asks "was he enrolled at that instant" (any membership period covers it) - the exam that is running
        # must stay open for a student who was moved or archived after it began.
        member_filter = StudentGroup.is_active if as_of is None else member_at(as_of)
        memberships = (
            self.db.query(StudentGroup)
            .filter(StudentGroup.group_id == group_id, member_filter)
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

        not_archived = (
            Student.status != "archived"
            if as_of is None
            else or_(Student.status != "archived", Student.archived_at > as_of)
        )
        students = (
            self.db.query(Student)
            .filter(Student.id.in_(list(student_ids)), not_archived)
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
        price_override: Optional[Any] = None,
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
                    mode=mode, price_override=price_override, actor_id=actor_id,
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
        price_override: Optional[Any] = None,
        actor_id: Optional[str] = None,
        payments_detail: bool = False,
        opening_balance: Optional[Any] = None,
        opening_source: str = "add",
    ) -> dict:
        """Enrol a student in a group.

        ``transfer`` (default): every other active membership is closed and the
        group becomes the primary one.  ``add``: other memberships stay, the
        primary group changes only if the student had none.

        The student follows the PRICE OF THE GROUP (ROADMAP #105); an individual price of another group is not
        carried.  ``price_override`` (``.amount`` + ``.reason``, both mandatory) is an explicit exception written for
        the membership from today; the caller must have checked the right to manage payments.  A membership that
        comes back to a group it had before keeps its earlier exceptions, like the rest of its history.
        Warnings (never refusals) are returned in ``warnings``.

        A ``transfer`` also carries the payments in force over to the new group - see
        ``PaymentService.carry_over_on_transfer``; ``payments_detail`` lets the note in ``warnings`` name
        dates / lessons (callers who may see payments).
        """
        if mode not in MEMBERSHIP_MODES:
            raise ValueError("Неизвестный способ добавления в группу")
        override_amount = override_reason = None
        if price_override is not None:
            from . import pricing  # lazy: pulls in the payments modules

            try:
                override_amount, override_reason = pricing.check_override_input(
                    price_override.amount, price_override.reason
                )
            except pricing.PriceError as exc:
                raise ValueError(exc.message) from exc
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
            raise ops_rules.GroupArchivedForEnrolment("Нельзя зачислить ученика в архивную группу")

        existing = (
            self.db.query(StudentGroup)
            .filter(
                StudentGroup.student_id == student_id,
                StudentGroup.group_id == group_id,
            )
            .with_for_update(key_share=True)  # FOR NO KEY UPDATE: an FK insert (FOR KEY SHARE) does not wait
            .first()
        )
        other_active_memberships = (
            self.db.query(StudentGroup)
            .filter(
                StudentGroup.student_id == student_id,
                StudentGroup.group_id != group_id,
                StudentGroup.is_active.is_(True),
            )
            .with_for_update(key_share=True)  # FOR NO KEY UPDATE: an FK insert (FOR KEY SHARE) does not wait
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
            if price_override is not None:
                # Nothing moves, but an authorised individual price is still the admin's intent.
                pricing.write_override(
                    self.db, existing.id, actor_id, amount=override_amount,
                    valid_from=ps.business_today(), reason=override_reason,
                )
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
        self.db.refresh(group)  # the lock is held now: an archiving that won the race is seen (O4.R4)
        if group.status != "active":
            raise ops_rules.GroupArchivedForEnrolment("Нельзя зачислить ученика в архивную группу")
        self._assert_no_open_attendance_for_groups(org_id, affected_group_ids)
        for om in closing:
            close_period(om, now_dt)
            on_membership_closed(self.db, om, now_dt, actor_id)  # LOGIC-A: F13 marker, pauses

        # Target membership is either a historical inactive record that will
        # be reactivated (its payment history stays attached), or a new one.
        if not already_member:
            active_count = (
                self.db.query(StudentGroup)
                .filter(
                    StudentGroup.group_id == group_id,
                    StudentGroup.is_active.is_(True),
                    student_not_archived(),
                )
                .count()
            )
            if group.max_students is not None and active_count >= group.max_students:
                raise ValueError("В группе достигнут лимит учеников")

        if existing:
            if not already_member:
                # Returning to a historical group starts a new membership
                # period (the earlier ones stay in ``student_group_periods``);
                # recorded attendance stays tied to past lessons.
                open_period(existing, now_dt)
                on_membership_returned(self.db, existing, now_dt, actor_id)  # LOGIC-A (F13): the price of the group again
            if closing:
                # A transfer into a group the student already belongs to keeps
                # its old joined_at (past rosters need it); this instant lets
                # the journal recognise the transfer.
                existing.transferred_at = now_dt
            membership = existing
        else:
            membership = StudentGroup(student_id=student_id, group_id=group_id)
            open_period(membership, now_dt)
            self.db.add(membership)

        if price_override is not None:
            self.db.flush()  # a new membership gets its id here
            pricing.write_override(
                self.db, membership.id, actor_id, amount=override_amount,
                valid_from=ps.business_date(now_dt), reason=override_reason,
            )

        if opening_balance is not None:  # LOGIC-A (decision 1): the opening balance of the new membership
            from .leave_service import apply_add_opening  # lazy

            self.db.flush()
            apply_add_opening(self.db, org_id, membership, actor_id, opening_balance, transfer=bool(closing), source=opening_source)

        # N13 P2.1: a transfer carries the payments in force over to the new group.  The price is not carried
        # (#105): in the new group the student follows the price of that group.
        carried: list[dict[str, str]] = []
        if closing:
            from .payments_service import PaymentService  # lazy: pulls in the status maths

            self.db.flush()
            carried = PaymentService(self.db).carry_over_on_transfer(
                org_id, actor_id, closing, membership,
                detailed=payments_detail, moment=now_dt,
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
            .with_for_update(key_share=True)  # FOR NO KEY UPDATE: an FK insert (FOR KEY SHARE) does not wait
            .first()
        )
        if not sg:
            raise ValueError("Ученик не в этой группе")
        self._lock_groups_for_membership_change(org_id, [group.id])
        self._assert_no_open_attendance_for_groups(org_id, [group.id])
        close_period(sg, datetime.now(timezone.utc))
        on_membership_closed(self.db, sg, datetime.now(timezone.utc), None)  # LOGIC-A: pauses (no author: no price marker)
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
            .filter(
                StudentGroup.group_id == group.id,
                StudentGroup.is_active,
                student_not_archived(),
            )
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
            lesson_type=group.lesson_type or "group",
            created_at=group.created_at.isoformat() if group.created_at else None,
            archivedAt=schedule_rules.archived_at(group),
            archiveReason=group.archive_reason,
        )

    def _with_substitution_flag(self, group: Group, reason: str) -> GroupResponse:
        """A teacher's "my groups" entry: ``substitution`` when the group is shown only because he substitutes in it."""
        response = self._build_group_response(group)
        response.substitution = reason == "substitution"
        return response

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
        """Краткая статистика организации для dashboard (total_students — без архивных)"""
        self._expire_due_diagnostic_invites(org_id)
        student_count = (
            self.db.query(func.count(Student.id))
            .filter(Student.org_id == org_id, Student.status != "archived")
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
            "no_group_students": student_views.count_no_group_students(self.db, org_id),
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
                if not schedule_rules.slot_has_time(payload):
                    continue
                lessons_out.append(
                    ScheduleLessonResponse(
                        id=str(lesson_id),
                        groupId=str(g.id),
                        groupName=g.name,
                        classLevel=g.class_level or 5,
                        validFrom=schedule_rules.slot_valid_from(g, s),
                        **payload,
                    )
                )

        return lessons_out

    def _attendance_teacher_snapshot(
        self,
        group: Group,
        lesson_id: Optional[str] = None,
        lesson_date: Optional[date] = None,
        occurrence: Optional[Dict[str, Any]] = None,
    ) -> Optional[UUID]:
        """Teacher to record on a new lesson row (LOGIC-B O5, SPEC 0.3a).

        The lesson-teacher resolver: the active substitution of the lesson, else the group's teacher at the planned END of the lesson
        (a handover takes what is still ahead; lessons that ended before it stay with the old teacher).  The row freezes him: it is the only
        source of "who taught" for payroll, and changes afterwards only through ``PUT /org/groups/{id}/lesson-teacher``.
        """
        if lesson_id is None or lesson_date is None:
            return group.teacher_id
        return lesson_teachers.lesson_teacher(self.db, group, lesson_id, lesson_date.isoformat(), occurrence)

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

    def _require_group_read_access(
        self,
        org_id: str,
        group_id: str,
        *,
        actor_id: Optional[str],
        is_org_admin: bool,
    ) -> Group:
        """Read access to a group's journal: its teacher, a teacher of it within the last 7 days, or a substitute (LOGIC-B O5).

        Writing the schedule keeps ``_require_group_access`` (the group's teacher or a manager); acting on one lesson is judged by
        ``attendance_rules.AttendanceRules.assert_lesson_rights``.
        """
        try:
            return self._require_group_access(org_id, group_id, actor_id=actor_id, is_org_admin=is_org_admin)
        except PermissionError:
            group = self.db.query(Group).filter(Group.id == group_id, Group.org_id == org_id).first()
            if group is not None and actor_id and lesson_teachers.LessonTeachers(self).can_read_group(group, actor_id):
                return group
            raise

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
            "startTime": slot.get("startTime") or slot.get("start_time"),
            "endTime": slot.get("endTime") or slot.get("end_time"),
            "subject": slot.get("subject") or "Математика",
            "room": ops_rules.normalize_room(slot.get("room")),
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
        sent = getattr(request, "model_fields_set", set())
        for field in fields:
            value = getattr(request, field, None)
            if value is not None:
                result[field] = ops_rules.normalize_room(value) if field == "room" else value
            elif field in ("room", "topic") and field in sent:
                result[field] = None            # W1 S25 (Л-4): an explicit ``null`` takes the room / the topic off (an absent field leaves it as it is)
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
            "student_name": student.full_name.strip(),
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
            "cancelled_at": invite.cancelled_at.isoformat() if invite.cancelled_at else None,
            "cancel_reason": invite.cancel_reason,
            "revived_at": invite.revived_at.isoformat() if invite.revived_at else None,
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
        # O1: no lesson before the slot's "valid from"; O4: none whose start lies in an inactive (archived) interval of the group
        intervals = schedule_rules.parse_intervals(group)
        slot_starts = {self._lesson_id_for_slot(group, slot): schedule_rules.slot_valid_from(group, slot) for slot in slots}
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
                if current_iso < slot_starts[lesson_id]:
                    continue
                base_payload = self._slot_payload(slot)
                configured_payload = dict(base_payload)
                for revision in recurring.get(lesson_id, []):
                    if revision.effective_date <= current_iso:
                        configured_payload = dict(revision.payload or configured_payload)

                if configured_payload["dayOfWeek"] != current_day.isoweekday():
                    continue
                if not schedule_rules.slot_has_time(configured_payload):
                    continue

                occurrence_override = occurrence_overrides.get((lesson_id, current_iso))
                payload = (
                    dict(occurrence_override.payload or configured_payload)
                    if occurrence_override
                    else configured_payload
                )
                if schedule_rules.lesson_is_inactive(intervals, current_iso, payload.get("startTime")):
                    continue
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
            if not schedule_rules.slot_has_time(payload) or schedule_rules.lesson_is_inactive(
                intervals, one_off.lesson_date, payload.get("startTime")
            ):
                continue
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
        """Reject a transfer/revision/new lesson that overlaps another lesson of the teacher who teaches THIS lesson (the group teacher
        on that date or a substitute); one check for every writer, ``TEACHER_BUSY`` 409 with ``conflicts`` (``schedule_rules``)."""
        schedule_rules.ScheduleRules(self).assert_teacher_free(
            group=group, lesson_id=lesson_id, occurrence_date=occurrence_date, payload=payload
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
            # O5: by the teacher of the LESSON - his groups, the groups he substitutes in, the groups he taught before a handover
            visible = lesson_teachers.LessonTeachers(self).groups_visible_to_teacher(
                org_id, teacher_id, window_from=date_from, window_to=date_to
            )
            query = query.filter(Group.id.in_(list(visible)))
        occurrences: List[ScheduleOccurrenceResponse] = [
            ScheduleOccurrenceResponse(**item)
            for item in lesson_teachers.LessonTeachers(self).annotated_occurrences(
                query.order_by(Group.name).all(), date_from, date_to, only_teacher=teacher_id, include_replaced=True
            )
        ]
        return sorted(occurrences, key=lambda item: (item.occurrenceDate, item.startTime, item.groupName))

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
        if group.status != "active":
            raise ops_rules.group_archived()
        today = datetime.now(BUSINESS_TIMEZONE).date()
        if req.effectiveFrom <= today:
            raise ValueError("Постоянное расписание можно менять только начиная с будущей даты")
        self._lock_group_row(group)         # W1 S6 (ДЫРА-12): two revisions / a revision and a stop of one group serialise

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
        changed_now = self._changed_schedule_fields(current_payload, updated_payload)
        # W1 S8 (ДЫРА-1): the same patch goes to every revision of the series that is already planned for a LATER date (a revision is a full snapshot: without this the changed
        # field silently falls back to the old value from that date on).  The answer lists them.
        later = self._patch_later_revisions(group, lesson_id, req, recurring_end)
        if not changed_now and not later:
            raise ops_rules.OpsError(400, "NO_CHANGES", "Укажите хотя бы одно изменение постоянного расписания")
        # only a change of the DAY takes lessons away from their dates (the exam bound to (lesson, date) would hang on a date that is no lesson); a change of the time keeps that identity
        # (R3 4) the check starts at the EARLIEST date where the day really changes: the date of the revision itself or the date of a later revision the patch moves to another day
        day_dates = ([req.effectiveFrom] if "dayOfWeek" in changed_now else []) + [date.fromisoformat(rev.effective_date) for rev, _, fields in later if "dayOfWeek" in fields]
        exams = (self._exams_of_changed_schedule(org_id, group, lesson_id, min(day_dates), release=bool(getattr(req, "releaseAffectedExams", False))) if day_dates else None)

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

        room_warnings = schedule_rules.ScheduleRules(self).warnings_for(
            group, lesson_id,
            [req.effectiveFrom + timedelta(days=offset) for offset in range(56)
             if (req.effectiveFrom + timedelta(days=offset)).isoweekday() == updated_payload["dayOfWeek"]],
            updated_payload,
        )
        room_warnings += self._check_later_revisions(group, lesson_id, later, room_warnings)       # (R3 3) the same checks for the later revisions the patch changes
        if changed_now:
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
        for revision, patched, _fields in later:
            before = dict(revision.payload or {})
            revision.payload = patched
            flag_modified(revision, "payload")
            revision.changed_by = actor_id
            self.db.add(GroupScheduleAudit(
                override_id=revision.id, org_id=group.org_id, group_id=group.id, lesson_id=lesson_id, scope="recurring", effective_date=revision.effective_date,
                actor_id=actor_id, before_payload=before, after_payload=patched, reason=f"{req.reason.strip()} (изменение с {req.effectiveFrom.isoformat()} применено и к этому изменению)"[:500],
            ))
        self.db.flush()
        from .lesson_substitutions import on_schedule_changed

        moved = on_schedule_changed(self, group, lesson_id, req.effectiveFrom, actor_id)       # W1 S8 (ДЫРА-3): substitutes / requests of lessons that are gone
        released = self._release_exams_of_changed_schedule(org_id, group, lesson_id, min(day_dates) if day_dates else req.effectiveFrom, exams, actor_id, req.reason.strip())
        self.db.commit()
        from .days_off import DaysOffService  # a revision that puts the series on a day off of the centre: those lessons are cancelled as the day off's own

        day_off_warnings = DaysOffService(self).apply_to_lesson(org_id, actor_id, group.id, lesson_id, req.effectiveFrom)
        closed_note = (
            [{"code": "SUBSTITUTIONS_CLOSED", "message": f"Замены и запросы на уроки, которых больше нет: {moved['closedRequests'] + moved['releasedSubstitutes']} — закрыты, запись в журнале «Замены»"}]
            if moved["closedRequests"] or moved["releasedSubstitutes"] else []
        )
        return ScheduleLessonRevisedB(
            id=lesson_id,
            groupId=str(group.id),
            groupName=group.name,
            classLevel=group.class_level or 5,
            validFrom=schedule_rules.slot_valid_from(group, self._find_schedule_slot(group, lesson_id)),
            warnings=[*room_warnings, *day_off_warnings, *closed_note],
            laterRevisions=[{"effectiveFrom": revision.effective_date, "fields": fields} for revision, _patched, fields in later],
            releasedExamIds=released,
            **updated_payload,
        )

    @staticmethod
    def _changed_schedule_fields(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
        return [key for key in ("dayOfWeek", "startTime", "endTime", "subject", "room", "format", "color", "topic") if after.get(key) != before.get(key)]

    def _org_role_of(self, org_id: str, user_id: Any) -> str:
        """The role of ``user_id`` in the organisation, from the membership row (``ValueError`` when there is none: no role is invented)."""
        role = self.db.query(OrganizationMember.org_role).filter(OrganizationMember.org_id == org_id, OrganizationMember.user_id == user_id).scalar()
        if role is None:
            raise ValueError("Не удалось определить роль сотрудника в центре")
        return role

    def _lock_group_row(self, group: Group) -> None:
        self.db.query(Group).filter(Group.id == group.id, Group.org_id == group.org_id).with_for_update().populate_existing().first()

    def _patch_later_revisions(self, group: Group, lesson_id: str, req: Any, recurring_end: Optional[GroupScheduleOverride]) -> List[tuple]:
        """``[(revision, patched payload, changed fields)]`` of the revisions planned for a date AFTER ``req.effectiveFrom`` that the patch changes (a revision at or after the date
        the series stops is dead and is left alone)."""
        query = self.db.query(GroupScheduleOverride).filter(
            GroupScheduleOverride.group_id == group.id, GroupScheduleOverride.lesson_id == lesson_id, GroupScheduleOverride.scope == "recurring",
            GroupScheduleOverride.effective_date > req.effectiveFrom.isoformat(),
        )
        if recurring_end is not None:
            query = query.filter(GroupScheduleOverride.effective_date < recurring_end.effective_date)
        out = []
        for revision in query.order_by(GroupScheduleOverride.effective_date, GroupScheduleOverride.created_at).with_for_update().all():
            patched = self._apply_schedule_patch(revision.payload or {}, req)
            self._validate_schedule_payload(patched)
            fields = self._changed_schedule_fields(revision.payload or {}, patched)
            if fields:
                out.append((revision, patched, fields))
        return out

    def _check_later_revisions(self, group: Group, lesson_id: str, later: List[tuple], known: List[Any]) -> List[Any]:
        """W1 R3 (3): every later revision the patch changes gets the teacher-time check (14 days from its OWN date, ``TEACHER_BUSY``) and the room warnings (56 days) of its new payload;
        returns the warnings that are not in ``known`` yet."""
        found: List[Any] = []
        for revision, patched, _fields in later:
            first = date.fromisoformat(revision.effective_date)
            days = [first + timedelta(days=offset) for offset in range(56) if (first + timedelta(days=offset)).isoweekday() == patched["dayOfWeek"]]
            for day in days:
                if (day - first).days < 14:
                    self._assert_teacher_time_is_available(group=group, lesson_id=lesson_id, occurrence_date=day, payload=patched)
            found += [w for w in schedule_rules.ScheduleRules(self).warnings_for(group, lesson_id, days, patched) if w not in known and w not in found]
        return found

    def _exams_of_changed_schedule(self, org_id: str, group: Group, lesson_id: str, effective_from: date, *, release: bool) -> Dict[str, Any]:
        """W1 S8 (ДЫРА-2): a change of the DAY of a weekly lesson looks at the exams of its future dates like a move of one lesson does (LESSONS 40): an exam that is
        launched or has attempts refuses it (409 ``LESSON_HAS_LIVE_EXAM``); a draft / lobby / printed one goes back to a draft - only when the author said so (409
        ``EXAMS_NEED_CONFIRMATION`` lists them, the answer is repeated with ``releaseAffectedExams``).  exam-service down: ``RuntimeError`` (the router answers 503)."""
        impact = self._fetch_exam_recurring_end_impact(org_id=org_id, group_id=str(group.id), lesson_id=lesson_id, effective_from=effective_from.isoformat())
        exams = list(impact.get("exams") or [])
        blocked = [item for item in exams if not item.get("can_release")]
        if blocked:
            names = ", ".join(f"«{item.get('title') or 'Экзамен'}»" for item in blocked[:2])
            raise ops_rules.conflict("LESSON_HAS_LIVE_EXAM", f"Нельзя изменить расписание: экзамен {names} уже запущен или имеет попытки учеников")
        if exams and not release:
            listed = [{"examId": str(item.get("exam_id") or ""), "title": str(item.get("title") or "Экзамен"), "lessonDate": str(item.get("lesson_date") or ""),
                       "status": str(item.get("status") or "draft")} for item in exams]
            raise ops_rules.conflict("EXAMS_NEED_CONFIRMATION", "К этим занятиям привязаны экзамены: они вернутся в черновик, привяжите их заново. Подтвердите изменение", exams=listed)
        return {"exams": exams}

    def _release_exams_of_changed_schedule(self, org_id: str, group: Group, lesson_id: str, effective_from: date, exams: Optional[Dict[str, Any]], actor_id: str,
                                           reason: str) -> List[str]:
        if not exams or not exams["exams"]:
            return []
        try:
            return self._release_exam_recurring_end(org_id=org_id, group_id=str(group.id), group_name=group.name, lesson_id=lesson_id, effective_from=effective_from.isoformat(),
                                                    actor_id=str(actor_id), reason=reason)
        except Exception:
            self.db.rollback()          # the schedule is written only when the exams are released (the remote release is atomic and checks the sessions again)
            raise

    def get_series_revisions(self, org_id: str, lesson_id: str, *, after: Optional[date], actor_id: Optional[str], is_org_admin: bool) -> Dict[str, Any]:
        """The revisions of a weekly lesson dated after ``after`` (default: today) with the fields each one changes, and the date the series stops (W1 F6)."""
        group = self._require_recurring_lesson(org_id, lesson_id, actor_id=actor_id, is_org_admin=is_org_admin)
        after = after or datetime.now(BUSINESS_TIMEZONE).date()
        rows = (self.db.query(GroupScheduleOverride).filter(GroupScheduleOverride.group_id == group.id, GroupScheduleOverride.lesson_id == lesson_id,
                                                           GroupScheduleOverride.scope == "recurring", GroupScheduleOverride.effective_date > after.isoformat())
                .order_by(GroupScheduleOverride.effective_date, GroupScheduleOverride.created_at).all())
        before = self._configured_schedule_payload_as_of(group, lesson_id, after)
        out = []
        for row in rows:
            payload = dict(row.payload or {})
            out.append({"effectiveFrom": row.effective_date, "reason": row.reason, "fields": self._changed_schedule_fields(before, payload)})
            before = payload
        end = self._recurring_end_override(group, lesson_id)
        return {"lessonId": lesson_id, "after": after.isoformat(), "revisions": out, "endsFrom": end.effective_date if end else None}

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
        slot = next(
            (s for s in (group.schedule or []) if isinstance(s, dict) and self._lesson_id_for_slot(group, s) == lesson_id), None
        )
        if slot is not None and effective_from.isoformat() < schedule_rules.slot_valid_from(group, slot):
            raise ops_rules.unprocessable(  # O1.R3: an end before the start of the slot
                "END_BEFORE_START", "Дата окончания не может быть раньше даты начала регулярного урока"
            )
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
        if group.status != "active":
            raise ops_rules.group_archived()
        self._lock_group_row(group)         # W1 S6 (ДЫРА-12): two stops of one series serialise (the second is «уже прекращены», never a 500)
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

        end_audit = GroupScheduleAudit(
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
        self.db.add(end_audit)
        self.db.flush()
        from .lesson_substitutions import on_schedule_changed

        # W1 S9 (ДЫРА-3, А-7): requests / substitutes of the lessons that stop are closed with a trace, the cancellations set in advance for those dates are removed (and recorded here)
        moved = on_schedule_changed(self, group, lesson_id, req.effectiveFrom, actor_id, ended=True)
        if moved["removedCancellations"] or moved["closedRequests"] or moved["releasedSubstitutes"]:
            end_audit.after_payload = {**end_payload, **moved}

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
            ),
            closedRequests=moved["closedRequests"],
            releasedSubstitutes=moved["releasedSubstitutes"],
            removedCancellations=len(moved["removedCancellations"]),
        )

    def create_lesson(
        self,
        org_id: str,
        req: CreateLessonRequest,
        *,
        actor_id: Optional[str] = None,
        is_org_admin: bool = False,
    ) -> ScheduleLessonResponse:
        """A new weekly slot with its "valid from" date (LOGIC-B O1 / O11): see ``schedule_rules.ScheduleRules.create_lesson``."""
        return schedule_rules.ScheduleRules(self).create_lesson(
            org_id, req, actor_id=actor_id, is_org_admin=is_org_admin
        )

    def create_one_off_lesson(
        self,
        org_id: str,
        req: CreateOneOffLessonRequest,
        *,
        actor_id: Optional[str] = None,
        is_org_admin: bool = False,
    ) -> ScheduleOccurrenceResponse:
        """Create a lesson for one date without altering a recurring rule (``schedule_rules``)."""
        return schedule_rules.ScheduleRules(self).create_one_off(
            org_id, req, actor_id=actor_id, is_org_admin=is_org_admin
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
                student_not_archived(),
                not_paused_now(),  # LOGIC-A: a student on a pause is not in the class
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
                # ANY stay of the student covers the instant, not only the last one.
                member_at(occurred_at),
                not_paused_at(occurred_at),  # LOGIC-A
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
        only_group_id: Optional[str] = None,
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
            # O5: the lesson's own teacher (frozen in its row; a legacy row without one follows the group's teacher)
            query = query.filter(func.coalesce(GroupAttendance.teacher_id, Group.teacher_id) == actor_id)
        if only_group_id:
            query = query.filter(GroupAttendance.group_id == only_group_id)
        open_rows = query.order_by(
            GroupAttendance.lesson_date.asc(),
            GroupAttendance.started_at.asc(),
            GroupAttendance.created_at.asc(),
        ).all()

        # The missed-lesson candidates are known up front, so the schedule
        # data of every group involved is loaded once (two queries) instead of
        # per group: the cost of this call must not grow with the group count.
        reconciliation_from = missed_lesson_window_start(today_date)
        # LOGIC-B (spec 5.2): an ARCHIVED group stays in the discovery - a lesson that began before the archive and was never started is still to be resolved
        # (the lessons whose start lies inside the archive interval are not occurrences at all)
        groups_query = self.db.query(Group).filter(
            Group.org_id == org_id,
            Group.status.in_(("active", "archived")),
        )
        if not is_org_admin:
            if not actor_id:
                raise PermissionError("Нет доступа к пропущенным урокам")
            # O5: the groups he teaches, taught within the window or substitutes in (the lesson-by-lesson filter is below)
            visible = lesson_teachers.LessonTeachers(self).groups_visible_to_teacher(
                org_id, actor_id, window_from=reconciliation_from
            )
            groups_query = groups_query.filter(Group.id.in_(list(visible)))
        if only_group_id:
            groups_query = groups_query.filter(Group.id == only_group_id)
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
        book = lesson_teachers.TeacherBook(self.db)
        book_group_ids = [*(row[1].id for row in open_rows), *group_ids]
        book.load_history(book_group_ids)
        book.load_assignments(book_group_ids, min(preload_from, today), today)
        # A never-started lesson of a group nobody belonged to at that moment
        # is not a lesson that was missed (the group had no students yet).
        # One query for every candidate group, not one per group.
        memberships_by_group: Dict[Any, list[tuple[Optional[datetime], Optional[datetime]]]] = {}
        if group_ids:
            window_start_utc = lesson_start_as_naive_utc(reconciliation_from, 0)
            # Every STAY counts (a student who came back has several), still one query for all the groups.
            memberships_by_group = group_period_spans(self.db, group_ids, not_before=window_start_utc)

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
            try:
                row_end = self._lesson_time_minutes(occurrence.get("endTime"), "endTime")
            except ValueError:
                row_end = None
            row_res = book.resolve(group, attendance.lesson_id, attendance.lesson_date, row_end)
            row_teacher = lesson_teachers.effective_teacher(attendance, row_res)

            actions.append(
                OpenAttendanceActionResponse(
                    groupId=str(group.id),
                    groupName=group.name,
                    classLevel=group.class_level or 5,
                    actionType="in_progress",
                    teacherId=str(row_teacher) if row_teacher else None,
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
                    version=attendance.version,
                    requiresAdmin=(not is_org_admin) and ops_rules.is_too_old_for_teacher(lesson_date, today_date),
                    ageDays=ops_rules.lesson_age_days(lesson_date, today_date),
                    isSubstitution=bool(row_teacher is not None and row_teacher != row_res.default_teacher_id),
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
                missed_res = book.resolve(group, occurrence["id"], occurrence_date, end_minutes)
                if not is_org_admin and str(missed_res.teacher_id or "") != str(actor_id):
                    continue  # O5: somebody else leads this lesson (the teacher of that date, or the substitute)
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
                        teacherId=str(missed_res.teacher_id) if missed_res.teacher_id else None,
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
                        version=None,
                        requiresAdmin=(not is_org_admin)
                        and ops_rules.is_too_old_for_teacher(date.fromisoformat(occurrence_date), today_date),
                        ageDays=ops_rules.lesson_age_days(date.fromisoformat(occurrence_date), today_date),
                        isSubstitution=missed_res.substitution,
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

        # O5: his own groups, the groups where he substitutes, and (for the lessons of the window) the groups he taught before a handover
        teachers_service = lesson_teachers.LessonTeachers(self)
        visible = teachers_service.groups_visible_to_teacher(
            org_id, actor_id, window_from=business_date - timedelta(days=30), window_to=business_date + timedelta(days=6)
        )
        schedule_groups = (
            self.db.query(Group)
            .filter(Group.org_id == org_id, Group.status == "active", Group.id.in_(list(visible)))
            .order_by(Group.name)
            .all()
        )
        groups = [g for g in schedule_groups if visible[g.id] == "own"]  # LOGIC-B2: a substituted lesson is in the schedule, the group is not "his"

        # The dashboard uses a short retrospective window to attach an
        # operational exam to its concrete lesson, and six days ahead for the
        # group-focus cards.  It is well within the public 63-day limit.
        window_start = business_date - timedelta(days=30)
        window_end = business_date + timedelta(days=6)
        schedule_window: list[ScheduleOccurrenceResponse] = [
            ScheduleOccurrenceResponse(**item)
            for item in teachers_service.annotated_occurrences(
                schedule_groups, window_start, window_end, only_teacher=actor_id, include_replaced=True
            )
        ]
        schedule_window.sort(key=lambda item: (item.occurrenceDate, item.startTime, item.groupName))
        # the SAME annotation as ``GET /org/schedule/occurrences`` (one function: the request marks, the outcome of a cancelled / moved original, ``movedFrom``, ``createdBy``)
        lesson_substitutions.annotate_occurrences(self, {"org_role": "teacher", "user_id": actor_id}, schedule_window)
        today_lessons = [
            occurrence
            for occurrence in schedule_window
            if occurrence.occurrenceDate == business_date.isoformat()
        ]

        group_ids = [group.id for group in schedule_groups]
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
        own_group_ids = [group.id for group in groups if visible[group.id] == "own"]
        if own_group_ids:
            distinct_student_count = (
                self.db.query(func.count(func.distinct(StudentGroup.student_id)))
                .filter(
                    StudentGroup.group_id.in_(own_group_ids),
                    StudentGroup.is_active.is_(True),
                    student_not_archived(),
                )
                .scalar()
            ) or 0

        return TeacherDashboardSummaryResponse(
            businessDate=business_date.isoformat(),
            groups=[self._with_substitution_flag(group, visible[group.id]) for group in groups],
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
        group = self._require_group_read_access(
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
        cancellation_kinds_map: Dict[str, str] = {}  # PAY-F
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
            if att.status == LessonAttendanceStatus.CANCELLED.value and att.cancellation_kind:
                cancellation_kinds_map[instance_key] = "day_off" if att.day_off_id else att.cancellation_kind
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
                student_not_archived(),
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
            if (event_at := self._membership_event_at(membership))
            and period_start <= ps.business_date(event_at) <= period_end
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
            period_map = periods_by_membership(self.db, [membership.id for membership in memberships])
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
                    "periods": periods_payload(period_map.get(membership.id, [])) if membership else [],
                    "transferred_to_group_name": destinations_by_student_id.get(student.id),
                })
            historical_students.sort(key=lambda item: (item.get("left_at") or "", item["full_name"]))

        excuses_map, corrections_map = AttendanceExcuseService(self.db).journal_payload(
            org_id, attendance_keys, include_review_note=is_org_manager, include_billing_mode=is_org_manager
        )

        versions, teachers_of_lessons = attendance_rules.AttendanceRules(self).journal_extras(
            group, att_records, sorted_lessons, period_start, period_end
        )
        return GroupAttendanceResponse(
            groupId=str(group.id),
            month=month,
            lessons=sorted_lessons,
            records=records_map,
            statuses=statuses_map,
            cancellationReasons=cancellation_reasons_map,
            cancellationKinds=cancellation_kinds_map,
            rosterStudentIds=roster_student_ids_map,
            historicalStudents=historical_students,
            membershipEvents=membership_events,
            diagnosticReportNotices=diagnostic_report_notices,
            excuses=excuses_map,
            corrections=corrections_map,
            versions=versions,
            lessonTeachers=teachers_of_lessons,
        )

    def reconcile_missed_group_attendance(
        self,
        org_id: str,
        group_id: str,
        req: StartAttendanceRequest,
        *,
        reconciled_by: Optional[str],
        is_org_admin: bool,
    ) -> int:
        """Open a historical journal without claiming that it was started now.

        This is a reconciliation decision, not a delayed "start lesson"
        operation: ``started_at`` stays NULL and the audit records who opened
        the historical journal and when.
        """
        if not reconciled_by:
            raise ValueError("Не удалось определить автора разбора урока")
        group = self._require_group_read_access(
            org_id, group_id, actor_id=reconciled_by, is_org_admin=is_org_admin
        )
        locked_group = (
            self.db.query(Group)
            .filter(Group.id == group.id, Group.org_id == org_id)
            .with_for_update()
            .populate_existing()
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
        rules = attendance_rules.AttendanceRules(self)
        rules.assert_lesson_rights(
            group, attendance, occurrence, req.lessonDate, req.lessonId, actor_id=reconciled_by, is_org_admin=is_org_admin
        )
        if attendance:
            if attendance.status == LessonAttendanceStatus.IN_PROGRESS.value:
                return attendance.version  # Idempotent double click: preserve the original audit row.
            if attendance.status == LessonAttendanceStatus.CANCELLED.value:
                raise ValueError("Пропущенный урок уже отменён")
            raise ValueError("Пропущенный урок уже завершён")
        rules.assert_unclosed_window(req.lessonDate)

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
            teacher_id=self._attendance_teacher_snapshot(group, req.lessonId, req.lessonDate, occurrence),
            version=1,
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
        return attendance.version

    def cancel_missed_group_attendance(
        self,
        org_id: str,
        group_id: str,
        req: ReconcileMissedLessonRequest,
        *,
        cancelled_by: Optional[str],
        is_org_admin: bool,
        is_org_manager: bool = False,
    ) -> Dict[str, Any]:
        """«Урока не было»: an explicit, reasoned cancellation of a past lesson that was never started - THE cancellation (``Substitutions.cancel_lesson``, W1 S2).

        A teacher may do it for a lesson of his own (always «по вине центра», ``teacher_cancel`` in the journal «Замены»); a lesson that has not passed yet is not «missed» - the
        teacher asks «Не смогу провести», the administrator cancels it from the lesson sheet.  Returns ``{"version", "warnings"}``."""
        manager = is_org_manager or is_org_admin
        cancellation_kind = req.cancellationKind or DEFAULT_CANCELLATION_KIND      # PAY-F: owner / admin choose, a teacher cancels «по вине центра»
        if cancellation_kind != DEFAULT_CANCELLATION_KIND and not is_org_manager:
            raise PermissionError("Выбирать «Праздник, выходной» может только владелец или администратор: преподаватель отменяет урок «по вине центра»")
        if not cancelled_by:
            raise ValueError("Не удалось определить автора отмены урока")
        group = self._require_group_read_access(
            org_id, group_id, actor_id=cancelled_by, is_org_admin=is_org_admin
        )
        locked_group = (
            self.db.query(Group)
            .filter(Group.id == group.id, Group.org_id == org_id)
            .with_for_update()
            .populate_existing()
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
        rules = attendance_rules.AttendanceRules(self)
        rules.assert_lesson_rights(
            group, attendance, occurrence, req.lessonDate, req.lessonId, actor_id=cancelled_by, is_org_admin=is_org_admin
        )
        if attendance:
            if attendance.status == LessonAttendanceStatus.CANCELLED.value:
                if (req.cancellationReason or None) == (attendance.cancellation_reason or None):
                    return {"version": attendance.version, "warnings": []}  # The same cancellation was already committed.
                # a different one: the ONE rule of every road answers (ALREADY_MOVED for a moved original, else ALREADY_CANCELLED) - W1 R3 9
            else:
                raise ValueError("Нельзя отменить урок, который уже начали или завершили")
        else:
            rules.assert_unclosed_window(req.lessonDate)

        from .lesson_substitutions import Substitutions

        out = Substitutions(self).cancel_lesson(
            org_id, cancelled_by, group_id, req.lessonId, req.lessonDate, reason=req.cancellationReason, kind=cancellation_kind, by_manager=manager,
            via="request" if manager else "teacher_cancel", archived_ok=True,       # the missed lessons of an archived group are resolved by this very road
        )
        self.db.commit()
        return {"version": out["version"], "warnings": out["warnings"]}

    def start_group_attendance(
        self,
        org_id: str,
        group_id: str,
        req: StartAttendanceRequest,
        *,
        started_by: Optional[str],
        is_org_admin: bool,
    ) -> int:
        """Create the only valid transition from scheduled to in_progress."""
        if not started_by:
            raise ValueError("Не удалось определить преподавателя, начавшего урок")
        group = self._require_group_read_access(
            org_id, group_id, actor_id=started_by, is_org_admin=is_org_admin
        )
        # The same group lock is acquired by a membership transfer. It makes
        # "start lesson" and "move student" mutually exclusive operations.
        locked_group = (
            self.db.query(Group)
            .filter(Group.id == group.id, Group.org_id == org_id)
            .with_for_update()
            .populate_existing()
            .first()
        )
        if not locked_group:
            raise ValueError("Группа не найдена")
        group = locked_group
        if group.status != "active":
            raise ops_rules.group_archived()  # O4: an archived group is read-only (the lock above made the status current)
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
        # O5: who leads THIS lesson (the substitute / the group's teacher at the planned end of the lesson); the actor must be him
        # (or a manager).  The lesson teacher is what the active-lesson checks below are about.
        resolved_teacher_id = attendance_rules.AttendanceRules(self).assert_lesson_rights(
            group, att, matching_slot, lesson_date, req.lessonId, actor_id=started_by, is_org_admin=is_org_admin, check_age=False
        )
        # Serialize starts for the teacher who leads this lesson.  It
        # prevents two browser tabs (or two groups) from creating overlapping
        # active lessons between the check below and the insert.
        lesson_teacher_id = resolved_teacher_id or started_by
        self.db.query(User.id).filter(User.id == lesson_teacher_id).with_for_update().first()
        if att:
            if att.status == LessonAttendanceStatus.CANCELLED.value:
                raise ValueError("Отменённый урок нельзя начать")
            if att.status == LessonAttendanceStatus.COMPLETED.value:
                raise ValueError("Завершённый урок нельзя начать повторно")
            # Idempotency protects a double click/retry; it does not alter the
            # original actual start timestamp or audit trail.
            return att.version

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
        if resolved_teacher_id:
            active_lesson_query = active_lesson_query.filter(
                GroupAttendance.teacher_id == resolved_teacher_id
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
        if resolved_teacher_id:
            subsequent_lesson_query = subsequent_lesson_query.filter(
                GroupAttendance.teacher_id == resolved_teacher_id
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
        if not roster_snapshot:  # O6: nobody to teach, nothing to start (also for a group that has just been emptied)
            raise ops_rules.conflict("NO_STUDENTS", ops_rules.ERROR_CATALOGUE["NO_STUDENTS"][1])
        att = GroupAttendance(
            org_id=org_id,
            group_id=group_id,
            version=1,
            teacher_id=resolved_teacher_id,
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
        return att.version

    def save_group_attendance(
        self,
        org_id: str,
        group_id: str,
        req: SaveAttendanceRequest,
        *,
        marked_by: Optional[str],
        is_org_admin: bool,
        is_org_manager: bool = False,
    ) -> Dict[str, Any]:
        """Сохранить полный проверенный снимок отметок одного экземпляра урока.

        Returns ``{"version": <row version>, "examSync": {...} | None, "warnings": [...]}`` (LOGIC-B O7 / O8).  A cancellation (``status`` ``cancelled``) is THE cancellation
        of ``Substitutions.cancel_lesson`` (W1 S2): its warnings (a day off, a released substitute) come back in ``warnings``."""
        if not marked_by:
            raise ValueError("Не удалось определить автора отметки")
        group = self._require_group_read_access(
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
        cancellation_kind = (req.cancellationKind or DEFAULT_CANCELLATION_KIND) if is_cancellation else None  # PAY-F
        manager = is_org_manager or is_org_admin
        if is_cancellation:
            # W1 S6: the lock of the group comes first (the order of the move and of every other writer of the lesson); the cancellation re-takes it
            self.db.query(Group).filter(Group.id == group.id, Group.org_id == org_id).with_for_update().populate_existing().first()
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

        if is_cancellation and not manager:
            # W1 S3: a teacher does not cancel a lesson himself - «Не смогу провести» (the administrator decides); a PAST lesson that never started is «Урока не было».  Two exceptions:
            # a lesson he started whose roster is empty (O6: it cannot be finished, the way out is its cancellation) and (S27) a FUTURE one-off lesson that HE added, that is no copy of a
            # move and has no marks (a lesson created by mistake must be removable) - both only «по вине центра»
            if not (self._empty_started_lesson(att) or self._own_future_one_off(group_id, target_lesson_id, lesson_date, marked_by, att)):
                raise ops_rules.forbidden("TEACHER_CANNOT_CANCEL", "Отменить урок может администратор. Нажмите «Не смогу провести»")
            if cancellation_kind != DEFAULT_CANCELLATION_KIND:
                raise PermissionError("Выбирать «Праздник, выходной» может только владелец или администратор: преподаватель отменяет урок «по вине центра»")

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

        # LOGIC-B: rights on THIS lesson (O5) and the 7-day window (decision 7); an archived group takes no new row (O4); the version of the
        # marks (O7): a stale ``expectedVersion`` is a conflict, an identical repeat answers 200 without a write.
        rules = attendance_rules.AttendanceRules(self)
        lesson_teacher_id = rules.assert_lesson_rights(
            group, att, matching_slot, lesson_date, target_lesson_id, actor_id=marked_by, is_org_admin=is_org_admin
        )
        if att is None and group.status != "active":
            raise ops_rules.group_archived()
        if att is None and is_cancellation:
            rules.assert_unclosed_window(lesson_date)  # a never-started lesson older than the "unclosed lessons" window is nobody's to cancel
        if rules.version_gate(att, req):
            return {"version": att.version, "examSync": None, "warnings": []}

        if not is_cancellation and not att:
            raise ValueError("Сначала начните урок, затем отмечайте посещаемость")
        if is_cancellation:
            if req.records:
                raise ValueError("У отменённого урока не может быть отметок посещаемости")
            if not req.cancellationReason:
                raise ValueError("Для отмены урока укажите причину")
            from .lesson_substitutions import Substitutions

            out = Substitutions(self).cancel_lesson(                     # W1 S2: the one road of every cancellation
                org_id, marked_by, group_id, target_lesson_id, lesson_date, reason=req.cancellationReason, kind=cancellation_kind, allow_started=True,
                by_manager=manager, via="request" if manager else "teacher_cancel",
            )
            self.db.commit()
            return {"version": out["version"], "examSync": None, "warnings": out["warnings"]}
        # An administrator may correct a completed lesson (a reason is required
        # below); anyone else only edits a lesson that is in progress.
        is_correction_save = bool(
            att
            and is_org_manager
            and att.status == LessonAttendanceStatus.COMPLETED.value
        )
        if att.status != LessonAttendanceStatus.IN_PROGRESS.value and not is_correction_save:
            raise ValueError("Отметки можно изменять только у начатого урока")

        if matching_slot and lesson_date == today and not is_correction_save:
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
            and att.status == LessonAttendanceStatus.IN_PROGRESS.value
            and att.roster_snapshot is None
        ):
            # Safe compatibility path for lessons started before the roster
            # snapshot migration. New lessons always receive this at start.
            att.roster_snapshot = self._active_attendance_roster(group.id)
            flag_modified(att, "roster_snapshot")
        roster_student_ids = set()
        if att:
            roster_student_ids = self._roster_ids_from_snapshot(att.roster_snapshot)
            if not roster_student_ids:
                # A completed legacy lesson cannot be rebuilt from the current
                # membership: the student may have been transferred since it
                # happened. Its saved record keys are the only durable roster.
                roster_student_ids = {str(student_id) for student_id in (att.records or {})}
            if (
                requested_status == LessonAttendanceStatus.COMPLETED.value
                and att.status != LessonAttendanceStatus.COMPLETED.value
                and not roster_student_ids
            ):
                # O6: a lesson with nobody in its roster is not "held" (it would be paid): the teacher cancels it instead.
                # the code is ``NO_STUDENTS`` (one code for both steps); ``alias`` keeps the old ``NO_STUDENTS_COMPLETE`` readable for one release
                raise ops_rules.conflict("NO_STUDENTS", ops_rules.ERROR_CATALOGUE["NO_STUDENTS_COMPLETE"][1], alias="NO_STUDENTS_COMPLETE")
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

        if req.cancellationReason:
            raise ValueError("Причина отмены передаётся только для отменённого урока")
        elif req.cancellationKind:
            raise ValueError("Вид отмены передаётся только для отменённого урока")
        if requested_status == LessonAttendanceStatus.COMPLETED.value:
            missing_student_ids = roster_student_ids - set(records)
            if missing_student_ids:
                raise ValueError("Перед завершением урока заполните отметки всех учеников из состава урока")

        # Reasons for new "excused" marks are validated before any write.
        excuse_service = AttendanceExcuseService(self.db)
        excuse_plan = excuse_service.plan_save(att, records, req.excuseReasons)

        # O8: ending a lesson while an exam of it is still live: warn first, the exam runs on its own timer (fail-open).
        completing = (
            requested_status == LessonAttendanceStatus.COMPLETED.value
            and att is not None
            and att.status == LessonAttendanceStatus.IN_PROGRESS.value
        )
        exam_warning: Optional[str] = None
        live_exams: List[Dict[str, Any]] = []
        if completing:
            exam_warning, live_exams = rules.live_exam_gate(
                org_id, group_id, target_lesson_id, lesson_date.isoformat(), bool(getattr(req, "acknowledgeLiveExam", False))
            )

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

        if not att:
            att = GroupAttendance(
                org_id=org_id,
                group_id=group_id,
                teacher_id=lesson_teacher_id,
                version=1,
                lesson_date=lesson_date.isoformat(),
                lesson_id=target_lesson_id,
                topic=req.topic,
                status=requested_status,
                records=records,
                marked_by=marked_by,
                started_at=None,
                completed_at=now if requested_status == LessonAttendanceStatus.COMPLETED.value else None,
            )
            self.db.add(att)
            action = "created"
        else:
            att.records = records
            flag_modified(att, "records")
            att.version = (att.version or 1) + 1
            att.status = requested_status
            if req.topic is not None:
                att.topic = req.topic
            if not is_correction_save:
                att.marked_by = marked_by
            if requested_status == LessonAttendanceStatus.COMPLETED.value:
                att.completed_at = att.completed_at or now
            else:
                att.completed_at = None
            action = "corrected" if is_completed_correction else "updated"

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
            )
        )
        self.db.flush()
        self.db.commit()
        exam_sync = (
            rules.after_completion(org_id, group_id, target_lesson_id, lesson_date.isoformat(), marked_by, exam_warning, live_exams)
            if completing
            else None
        )
        return {"version": att.version, "examSync": exam_sync, "warnings": []}

    def _own_future_one_off(self, group_id: str, lesson_id: str, lesson_date: date, actor_id: Optional[str], att: Optional[GroupAttendance]) -> bool:
        """W1 S27 (ОД-24): a one-off lesson that THIS teacher created, still ahead, with no journal row but its own cancellation (so no marks) and not the copy of a move (a move has its own road)."""
        if (att is not None and att.status != LessonAttendanceStatus.CANCELLED.value) or not str(lesson_id).startswith("one-off-") or not actor_id:      # a repeat of his own cancellation is answered, not refused
            return False
        one_off = self.db.query(GroupScheduleOneOffLesson).filter(GroupScheduleOneOffLesson.id == lesson_id, GroupScheduleOneOffLesson.group_id == group_id).first()
        if one_off is None or str(one_off.created_by) != str(actor_id) or one_off.lesson_date != lesson_date.isoformat():
            return False
        payload = one_off.payload or {}
        try:
            starts = business_clock.lesson_instant_utc(lesson_date, self._validate_lesson_time_range(payload.get("startTime"), payload.get("endTime"))[0])
        except ValueError:
            return False
        if starts <= business_clock.utc_now():
            return False
        return lesson_substitutions.moved_from_map(self.db, [one_off.group_id], [lesson_id]).get((str(one_off.group_id), lesson_id)) is None

    def _empty_started_lesson(self, att: Optional[GroupAttendance]) -> bool:
        """A started lesson with no marks whose roster is empty (O6): the teacher cannot finish it and may cancel it; the row he already cancelled this way is his too, so an exact repeat
        of his cancellation reaches the version gate and is answered 200 (W1 R3 8)."""
        return bool(
            att is not None
            and att.status in (LessonAttendanceStatus.IN_PROGRESS.value, LessonAttendanceStatus.CANCELLED.value)
            and not att.records
            and not self._roster_ids_from_snapshot(att.roster_snapshot)
        )

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
