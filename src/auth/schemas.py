"""
ALGO V1 - Pydantic Schemas для аутентификации
B2B модель: Organizations → Members → Students (no login)
"""

from datetime import date
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError
from . import password_rules
from .payments_schemas import OpeningIn

PASSWORD_NUL_MESSAGE = "Пароль содержит недопустимый символ"


def _password_without_nul(value: str) -> str:
    """K2-2 (F): the ONE rule for a password that is being SET (``password_rules``: 8 characters, 72 bytes, no NUL); a login attempt
    with a wrong one is merely a wrong password (AuthService.verify_password answers False)."""
    reason = password_rules.problem(value)
    if reason:
        raise PydanticCustomError("password_nul" if "\x00" in value else "password_rule", reason)
    return value



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
    password: str = Field(..., max_length=128)
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    language: str = Field(default="ru")

    @field_validator("password")
    @classmethod
    def validate_password(cls, v):
        return _password_without_nul(v)

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
    # K2-1 (D): «Запомнить меня» - the server keeps the mode on the session (14 days sliding; without it: until the browser closes, at most 12 h)
    remember_me: bool = False

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v):
        return v.lower().strip()


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(..., max_length=128)

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v):
        return _password_without_nul(v)


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
    password: str = Field(..., max_length=128)
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)

    @field_validator("password")
    @classmethod
    def validate_password(cls, v):
        return _password_without_nul(v)

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


class ArchivedGroupOut(BaseModel):
    """A group the student was removed from by the last archiving (the return window offers it again)."""

    group_id: str
    group_name: str
    group_status: str
    is_full: bool


class StudentResponse(BaseModel):
    id: str
    org_id: str
    first_name: str
    last_name: str
    full_name: str
    class_level: int
    group_id: Optional[str] = None
    student_number: Optional[str] = None
    display_id: Optional[str] = None  # публичный ID вида "AL15-001" (может быть null в буфере)
    sequence_number: Optional[int] = None  # порядковый номер в организации
    student_phone: Optional[str] = None
    comment: Optional[str] = None
    status: str
    created_at: Optional[str] = None
    # Number of active memberships in live (not archived) groups; filled in list responses only.
    groups_count: Optional[int] = None
    # The group the student is in now (primary while it is a live membership, else the earliest one);
    # filled in list responses only, together with groups_count.
    current_group_id: Optional[str] = None
    # Only for status == "archived" (null otherwise): the instant the student was archived (naive UTC
    # ISO; the migration stamped the students archived before the action existed with their ``updated_at``;
    # null only for a row archived by an old image after that) and the reason, if any.
    archived_at: Optional[str] = None
    archive_reason: Optional[str] = None
    # Only GET /org/students/{id} (and the profile) of an archived student: the groups closed by the
    # last archiving.
    archived_groups: Optional[List[ArchivedGroupOut]] = None


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


# N13 P3: kind of lessons - the teacher's payroll percent is set per kind.
LessonType = Literal["group", "mini_group", "pair", "individual"]
# PAY-F: why the centre cancelled a lesson: its own fault (may be deducted from the next payment) or a holiday / day off (never)
CancellationKind = Literal["centre", "holiday"]
DEFAULT_CANCELLATION_KIND: CancellationKind = "centre"  # the usual cancellation is the centre's own (a holiday is chosen on purpose)


class GroupCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    class_level: Optional[int] = Field(None, ge=1, le=11)
    teacher_id: Optional[str] = None
    description: Optional[str] = None
    max_students: int = Field(default=30, ge=1, le=100)
    lesson_type: LessonType = "group"

    @field_validator("lesson_type", mode="before")
    @classmethod
    def _default_lesson_type(cls, value):
        return "group" if value is None else value  # a form that leaves the field empty means the default


class GroupUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    class_level: Optional[int] = Field(None, ge=1, le=11)
    teacher_id: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    max_students: Optional[int] = Field(None, ge=1, le=100)
    lesson_type: Optional[LessonType] = None


class GroupResponse(BaseModel):
    id: str
    org_id: str
    name: str
    class_level: Optional[int] = None
    teacher_id: Optional[str] = None
    teacher_name: Optional[str] = None
    description: Optional[str] = None
    schedule: Optional[List[Dict[str, Any]]] = None
    status: str = "active"
    max_students: int = 30
    student_count: int = 0
    lesson_type: str = "group"
    created_at: Optional[str] = None


