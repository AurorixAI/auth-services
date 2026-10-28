"""Request / response models of LOGIC-B2 "Замены" (SPEC logicB2_spec.md section 4: the frozen contract).

A teacher asks "I cannot teach this lesson"; the owner / admin decides (substitute / reschedule / cancel / reject).  JSON is camelCase, people are
``«Фамилия И.»``, moments are UTC naive ISO, lesson dates are ``YYYY-MM-DD``.  Errors are ``{"detail": {"code", "message", ...}}`` (``ops_rules.OpsError``).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from .ops_schemas import CancelWarning, OpsErrorDetail, TeacherConflict
from .schemas import CancellationKind

ReasonCode = Literal["sick", "personal", "other"]
RequestStatus = Literal["pending", "substituted", "rescheduled", "cancelled", "rejected", "withdrawn", "closed"]
DecisionKind = Literal["substitute", "reschedule", "cancel", "reject", "withdraw", "closed"]
ClosedReason = Literal[
    "LESSON_STARTED", "LESSON_CANCELLED", "LESSON_REMOVED", "GROUP_ARCHIVED", "GROUP_TEACHER_CHANGED", "TEACHER_REMOVED", "LESSON_OUTSIDE_WINDOW",
    "ASSIGNMENT_CLEARED", "LESSON_RESTORED", "KIND_CHANGED", "ROLE_CHANGED", "ONE_OFF_ADDED",
]

REASON_LABELS = {"sick": "Болезнь", "personal": "Личное", "other": "Другое"}
CLOSED_REASON_TEXT = {
    "LESSON_STARTED": "Урок начат — запрос закрыт",
    "LESSON_CANCELLED": "Урок отменён",
    "LESSON_REMOVED": "Урока больше нет в расписании",
    "GROUP_ARCHIVED": "Группа закрыта",
    "GROUP_TEACHER_CHANGED": "Преподаватель группы изменён",
    "TEACHER_REMOVED": "Преподаватель больше не работает в центре",
    "LESSON_OUTSIDE_WINDOW": "Не решено вовремя",
    "ASSIGNMENT_CLEARED": "Замена снята",
    "LESSON_RESTORED": "Урок возвращён",
    "KIND_CHANGED": "Вид отмены изменён",
    "ROLE_CHANGED": "Сотрудник стал администратором",
    "ONE_OFF_ADDED": "Разовый урок добавил преподаватель",
}


# ------------------------------------------------------------------------------------------------ item (4.1)
class PersonRef(BaseModel):
    id: Optional[str] = None  # null: a teacher reads an administrator by the role only (#157 A1)
    name: Optional[str] = None
    role: Optional[str] = None  # the person's org role in this centre (owner | admin | teacher): the journal names the owner as the owner


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
    via: Optional[Literal["request", "teacher_move", "lesson_teacher_put", "system", "withdraw", "teacher_cancel", "manager_restore", "reason_change"]] = None
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
    trigger: Optional[str] = None  # P1.2: how a record the system made came to be: manager_assign | manager_move | teacher_move (null for a request of a teacher / a reopened one)
    clearedAt: Optional[str] = None  # P1.2: the substitution of the record stopped later (taken off, replaced, released, the lesson cancelled / moved); the status does not change
    clearedBy: Optional[PersonRef] = None
    clearedTo: Optional[PersonRef] = None  # the teacher who took the lesson over; null = the main teacher leads again
    clearedTrigger: Optional[str] = None  # put | member_removed | role_changed | group_teacher_changed | cancel | move | release
    outcome: str = "pending"  # #157 A11: THE code of what became of the record (P157_API.md §2) - one function for the list, the detail and ``mine``; the texts are the front's
    lastActivityAt: Optional[str] = None  # #157 A8: the later of the decision and the newest event of the record; the «Решённые» are sorted by it
    needsDecision: bool = False  # #157: ``needsDecision`` of the newest ``day_off_voided`` of the record (an administrator has to decide something after a day off was cancelled); false when there is none
    closedTrigger: Optional[str] = None  # #157 A9: the trigger of the ``closed`` event (day_off, journal_row, window_expired, role_changed, ...); null for a record that is not closed


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


class ClearedForMe(BaseModel):
    """W1 S22: a lesson where I was the substitute and the assignment was taken off (last 14 days)."""

    groupId: str
    groupName: str
    lessonId: str
    lessonDate: str
    startTime: Optional[str] = None
    endTime: Optional[str] = None
    clearedAt: str
    clearedBy: Optional[PersonRef] = None


class MineOut(BaseModel):
    items: List[SubstitutionItem] = Field(default_factory=list)
    pendingTotal: int = 0
    assignedToMe: List[AssignedToMe] = Field(default_factory=list)
    clearedForMe: List[ClearedForMe] = Field(default_factory=list)


class ColleagueOut(BaseModel):
    """«Предложить коллегу»: a teacher of the centre (no e-mail, no phone)."""

    id: str
    name: str
    role: Literal["teacher"]


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
    expiringTotal: int = 0


class SubstitutionSummary(BaseModel):
    pendingTotal: int = 0
    urgentTotal: int = 0
    overdueTotal: int = 0
    expiringTotal: int = 0     # W1 S20: requests that fall out of the window of the unclosed lessons on the 1st (shown only in the last 3 days before it)
    oneOffDecisionsTotal: int = 0     # #154: held additional lessons / empty make-ups that wait for «Оплатить / Не оплачивать»
    waitingTotal: int = 0             # #154: ``pendingTotal`` + ``oneOffDecisionsTotal`` - the counter of the tab «Ждут решения» and of the badge of the menu


class EventActor(BaseModel):
    """#157 A1: an owner / admin reads who did it (``id``, ``name``, ``role``)."""

    id: str
    name: Optional[str] = None
    role: Optional[str] = None


