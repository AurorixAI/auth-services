"""Request / response models of the operations package (LOGIC-B, SPEC section 4: the frozen contract).

``schemas.py`` is not edited (mixed CRLF/LF, SPEC 0.5): every changed shape is a subclass here.  The router and the
service import these under the names they used before, so the code reads as before.
All new fields are additive; a dict key spelled in camelCase / snake_case is spelled exactly as in the SPEC.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .import_service import (
    ImportCommitResponse,
    ImportCounts,
    ImportIssue,
    ImportPreviewResponse,
)
from .schemas import (
    CreateLessonRequest,
    CreateOneOffLessonRequest,
    GroupAttendanceResponse,
    GroupResponse,
    InviteMemberRequest,
    OpenAttendanceActionResponse,
    ScheduleLessonResponse,
    ScheduleOccurrenceResponse,
    SaveAttendanceRequest,
    StudentResponse,
    TeacherDashboardSummaryResponse,
    AcceptInviteRequest,
)

# ------------------------------------------------------------------------------------------------ common


class OpsWarning(BaseModel):
    code: str
    message: str


class OpsErrorDetail(BaseModel):
    """``detail`` of every coded refusal; the extra keys depend on the code (``extra="allow"``)."""

    model_config = ConfigDict(extra="allow")
    code: str
    message: str


class OpsErrorResponse(BaseModel):
    detail: OpsErrorDetail


class TeacherConflict(BaseModel):
    """One overlap of a teacher's lessons (``TEACHER_BUSY.conflicts[]``)."""

    date: str
    startTime: str
    endTime: str
    groupId: str
    groupName: str
    withGroupName: str


class TeacherBusyDetail(OpsErrorDetail):
    conflicts: List[TeacherConflict] = Field(default_factory=list)
    total: int = 0


# ------------------------------------------------------------------------------------------- 4.1 schedule


class LessonTeacherInfo(BaseModel):
    teacherId: Optional[str] = None
    teacherName: Optional[str] = None
    isSubstitution: bool = False
    defaultTeacherId: Optional[str] = None
    defaultTeacherName: Optional[str] = None


class CreateLessonRequestB(CreateLessonRequest):
    """``room`` has no default (``null`` = "Кабинет не указан"); ``validFrom`` defaults to the business today."""

    room: Optional[str] = None
    validFrom: Optional[date] = None


class CreateOneOffLessonRequestB(CreateOneOffLessonRequest):
    room: Optional[str] = None


class ScheduleLessonResponseB(ScheduleLessonResponse):
    room: Optional[str] = None
    validFrom: Optional[str] = None
    warnings: List[OpsWarning] = Field(default_factory=list)


class ScheduleOccurrenceResponseB(ScheduleOccurrenceResponse):
    room: Optional[str] = None
    originalRoom: Optional[str] = None
    validFrom: Optional[str] = None
    warnings: List[OpsWarning] = Field(default_factory=list)
    teacherId: Optional[str] = None
    teacherName: Optional[str] = None
    isSubstitution: bool = False
    defaultTeacherId: Optional[str] = None
    defaultTeacherName: Optional[str] = None
    # LOGIC-B2 (additive): the pending request of the lesson (manager and the requester only) and what happened to a cancelled original (no reasons)
    substitutionRequest: Optional[Dict[str, Any]] = None
    substitutionOutcome: Optional[Dict[str, Any]] = None


# -------------------------------------------------------------------------------- 4.2 journal and marks