MAX_MONEY = 10_000_000_000


class PriceOverrideInput(BaseModel):
    """An individual price given together with the enrolment (ROADMAP #105): a sum and the reason, both mandatory."""

    amount: int = Field(..., ge=0, le=MAX_MONEY)
    reason: str = Field(..., max_length=200)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        value = (value or "").strip()
        if len(value) < 3:
            raise ValueError("Причина индивидуальной цены: от 3 до 200 символов")
        return value


RETIRED_FEE_MESSAGE = (
    "Поле monthly_fee больше не используется: ученик берёт цену группы. "
    "Индивидуальная цена — поле price_override {amount, reason}"
)


class AddStudentToGroupRequest(BaseModel):
    """Optional body of "add a student to a group".

    Without a body the student follows the price of the group.  ``price_override`` is an explicit individual
    price (sum + reason) and needs the right to manage payments.
    """

    price_override: Optional[PriceOverrideInput] = None
    monthly_fee: Optional[int] = None  # retired (#105): refused with a message, never silently dropped
    # LOGIC-A (decision 1): the opening balance of the new membership (needs access to money); see payments_schemas.OpeningIn
    opening_balance: Optional[OpeningIn] = None

    @field_validator("monthly_fee")
    @classmethod
    def _retired(cls, value: Optional[int]) -> Optional[int]:
        if value is not None:
            raise ValueError(RETIRED_FEE_MESSAGE)
        return value


class BulkAddStudentsRequest(BaseModel):
    student_ids: list[str] = Field(..., min_length=1)
    # One individual price for everybody, or none.  Needs the right to manage payments.
    price_override: Optional[PriceOverrideInput] = None
    monthly_fee: Optional[int] = None  # retired (#105), see AddStudentToGroupRequest

    @field_validator("monthly_fee")
    @classmethod
    def _retired(cls, value: Optional[int]) -> Optional[int]:
        if value is not None:
            raise ValueError(RETIRED_FEE_MESSAGE)
        return value


# ============================================================================
# SCHEDULE & ATTENDANCE SCHEMAS
# ============================================================================


class ScheduleLessonResponse(BaseModel):
    id: str
    groupId: str
    groupName: str
    classLevel: int = 5
    subject: str = "Математика"
    dayOfWeek: int  # 1 (Mon) .. 7 (Sun)
    startTime: str   # "14:00"
    endTime: str     # "15:30"
    room: str = "Кабинет 204"
    format: str = "offline"  # "offline" | "online"
    color: str = "blue"
    topic: Optional[str] = None


class CreateLessonRequest(BaseModel):
    groupId: str
    dayOfWeek: int = Field(..., ge=1, le=7)
    startTime: str
    endTime: str
    subject: Optional[str] = "Математика"
    room: Optional[str] = "Кабинет 204"
    format: Optional[str] = "offline"
    color: Optional[str] = "blue"
    topic: Optional[str] = None


class CreateOneOffLessonRequest(BaseModel):
    """Разовое занятие на конкретную календарную дату.

    Предмет намеренно не передаётся: на текущем этапе все группы платформы
    относятся к математике. Это исключает появление противоречивого предмета
    в одном расписании группы.
    """

    groupId: str
    lessonDate: date
    startTime: str
    endTime: str
    room: Optional[str] = "Кабинет 204"
    format: Optional[str] = "offline"
    color: Optional[str] = "blue"
    topic: Optional[str] = None


class UpdateLessonRequest(BaseModel):
    groupId: Optional[str] = None
    dayOfWeek: Optional[int] = Field(None, ge=1, le=7)
    startTime: Optional[str] = None
    endTime: Optional[str] = None
    subject: Optional[str] = None
    room: Optional[str] = None
    format: Optional[str] = None
    color: Optional[str] = None
    topic: Optional[str] = None


class ScheduleOccurrenceResponse(ScheduleLessonResponse):
    """Конкретное занятие на дату, уже с учётом всех переносов."""

    occurrenceDate: str
    isRescheduled: bool = False
    originalStartTime: Optional[str] = None
    originalEndTime: Optional[str] = None
    originalRoom: Optional[str] = None
    rescheduleReason: Optional[str] = None
    isOneOff: bool = False