class EventActorRole(BaseModel):
    """#157 A1: a teacher reads the ROLE of the author only - no name, no id."""

    role: Optional[str] = None


class SubstitutionEvent(BaseModel):
    requestId: str  # #157 A5: the record the event belongs to (the history runs over the whole chain)
    action: str
    actor: Optional[Union[EventActor, EventActorRole]] = None
    at: str
    snapshot: Dict[str, Any] = Field(default_factory=dict)
    substituteName: Optional[str] = None  # #157 R3: the teacher a substitution passes to («Фамилия И.») on ``substituted`` / ``assignment_cleared``: the front prefixes «Новая замена:» / «Заменена на»
    mergedRequestId: Optional[str] = None  # #157 A5: «taken off» on the old record + «substituted» on the new one are ONE event; this is the new record


class ExamBinding(BaseModel):
    state: Literal["ok", "none", "unavailable"] = "none"
    title: Optional[str] = None
    canCancel: bool = True


class LessonExamBinding(ExamBinding):
    """#124: the precheck of «Отменить урок» on the lesson sheet.  ``state`` / ``title`` as ``examBinding``; ``canCancel`` / ``message`` = the answer of the SAME
    ``cancellation-check`` of exam-service that refuses the cancellation itself (an exam that is launched OR has attempts blocks it)."""

    message: Optional[str] = None


class SubstitutionDetailOut(BaseModel):
    item: SubstitutionItem
    chain: List[SubstitutionItem] = Field(default_factory=list)
    next: List[SubstitutionItem] = Field(default_factory=list)  # #157 A5: the records that came after (a reopened request, the next substitution), oldest first
    truncated: bool = False  # #157 A5: the chain / the history is longer than the limits (10 records each way, 60 events)
    events: List[SubstitutionEvent] = Field(default_factory=list)
    examBinding: Optional[ExamBinding] = None  # #157 A7: only a waiting record asked by a manager has one


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
    substitutePayEstimate: Optional[int] = None          # W1: an ESTIMATE (UZS) of what this lesson would pay him (payroll engine, roster of the lesson); null + the reason below
    substitutePayEstimateReason: Optional[str] = None    # ``RATE_MISSING`` | ``NO_STUDENTS`` when there is no estimate
    rateMissing: Optional[bool] = None  # no payroll rate for the type of the group in the month of the lesson: a yes / no for the owner and the administrator (the rates stay the owner's)


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
    model_config = ConfigDict(extra="allow")        # ``STUDENT_BUSY`` also carries ``students`` [{id, name, groupName, startTime, endTime}] (W1 S12)
    code: str
    message: str


class MoveCheckOut(BaseModel):
    ok: bool
    teacherId: Optional[str] = None
    teacherName: Optional[str] = None
    teacherConflicts: List[TeacherConflict] = Field(default_factory=list)
    groupConflicts: List[TeacherConflict] = Field(default_factory=list)
    warnings: List[MoveWarning] = Field(default_factory=list)