class AttendanceUpdatedBy(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None


class GroupAttendanceResponseB(GroupAttendanceResponse):
    # {"<date>_<lessonId>": version}
    versions: Dict[str, int] = Field(default_factory=dict)
    # {"<date>_<lessonId>": who teaches that occurrence}
    lessonTeachers: Dict[str, LessonTeacherInfo] = Field(default_factory=dict)
    # LOGIC-B2 (additive): {"<date>_<lessonId>": {id, status, reasonCode, teacherName}} for the manager / the requester; ``lessonRoster`` only for a substitute
    substitutionRequests: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    lessonRoster: Optional[List[Dict[str, Any]]] = None


class SaveAttendanceRequestB(SaveAttendanceRequest):
    expectedVersion: Optional[int] = Field(None, ge=1)
    acknowledgeLiveExam: bool = False


class ExamSyncOut(BaseModel):
    released: List[str] = Field(default_factory=list)
    leftRunning: List[str] = Field(default_factory=list)
    warning: Optional[str] = None


class StartAttendanceOut(BaseModel):
    ok: bool = True
    started: bool = True
    version: int


class ReconcileOut(BaseModel):
    ok: bool = True
    reconciled: bool = True
    version: int


class ReconcileCancelOut(BaseModel):
    ok: bool = True
    cancelled: bool = True
    version: int


class SaveAttendanceOut(BaseModel):
    ok: bool = True
    saved: bool = True
    version: int
    examSync: Optional[ExamSyncOut] = None


class AttendanceConflictDetail(OpsErrorDetail):
    currentVersion: int
    updatedBy: AttendanceUpdatedBy
    updatedAt: Optional[str] = None
    records: Dict[str, str] = Field(default_factory=dict)


class LessonTeacherChangedDetail(OpsErrorDetail):
    currentTeacherId: Optional[str] = None
    teacher_name: Optional[str] = None


class LiveExamItem(BaseModel):
    examId: str
    title: str
    status: Literal["active", "lobby", "print_ready"]
    remainingSeconds: Optional[int] = None
    waitingCount: int = 0


class LiveExamDetail(OpsErrorDetail):
    exams: List[LiveExamItem] = Field(default_factory=list)


class OpenAttendanceActionResponseB(OpenAttendanceActionResponse):
    version: Optional[int] = None
    requiresAdmin: bool = False
    ageDays: int = 0
    isSubstitution: bool = False


class GroupResponseB(GroupResponse):
    archivedAt: Optional[str] = None
    archiveReason: Optional[str] = None
    # "My groups" of a teacher: the group is shown only because of a substitution
    substitution: bool = False


class TeacherDashboardSummaryResponseB(TeacherDashboardSummaryResponse):
    groups: List[GroupResponseB] = Field(default_factory=list)
    todayLessons: List[ScheduleOccurrenceResponseB] = Field(default_factory=list)
    scheduleWindow: List[ScheduleOccurrenceResponseB] = Field(default_factory=list)
    openAttendanceActions: List[OpenAttendanceActionResponseB] = Field(default_factory=list)


# -------------------------------------------------------------------------------------- 4.3 lesson teacher


class LessonTeacherPut(BaseModel):
    lessonDate: date
    lessonId: str = Field(..., min_length=1, max_length=100)
    teacherId: Optional[str] = None  # null = take the substitution off
    reason: str = Field(..., min_length=3, max_length=300)
    expectedTeacherId: Optional[str] = None


class LessonTeacherPayrollSheet(BaseModel):
    sheet_id: Optional[str] = None
    teacher_id: str
    month: str
    status: Optional[str] = None


class LessonTeacherOut(BaseModel):
    groupId: str
    lessonId: str
    lessonDate: str
    teacherId: Optional[str] = None
    teacherName: Optional[str] = None
    isSubstitution: bool = False
    defaultTeacherId: Optional[str] = None
    defaultTeacherName: Optional[str] = None
    attendanceUpdated: bool = False
    version: Optional[int] = None
    payrollSheetsAffected: List[LessonTeacherPayrollSheet] = Field(default_factory=list)
    requestClosed: Optional[Dict[str, Any]] = None  # LOGIC-B2: the pending substitution request this assignment decided


class BusyWith(BaseModel):
    groupName: str
    startTime: str
    endTime: str


class LessonTeacherCandidate(BaseModel):
    teacherId: str
    name: str
    busy: bool = False
    busyWith: Optional[BusyWith] = None


class LessonTeacherCandidatesOut(BaseModel):
    lessonId: str
    lessonDate: str
    startTime: str
    endTime: str
    currentTeacherId: Optional[str] = None
    defaultTeacherId: Optional[str] = None
    candidates: List[LessonTeacherCandidate] = Field(default_factory=list)


# ------------------------------------------------------------------------------------------ 4.4 group archive


class ArchiveBlocker(BaseModel):
    code: Literal["ACTIVE_STUDENTS", "OPEN_LESSON", "MISSED_LESSONS", "LIVE_EXAMS"]
    count: int
    students: Optional[List[Dict[str, Any]]] = None
    lessons: Optional[List[Dict[str, Any]]] = None
    exams: Optional[List[Dict[str, Any]]] = None


class ArchiveAutoAction(BaseModel):
    code: Literal["RELEASE_EXAMS", "STOP_FUTURE_LESSONS"]
    count: int


class ArchiveImpactOut(BaseModel):
    groupId: str
    groupName: str
    status: str
    canArchive: bool
    examService: Literal["ok", "unavailable"] = "ok"
    blockers: List[ArchiveBlocker] = Field(default_factory=list)
    autoActions: List[ArchiveAutoAction] = Field(default_factory=list)
    warnings: List[Dict[str, Any]] = Field(default_factory=list)
    # Legacy: a group archived before the operation existed that still holds students.
    legacyStudentsLeft: int = 0


class ArchiveGroupRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=300)
    missedLessonsAction: Optional[Literal["cancel"]] = None

    @field_validator("reason")
    @classmethod
    def _strip(cls, value):
        value = (value or "").strip()
        return value or None


