"""Request / response models of LOGIC-B2 "Замены" (SPEC logicB2_spec.md section 4: the frozen contract).

A teacher asks "I cannot teach this lesson"; the owner / admin decides (substitute / reschedule / cancel / reject).  JSON is camelCase, people are
``«Фамилия И.»``, moments are UTC naive ISO, lesson dates are ``YYYY-MM-DD``.  Errors are ``{"detail": {"code", "message", ...}}`` (``ops_rules.OpsError``).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .ops_schemas import OpsErrorDetail, TeacherConflict

ReasonCode = Literal["sick", "personal", "other"]
RequestStatus = Literal["pending", "substituted", "rescheduled", "cancelled", "rejected", "withdrawn", "closed"]
DecisionKind = Literal["substitute", "reschedule", "cancel", "reject", "withdraw", "closed"]
ClosedReason = Literal[
    "LESSON_STARTED", "LESSON_CANCELLED", "LESSON_REMOVED", "GROUP_ARCHIVED", "GROUP_TEACHER_CHANGED", "TEACHER_REMOVED", "LESSON_OUTSIDE_WINDOW"
]

REASON_LABELS = {"sick": "Болезнь", "personal": "Личное", "other": "Другое"}
CLOSED_REASON_TEXT = {
    "LESSON_STARTED": "Урок начат — запрос закрыт",
    "LESSON_CANCELLED": "Урок отменён",
    "LESSON_REMOVED": "Урока больше нет в расписании",
    "GROUP_ARCHIVED": "Группа закрыта",
    "GROUP_TEACHER_CHANGED": "Преподаватель группы изменён",
    "TEACHER_REMOVED": "Преподаватель больше не работает в центре",
    "LESSON_OUTSIDE_WINDOW": "Срок разбора истёк",
}


# ------------------------------------------------------------------------------------------------ item (4.1)
class PersonRef(BaseModel):
    id: str
    name: Optional[str] = None


class SuggestedTeacher(BaseModel):
    id: str
    name: Optional[str] = None
    active: bool = True


class SubGroup(BaseModel):
    id: str
    name: str
    studentCount: int = 0


class SubLesson(BaseModel):
    lessonId: str
    date: str
    startTime: str
    endTime: str
    room: Optional[str] = None
    format: str = "offline"
    startsAtUtc: str
    exists: bool = True


class MovedTo(BaseModel):
    lessonId: Optional[str] = None
    date: str
    startTime: str
    endTime: str


class SubDecision(BaseModel):
    kind: DecisionKind
    by: Optional[PersonRef] = None
    at: str
    via: Optional[Literal["request", "lesson_teacher_put", "system", "withdraw"]] = None
    note: Optional[str] = None
    substitute: Optional[PersonRef] = None
    movedTo: Optional[MovedTo] = None
    closedReason: Optional[ClosedReason] = None
    closedReasonText: Optional[str] = None


class SubPrevious(BaseModel):
    id: str
    status: RequestStatus
    teacherName: Optional[str] = None
    substituteName: Optional[str] = None


class SubstitutionItem(BaseModel):
    id: str
    batchId: str
    status: RequestStatus
    origin: Literal["teacher", "system"] = "teacher"
    group: SubGroup
    lesson: SubLesson
    teacher: PersonRef
    reasonCode: ReasonCode
    reasonLabel: str
    comment: Optional[str] = None
    suggestedTeacher: Optional[SuggestedTeacher] = None
    createdAt: str
    minutesToStart: int
    urgent: bool = False
    overdue: bool = False
    decision: Optional[SubDecision] = None
    previous: Optional[SubPrevious] = None


# ---------------------------------------------------------------------------------------- teacher (4.2)
class MyLesson(BaseModel):
    groupId: str
    groupName: str
    lessonId: str
    lessonDate: str
    startTime: str
    endTime: str
    room: Optional[str] = None
    format: str = "offline"
    isSubstitution: bool = False
    status: Literal["scheduled", "requested", "started", "passed", "cancelled"]
    requestId: Optional[str] = None
    canRequest: bool
    blockReason: Optional[Literal["LESSON_ALREADY_STARTED", "LESSON_CANCELLED", "ALREADY_REQUESTED"]] = None


class MyLessonsOut(BaseModel):
    dateFrom: str
    dateTo: str
    lessons: List[MyLesson] = Field(default_factory=list)


class SubstitutionLessonRef(BaseModel):
    groupId: str
    lessonId: str = Field(..., min_length=1, max_length=100)
    lessonDate: date


class CreateSubstitutionRequest(BaseModel):
    lessons: List[SubstitutionLessonRef] = Field(..., min_length=1)
    reasonCode: ReasonCode
    comment: Optional[str] = Field(None, max_length=500)
    suggestedTeacherId: Optional[str] = None


class SkippedLesson(BaseModel):
    groupId: str
    lessonId: str
    lessonDate: str
    code: str
    message: str


class CreateSubstitutionOut(BaseModel):
    batchId: str
    created: List[SubstitutionItem] = Field(default_factory=list)
    alreadyRequested: List[SubstitutionItem] = Field(default_factory=list)
    skipped: List[SkippedLesson] = Field(default_factory=list)


class AssignedToMe(BaseModel):
    groupId: str
    groupName: str
    lessonId: str
    lessonDate: str
    startTime: str
    endTime: str
    replacing: Optional[PersonRef] = None
    assignedAt: Optional[str] = None


class MineOut(BaseModel):
    items: List[SubstitutionItem] = Field(default_factory=list)
    pendingTotal: int = 0
    assignedToMe: List[AssignedToMe] = Field(default_factory=list)


class ColleagueOut(BaseModel):
    """«Предложить коллегу»: a member of the centre who can teach (no e-mail, no phone)."""

    id: str
    name: str
    role: Literal["teacher", "owner", "admin"]


class WithdrawBatchRequest(BaseModel):
    ids: List[str] = Field(..., min_length=1, max_length=100)


class WithdrawSkipped(BaseModel):
    id: str
    code: str
    message: str


class WithdrawBatchOut(BaseModel):
    withdrawn: List[SubstitutionItem] = Field(default_factory=list)
    skipped: List[WithdrawSkipped] = Field(default_factory=list)


# -------------------------------------------------------------------------------- owner / admin (4.3)
class SubstitutionListOut(BaseModel):
    items: List[SubstitutionItem] = Field(default_factory=list)
    total: int = 0
    pendingTotal: int = 0
    urgentTotal: int = 0


class SubstitutionSummary(BaseModel):
    pendingTotal: int = 0
    urgentTotal: int = 0
    overdueTotal: int = 0


class SubstitutionEvent(BaseModel):
    action: str
    actor: Optional[PersonRef] = None
    at: str
    snapshot: Dict[str, Any] = Field(default_factory=dict)


class ExamBinding(BaseModel):
    state: Literal["ok", "none", "unavailable"] = "none"
    title: Optional[str] = None
    canCancel: bool = True


class SubstitutionDetailOut(BaseModel):
    item: SubstitutionItem
    chain: List[SubstitutionItem] = Field(default_factory=list)
    events: List[SubstitutionEvent] = Field(default_factory=list)
    examBinding: ExamBinding = Field(default_factory=ExamBinding)


class CandidateBusy(BaseModel):
    kind: Literal["lesson", "substitution"]
    groupName: str
    startTime: str
    endTime: str
    pendingRequest: bool = False


class SubstitutionCandidate(BaseModel):
    teacherId: str
    name: str
    orgRole: str
    suggested: bool = False
    isDefaultTeacher: bool = False
    busy: bool = False
    busyWith: Optional[CandidateBusy] = None
    rateMissing: Optional[bool] = None  # only the owner sees it


class CandidateLesson(BaseModel):
    groupId: str
    groupName: str
    lessonId: str
    date: str
    startTime: str
    endTime: str


class CandidatesOut(BaseModel):
    requestId: str
    lesson: CandidateLesson
    requesterId: str
    candidates: List[SubstitutionCandidate] = Field(default_factory=list)


class MoveWarning(BaseModel):
    code: str
    message: str


class MoveCheckOut(BaseModel):
    ok: bool
    teacherId: Optional[str] = None
    teacherName: Optional[str] = None
    teacherConflicts: List[TeacherConflict] = Field(default_factory=list)
    groupConflicts: List[TeacherConflict] = Field(default_factory=list)
    warnings: List[MoveWarning] = Field(default_factory=list)


class DecideRequest(BaseModel):
    """One body for the four decisions; the service checks the fields of the chosen decision (422 with a code)."""

    decision: Literal["substitute", "reschedule", "cancel", "reject"]
    teacherId: Optional[str] = None                       # substitute
    note: Optional[str] = Field(None, max_length=300)     # substitute: the comment of the administrator
    newDate: Optional[date] = None                        # reschedule
    startTime: Optional[str] = None
    endTime: Optional[str] = None
    room: Optional[str] = None
    format: Optional[Literal["offline", "online"]] = None
    reason: Optional[str] = Field(None, max_length=500)   # cancel / reschedule (3..500), reject (3..300)
    expectedStatus: Optional[Literal["pending"]] = None


class LessonMoveRequest(BaseModel):
    """#124: the administrator moves a future lesson straight from the lesson sheet (no request of a teacher needed).  Same fields and rules as the decision
    ``reschedule``; ``expectedStartTime`` / ``expectedEndTime`` are what the sheet showed (409 ``LESSON_CHANGED`` when the schedule moved meanwhile)."""

    lessonId: str = Field(..., min_length=1, max_length=100)
    lessonDate: date
    newDate: Optional[date] = None
    startTime: Optional[str] = None
    endTime: Optional[str] = None
    room: Optional[str] = None
    format: Optional[Literal["offline", "online"]] = None
    reason: Optional[str] = Field(None, max_length=500)
    expectedStartTime: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    expectedEndTime: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")


class NewLesson(BaseModel):
    id: str
    groupId: str
    date: str
    startTime: str
    endTime: str
    room: Optional[str] = None
    format: str = "offline"


class DecideResult(BaseModel):
    kind: Literal["substitute", "reschedule", "cancel", "reject"]
    teacherId: Optional[str] = None
    teacherName: Optional[str] = None
    isSubstitution: Optional[bool] = None
    attendanceUpdated: Optional[bool] = None
    payrollSheetsAffected: Optional[List[Dict[str, Any]]] = None
    cancelledVersion: Optional[int] = None
    newLesson: Optional[NewLesson] = None
    version: Optional[int] = None
    warnings: Optional[List[MoveWarning]] = None


class DecideOut(BaseModel):
    item: SubstitutionItem
    result: DecideResult


class DecideBulkRequest(BaseModel):
    ids: List[str] = Field(..., min_length=1, max_length=50)
    decision: Literal["substitute", "cancel", "reject"]
    teacherId: Optional[str] = None
    reason: Optional[str] = Field(None, max_length=500)
    note: Optional[str] = Field(None, max_length=300)


class BulkSkipped(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    code: str
    message: str
    conflicts: Optional[List[TeacherConflict]] = None
    total: Optional[int] = None


class DecideBulkOut(BaseModel):
    updated: int = 0
    items: List[SubstitutionItem] = Field(default_factory=list)
    skipped: List[BulkSkipped] = Field(default_factory=list)


class SubstitutionAlreadyDecidedDetail(OpsErrorDetail):
    item: Optional[SubstitutionItem] = None


# ---------------------------------------------------------------------------- additive fields (4.5): shapes
class SubstitutionRequestMark(BaseModel):
    """``substitutionRequest`` of an occurrence / one entry of the journal's ``substitutionRequests`` (manager and the requester only)."""

    id: str
    status: RequestStatus
    reasonCode: ReasonCode
    teacherName: Optional[str] = None