class LessonMoveCheckOut(MoveCheckOut):
    """#124 ``lesson-move-check`` = ``MoveCheckOut`` (the frozen B2 shape of the request check) + the refusal of the ORIGIN lesson (R3 #6): ``ok = false`` with
    ``code`` / ``message`` when POST ``lesson-move`` would refuse the lesson itself (started, cancelled, archived group, already decided, no teacher); both null otherwise."""

    code: Optional[str] = None
    message: Optional[str] = None


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
    cancellationKind: Optional[CancellationKind] = None    # cancel (W1 S11): «по вине центра» (default) | «праздник / выходной»


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
    reason: Optional[str] = None                          # 3..500 after trim: the service answers REASON_REQUIRED / REASON_TOO_LONG (M10), not a schema error
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
    cancellationKind: Optional[CancellationKind] = None   # cancel: the kind that was written (a date of a live day off turns it into ``holiday``, then ``warnings`` says so)


class DecideOut(BaseModel):
    item: SubstitutionItem
    result: DecideResult


class DecideBulkRequest(BaseModel):
    ids: List[str] = Field(..., min_length=1, max_length=50)
    decision: Literal["substitute", "cancel", "reject"]
    teacherId: Optional[str] = None
    reason: Optional[str] = Field(None, max_length=500)
    note: Optional[str] = Field(None, max_length=300)
    cancellationKind: Optional[CancellationKind] = None    # cancel (W1 S11): one kind for every request of the batch


class BulkSkipped(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    code: str
    message: str
    conflicts: Optional[List[TeacherConflict]] = None
    total: Optional[int] = None


class BulkResult(BaseModel):
    """W1 R3 (D): what ONE decided request of a bulk decision did besides deciding - a bulk cancel that turned into a holiday (``CANCELLED_AS_HOLIDAY``), released a substitute
    (``SUBSTITUTE_RELEASED``) ... is not silent."""

    id: str
    kind: Literal["substitute", "reschedule", "cancel", "reject"]
    cancellationKind: Optional[CancellationKind] = None
    warnings: List[MoveWarning] = Field(default_factory=list)


class DecideBulkOut(BaseModel):
    updated: int = 0
    items: List[SubstitutionItem] = Field(default_factory=list)
    skipped: List[BulkSkipped] = Field(default_factory=list)
    results: List[BulkResult] = Field(default_factory=list)       # one row per decided request, in the order of ``items``
    warningsTotal: int = 0                                         # the number of warnings in ``results``


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


class MovedFrom(BaseModel):
    """``movedFrom`` of the new lesson that a move created: the slot of the ORIGINAL (no reason, no comment - D-12).  Present only on such a lesson."""

    lessonId: str
    date: str
    startTime: str
    endTime: str


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


# #124 (kept apart: the frozen ``logicB2_contract.json`` lists ``ADDITIVE_FIELDS`` only): ``movedFrom`` of the new lesson that a move created
MOVED_FROM_ADDITIVE = [
    {"endpoint": "GET /org/schedule/occurrences", "field": "movedFrom", "schema": "MovedFrom|null", "audience": "everybody who sees the schedule (#124)"},
    {"endpoint": "GET /org/groups/{id}/attendance", "field": "lessons[].movedFrom", "schema": "MovedFrom (only on a moved lesson)", "audience": "everybody who reads the journal (#124)"},
    {"endpoint": "GET /org/students/{id}/attendance", "field": "lessons[].movedFrom", "schema": "MovedFrom (only on a moved lesson)", "audience": "manager (#124)"},
]

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


# ------------------------------------------------------------------------------------------ W1 S5: «Вернуть отменённый урок»
class LessonRestoreRequest(BaseModel):
    """``POST /org/groups/{id}/lessons/restore``: a cancelled lesson that has not started comes back (owner / admin); the reason is recorded (3..500 after trim)."""

    lessonId: str = Field(..., min_length=1, max_length=100)
    lessonDate: date
    reason: str = Field(..., max_length=500)


class LessonRestoreOut(BaseModel):
    ok: bool = True
    restored: bool = True
    lessonId: str
    lessonDate: str
    previousKind: Optional[CancellationKind] = None       # what the cancellation was (null: an old one / a move): the deduction of ``centre`` is gone with the row
    examsNote: str = ""                                    # the exams freed by the cancellation do not come back
    warnings: List[CancelWarning] = Field(default_factory=list)