class ReviseRecurringLessonRequest(BaseModel):
    """Изменение постоянного расписания, действующее с будущей даты."""

    effectiveFrom: date
    dayOfWeek: Optional[int] = Field(None, ge=1, le=7)
    startTime: Optional[str] = None
    endTime: Optional[str] = None
    subject: Optional[str] = Field(None, max_length=200)
    room: Optional[str] = Field(None, max_length=200)
    format: Optional[str] = None
    color: Optional[str] = None
    topic: Optional[str] = Field(None, max_length=300)
    reason: str = Field(..., min_length=3, max_length=500)


class EndRecurringLessonRequest(BaseModel):
    """Stop a recurring lesson from a future date without deleting history."""

    effectiveFrom: date
    reason: str = Field(..., min_length=3, max_length=500)
    # A teacher must explicitly acknowledge that every untouched future
    # delivery reported by preflight will be returned to a safe draft state.
    releaseAffectedExams: bool = False


class RecurringLessonEndExamImpact(BaseModel):
    examId: str
    title: str
    lessonDate: str
    deliveryMode: str
    status: str
    canRelease: bool
    blockerReason: Optional[str] = None


class RecurringLessonEndImpactResponse(BaseModel):
    groupId: str
    lessonId: str
    effectiveFrom: date
    affectedExamCount: int = 0
    releasableExamCount: int = 0
    blockedExamCount: int = 0
    canEnd: bool
    exams: List[RecurringLessonEndExamImpact] = Field(default_factory=list)


class EndRecurringLessonResponse(RecurringLessonEndImpactResponse):
    ended: bool = True
    releasedExamIds: List[str] = Field(default_factory=list)