class SubstitutionOutcome(BaseModel):
    """``substitutionOutcome`` of a cancelled original: no reason (D-12)."""

    kind: Literal["rescheduled", "cancelled"]
    movedTo: Optional[MovedTo] = None


class StudentNotice(BaseModel):
    """``notices[]`` of the student card: what a student sees (no reasons, no comments - D-12)."""

    lessonId: str
    date: str
    groupId: str
    groupName: str
    kind: Literal["cancelled", "rescheduled"]
    movedTo: Optional[MovedTo] = None


class RequestClosed(BaseModel):
    id: str
    status: RequestStatus


class LessonRosterStudent(BaseModel):
    id: str
    first_name: str
    last_name: str
    full_name: str
    display_id: Optional[str] = None
    class_level: Optional[int] = None


# SPEC 4.5 + the lead's decision on the substitute's rights: the additive fields of the existing endpoints (documented in the contract as ``x-additive``)
ADDITIVE_FIELDS = [
    {"endpoint": "GET /org/schedule/occurrences", "field": "substitutionRequest", "schema": "SubstitutionRequestMark|null", "audience": "manager, requester"},
    {"endpoint": "GET /org/schedule/occurrences", "field": "substitutionOutcome", "schema": "SubstitutionOutcome|null", "audience": "everybody who sees the schedule"},
    {"endpoint": "GET /org/groups/{id}/attendance", "field": "lessons[].substitutionOutcome", "schema": "SubstitutionOutcome|null", "audience": "everybody who reads the journal (a cancelled / moved lesson; no reason)"},
    {"endpoint": "GET /org/groups/{id}/attendance", "field": "substitutionRequests", "schema": "{<date>_<lessonId>: SubstitutionRequestMark}", "audience": "manager, requester"},
    {"endpoint": "GET /org/groups/{id}/attendance", "field": "lessonRoster", "schema": "[LessonRosterStudent]", "audience": "a substitute: only the students of HIS lesson (the journal of a substitute holds only that lesson)"},
    {"endpoint": "GET /org/students/{id}/attendance", "field": "lessons[].teacherName / isSubstitution", "schema": "string|null / bool", "audience": "manager"},
    {"endpoint": "GET /org/students/{id}/attendance", "field": "notices", "schema": "[StudentNotice]", "audience": "manager"},
    {"endpoint": "GET /org/members/{id}/removal-impact", "field": "pendingSubstitutionRequests", "schema": "{count:int}", "audience": "manager"},
    {"endpoint": "DELETE /org/members/{id}", "field": "closedSubstitutionRequests", "schema": "int", "audience": "owner"},
    {"endpoint": "POST /org/members/{id}/handover", "field": "closedSubstitutionRequests", "schema": "int", "audience": "manager"},
    {"endpoint": "GET /org/groups/{id}/archive-impact", "field": "warnings[PENDING_SUBSTITUTIONS]", "schema": "{code,count,message}", "audience": "manager"},
    {"endpoint": "POST /org/groups/{id}/archive", "field": "closedSubstitutionRequests", "schema": "int", "audience": "manager"},
    {"endpoint": "PUT /org/groups/{id}/lesson-teacher", "field": "requestClosed", "schema": "RequestClosed|null", "audience": "manager"},
    {"endpoint": "GET /org/groups/{id}", "field": "(403 for a substitute)", "schema": "-", "audience": "the lead's decision: a substitute has no group page"},
    {"endpoint": "GET /org/groups/{id}/lesson-access", "field": "(new, internal for exam-service)", "schema": "LessonAccessOut", "audience": "exam-service D-1"},
]


class LessonAccessLesson(BaseModel):
    lessonId: str
    lessonDate: str


class LessonAccessOut(BaseModel):
    """What exam-service may ask a substitute about: the group (name, grade) and ONLY the lessons he substitutes in."""

    id: str
    org_id: str
    name: str
    class_level: Optional[int] = None
    teacher_id: Optional[str] = None
    teacher_name: Optional[str] = None
    status: str = "active"
    substitution: bool = True
    lessons: List[LessonAccessLesson] = Field(default_factory=list)