class ArchiveGroupOut(GroupResponseB):
    releasedExamIds: List[str] = Field(default_factory=list)
    cancelledMissedLessons: int = 0
    closedSubstitutionRequests: int = 0  # LOGIC-B2
    # exam-service did not confirm the release at archive time: the group IS archived, the release is retried (durable marker)
    examReleasePending: bool = False


class RestoreGroupRequest(BaseModel):
    teacherId: Optional[str] = None


class GroupArchiveBlockedDetail(OpsErrorDetail):
    blockers: List[ArchiveBlocker] = Field(default_factory=list)


class TeacherInactiveDetail(OpsErrorDetail):
    teacherName: Optional[str] = None


# ------------------------------------------------------------------------------------------- 4.5 staff


class MemberOut(BaseModel):
    """``GET /org/members`` item: the user's own fields (``User.to_dict``) + the membership."""

    model_config = ConfigDict(extra="allow")
    id: str
    email: str
    first_name: str
    last_name: str
    full_name: Optional[str] = None
    role: Optional[str] = None
    org_role: str
    member_id: str
    is_active: bool = True
    removed_at: Optional[str] = None


class RemovalGroup(BaseModel):
    id: str
    name: str
    studentCount: int = 0
    nextLesson: Optional[Dict[str, str]] = None


class RemovalLesson(BaseModel):
    groupId: str
    groupName: str
    lessonDate: str
    lessonId: str


class CountOut(BaseModel):
    count: int = 0


class RemovalImpactOut(BaseModel):
    memberId: str
    userId: str
    name: Optional[str] = None
    orgRole: str
    canRemove: bool
    groups: List[RemovalGroup] = Field(default_factory=list)
    inProgressLessons: List[RemovalLesson] = Field(default_factory=list)
    futureSubstitutions: CountOut = Field(default_factory=CountOut)
    missedLessons: CountOut = Field(default_factory=CountOut)
    pendingSubstitutionRequests: CountOut = Field(default_factory=CountOut)  # LOGIC-B2


class HandoverAssignment(BaseModel):
    groupId: str
    teacherId: Optional[str] = None


class HandoverRequest(BaseModel):
    assignments: List[HandoverAssignment] = Field(default_factory=list)
    confirmUnassigned: bool = False


class HandedOverGroup(BaseModel):
    groupId: str
    groupName: str
    fromTeacherId: Optional[str] = None
    toTeacherId: Optional[str] = None


class HandoverOut(BaseModel):
    handedOver: List[HandedOverGroup] = Field(default_factory=list)
    remainingGroups: List[Dict[str, Any]] = Field(default_factory=list)
    releasedSubstitutions: int = 0
    closedSubstitutionRequests: int = 0  # LOGIC-B2


class RemoveMemberOut(BaseModel):
    message: str
    releasedSubstitutions: int = 0
    closedSubstitutionRequests: int = 0  # LOGIC-B2


class RestoreMemberOut(BaseModel):
    member_id: str
    is_active: bool = True


class MemberHasAssignmentsDetail(OpsErrorDetail):
    removal: Optional[RemovalImpactOut] = None
    # the body of the refusal is "as removal-impact": the fields are repeated at the top level
    memberId: Optional[str] = None
    canRemove: Optional[bool] = None
    groups: List[RemovalGroup] = Field(default_factory=list)
    inProgressLessons: List[RemovalLesson] = Field(default_factory=list)
    futureSubstitutions: Optional[CountOut] = None
    missedLessons: Optional[CountOut] = None


# --------------------------------------------------------------------------------------- 4.6 invitations


class InviteMemberRequestB(InviteMemberRequest):
    """``email`` is mandatory, checked by the service (422 ``EMAIL_REQUIRED``), normalised ``lower().strip()``."""

    email: Optional[str] = Field(None, max_length=255)