class LessonAttendanceStatus(str, Enum):
    """Допустимые состояния экземпляра урока."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class StudentAttendanceStatus(str, Enum):
    """Допустимые отметки ученика в журнале."""

    PRESENT = "present"
    ABSENT = "absent"
    EXCUSED = "excused"
    LATE = "late"


class StartAttendanceRequest(BaseModel):
    """Explicit start of one scheduled lesson occurrence."""

    lessonDate: date
    lessonId: str = Field(..., min_length=1, max_length=100)
    topic: Optional[str] = Field(None, max_length=300)


class SaveAttendanceRequest(BaseModel):
    """Полный снимок отметок одного занятия.

    `records` намеренно заменяет снимок на сервере, а не дополняет его: так
    сброс отметки действительно удаляет ключ и не появляется после reload.
    """

    lessonDate: date
    lessonId: str = Field(..., min_length=1, max_length=100)
    topic: Optional[str] = Field(None, max_length=300)
    status: LessonAttendanceStatus = LessonAttendanceStatus.IN_PROGRESS
    records: Dict[UUID, StudentAttendanceStatus] = Field(default_factory=dict)
    correctionReason: Optional[str] = Field(None, min_length=3, max_length=500)
    cancellationReason: Optional[str] = Field(None, min_length=3, max_length=500)
    cancellationKind: Optional[CancellationKind] = None  # PAY-F: only with a cancellation; absent = "centre"
    # Reason for every student newly marked "excused" (3..300 characters). May
    # also be sent for an already excused student to change the reason.
    excuseReasons: Optional[Dict[UUID, str]] = None


class GroupAttendanceResponse(BaseModel):
    groupId: str
    month: Optional[str] = None
    lessons: List[Dict[str, Any]] = Field(default_factory=list)
    records: Dict[str, Dict[str, str]] = Field(default_factory=dict)
    statuses: Dict[str, str] = Field(default_factory=dict)  # { instance_key: status }
    cancellationReasons: Dict[str, str] = Field(default_factory=dict)
    # PAY-F: instance_key -> `centre` | `holiday` | `day_off` (cancelled by a day off of the centre); a cancellation with no kind (old / a move) is absent
    cancellationKinds: Dict[str, str] = Field(default_factory=dict)
    # The students who belonged to each actual lesson, frozen when the lesson
    # started. This keeps a transfer from altering the historical journal.
    rosterStudentIds: Dict[str, List[str]] = Field(default_factory=dict)
    # Students who were transferred out but have real attendance in the
    # selected period. They are rendered separately from the active roster.
    historicalStudents: List[Dict[str, Any]] = Field(default_factory=list)
    # Only real transfers are returned here. A simple addition to a group is
    # not a visual event in the attendance journal.
    membershipEvents: List[Dict[str, Any]] = Field(default_factory=list)
    # A student newly enrolled after an initial diagnostic stays flagged only
    # until the teacher of the current group has opened that report.
    diagnosticReportNotices: List[Dict[str, Any]] = Field(default_factory=list)
    # Excuse reasons and their review state: { lessonKey: { studentId: {...} } }.
    # ``review_note`` and the decision mark (``decided_after_deadline``, ``deadline_at_decision``) are present for
    # administrators only.
    excuses: Dict[str, Dict[str, Dict[str, Any]]] = Field(default_factory=dict)
    # Latest correction of a completed lesson: { lessonKey: {by_name, at, reason} }.
    corrections: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class OpenAttendanceActionResponse(BaseModel):
    """A lesson instance that needs the teacher's explicit decision.

    ``in_progress`` is a real started lesson with an open journal. ``missed``
    is a past schedule occurrence that was never started and therefore must be
    reconciled explicitly, never silently treated as an absence.
    """

    actionType: Literal["in_progress", "missed"] = "in_progress"

    groupId: str
    groupName: str
    classLevel: int
    lessonId: str
    lessonDate: str
    subject: str
    startTime: str
    endTime: str
    room: Optional[str] = None
    format: str = "offline"
    topic: Optional[str] = None
    markedCount: int = 0
    rosterCount: int = 0
    remainingCount: int = 0
    # Who taught / is to teach the lesson (lesson snapshot, else group teacher).
    teacherId: Optional[str] = None
    teacherName: Optional[str] = None


class TeacherDashboardLessonAttendanceStateResponse(BaseModel):
    """Attendance state for one concrete lesson in the teacher dashboard.

    ``status`` is intentionally nullable: a planned lesson has no attendance
    row yet.  This lets the client distinguish it from an unavailable request
    instead of guessing that the lesson was never started.
    """

    lessonId: str
    groupId: str
    status: Optional[str] = None
    hasSavedRecords: bool = False


class TeacherDashboardSummaryResponse(BaseModel):
    """Single authoritative snapshot for the teacher's operational dashboard."""

    businessDate: str
    groups: List[GroupResponse] = Field(default_factory=list)
    # Distinct active students across the teacher's active groups (a student
    # of two groups counts once); groups[].student_count stays per group.
    studentCount: int = 0
    todayLessons: List[ScheduleOccurrenceResponse] = Field(default_factory=list)
    scheduleWindow: List[ScheduleOccurrenceResponse] = Field(default_factory=list)
    openAttendanceActions: List[OpenAttendanceActionResponse] = Field(default_factory=list)
    attendanceStates: List[TeacherDashboardLessonAttendanceStateResponse] = Field(default_factory=list)


class ReconcileMissedLessonRequest(BaseModel):
    """Explicit teacher decision for a past lesson that was never started."""

    lessonDate: date
    lessonId: str = Field(..., min_length=1, max_length=100)
    cancellationReason: str = Field(..., min_length=3, max_length=500)
    cancellationKind: Optional[CancellationKind] = None  # PAY-F: absent = "centre"


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
    language: Optional[str] = "ru"
    is_active: bool = True
    is_verified: bool = False
    must_change_password: bool = False
    created_at: Optional[str] = None
    # May this user see money (owner; an administrator only if the owner allowed it)?  Filled by GET/PUT /auth/me; absent elsewhere.
    payments_access: Optional[bool] = None


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
    """INTERNAL: what a sign-in or a refresh hands to the route, which puts the two tokens into the httpOnly cookies and never into the body
    (K2-1, A2: no ``*token*`` field is ever in a response). ``remember`` is the mode of the session (how long the cookies live)."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 3600
    remember: bool = False


class AuthResponse(BaseModel):
    """INTERNAL result of a sign-in (service -> route). The routes answer with ``SessionResponse`` (``user`` + ``org``): the tokens go to the cookies."""

    user: UserResponse
    tokens: Optional[TokenPair] = None
    org: Optional[OrgResponse] = None


class SessionResponse(BaseModel):
    """The body of every sign-in / refresh answer: who signed in. No token of any kind (K2-1, A2)."""

    user: UserResponse
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