class InvitationOut(BaseModel):
    id: str
    code: str
    org_role: str
    email: Optional[str] = None
    expires_at: str
    status: Literal["pending", "accepted", "expired", "cancelled"]
    created_at: Optional[str] = None
    is_returning: bool = False


class InviteInfoOut(BaseModel):
    org_name: str
    org_role: str
    email: Optional[str] = None
    is_returning: bool = False


class AcceptInviteRequestB(AcceptInviteRequest):
    """The e-mail comes from the invitation; a sent one that differs is refused (400 ``INVITE_EMAIL_MISMATCH``)."""

    email: Optional[str] = Field(None, max_length=255)
    # required for a NEW member (checked by the service), optional for a returning one (an existing name is never overwritten)
    first_name: Optional[str] = Field(None, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v):  # type: ignore[override]
        return v.lower().strip() if v else None


# ------------------------------------------------------------------------------------------- 4.7 import


class ImportIssueB(ImportIssue):
    code: Optional[str] = None


class ImportSkipped(BaseModel):
    sheet: str
    row: int
    code: str
    message: str
    student: Optional[str] = None
    group: Optional[str] = None


class ImportCountsB(ImportCounts):
    students_enrolled: int = 0
    students_unchanged: int = 0
    students_skipped: int = 0


class CreatedGroup(BaseModel):
    id: str
    name: str


class ImportPreviewResponseB(ImportPreviewResponse):
    counts: ImportCountsB
    issues: List[ImportIssueB]
    skipped: List[ImportSkipped] = Field(default_factory=list)


class ImportCommitResponseB(ImportCommitResponse):
    counts: ImportCountsB
    issues: List[ImportIssueB]
    skipped: List[ImportSkipped] = Field(default_factory=list)
    created_groups: List[CreatedGroup] = Field(default_factory=list)


# ----------------------------------------------------------------------------------------- 4.8 students

StudentStatusView = Literal["new", "diagnosed", "active", "no_group", "archived"]


class StudentResponseB(StudentResponse):
    has_group: bool = False
    status_view: StudentStatusView = "new"


class OrgStatsOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    total_students: int = 0
    total_groups: int = 0
    total_members: int = 0
    active_diagnostics: int = 0
    completed_diagnostics: int = 0
    new_students: int = 0
    no_group_students: int = 0


# ----------------------------------------------------------------------- 4.9 exam-service / auth (internal)


class ExamCompletionItem(BaseModel):
    examId: str
    title: str
    status: Literal["active", "lobby", "print_ready"]
    deliveryMode: Optional[str] = None
    remainingSeconds: Optional[int] = None
    waitingCount: int = 0
    endsAt: Optional[str] = None


class CompletionCheckOut(BaseModel):
    ok: bool = True
    exams: List[ExamCompletionItem] = Field(default_factory=list)


class CompletedOut(BaseModel):
    ok: bool = True
    released: List[str] = Field(default_factory=list)
    leftRunning: List[str] = Field(default_factory=list)


class GroupArchiveImpactExam(BaseModel):
    examId: str
    title: str
    status: str
    lessonDate: Optional[str] = None
    canRelease: bool = True
    blockerReason: Optional[str] = None


class GroupArchiveImpactOut(BaseModel):
    exams: List[GroupArchiveImpactExam] = Field(default_factory=list)


class GroupArchiveReleaseOut(BaseModel):
    ok: bool = True
    releasedExamIds: List[str] = Field(default_factory=list)


class VerifyStudentAsOfOut(BaseModel):
    """Answer of ``GET /public/groups/{gid}/verify-student?as_of=...`` (same shape as without ``as_of``)."""

    model_config = ConfigDict(extra="allow")
    enrolled: bool
    group_name: str = ""
    message: Optional[str] = None
    student: Optional[Dict[str, Any]] = None


class LessonBindingRequest(BaseModel):
    """Body of ``POST /internal/lesson-bindings/completion-check`` and ``.../completed`` (exam-service)."""

    org_id: str
    group_id: str
    lesson_id: str
    lesson_date: str
    actor_id: Optional[str] = None  # only ``completed``


class GroupBindingRequest(BaseModel):
    """Body of ``POST /internal/group-bindings/archive-impact`` and ``.../archive-release`` (exam-service)."""

    org_id: str
    group_id: str
    actor_id: Optional[str] = None
    reason: Optional[str] = None
