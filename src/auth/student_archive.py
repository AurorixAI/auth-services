"""«Ученик ушёл из центра»: archiving a student and bringing him back (N13, ROADMAP 24 / 64).

Archiving closes every active membership, empties the primary group, writes one ``group_audit`` row per
group and stamps the student (``archived_at`` / ``archive_reason``) - all in ONE transaction.  Nothing is
deleted: payments, receipts, lesson marks (``roster_snapshot``), excuses and payroll sheets stay, and the
closed memberships keep their payment history (the payments page lists them without actions).

Invariants (after ``archive`` and until ``restore``):
  * ``students.status == 'archived'``  =>  no active membership, ``students.group_id IS NULL``;
  * the groups' student counts and every "who is in the group" list count active memberships of students
    who are NOT archived (``student_not_archived``), so a stale row can never inflate a group.

Lock order (the one of the payments module: student -> membership -> payment; groups last, in id order):
the student is locked FOR NO KEY UPDATE (it still excludes the FOR UPDATE of ``add_student_to_group`` and
``remove_student_from_group`` and another archiving, but not the FOR KEY SHARE that a payment insert takes
on the student through its foreign key - a plain FOR UPDATE here could deadlock with a payment being
recorded), then the memberships FOR NO KEY UPDATE (as the payments module does), then the groups.

Diagnostic invites (ROADMAP #89): in the same transaction every unfinished link of the student - status
``pending`` or ``confirmed`` - becomes ``cancelled`` (``cancelled_at`` = the archiving instant,
``cancel_reason`` = ``student_archived``), so the code stops working at once.  A ``in_progress`` invite is NOT
cancelled: the student may finish, and the result stays on his card.  ``completed`` / ``expired`` stay as they
are.  Restoring the student does not revive a cancelled invite (a new one is created).

The module imports ``AuthService`` lazily: ``service.py`` imports this module for the guards.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .models import DiagnosticInvite, Group, GroupAudit, Student, StudentDiagnosticResult, StudentGroup
from .payments_service import CONCURRENT_EDIT_MESSAGE, is_deadlock
from .schemas import ArchivedGroupOut, StudentResponse

logger = logging.getLogger(__name__)

ARCHIVED = "archived"
ACTION_ARCHIVED = "student_archived"
ACTION_RESTORED = "student_restored"
REASON_MAX = 300
RESTORE_GROUPS_MAX = 50
GENERIC_STATUS_MESSAGE = "Используйте «В архив» / «Вернуть из архива»"
RESTORE_FAILED_MESSAGE = "Не удалось вернуть в группу — повторите позже"
INVITE_CANCEL_REASON = "student_archived"
INVITE_CANCELLED_MESSAGE = "Приглашение отменено"
ARCHIVED_STUDENT_INVITE_MESSAGE = (
    "Ученик в архиве: диагностику создать нельзя. Сначала верните ученика из архива."
)
# Invite states the archiving switches off / leaves alone (the others are over already).
INVITE_CANCELLABLE = ("pending", "confirmed")


# ---------------------------------------------------------------------------------------------- errors


class ArchiveError(ValueError):
    """Base: a refusal with an HTTP status (``ValueError`` so that a caller that only knows 400 still works)."""

    http_status = 400


class StudentNotFound(ArchiveError):
    http_status = 404


class ArchiveConflict(ArchiveError):
    """The student is in the wrong state for the action (already archived / not archived / a lesson is open)."""

    http_status = 409


class ArchiveStatusViolation(ArchiveError):
    """The generic profile edit tried to move a student into or out of the archive."""

    http_status = 422


# ------------------------------------------------------------------------------------- request/response


class ArchiveStudentRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=REASON_MAX)

    @field_validator("reason")
    @classmethod
    def _blank_is_none(cls, value: Optional[str]) -> Optional[str]:
        value = (value or "").strip()
        return value or None


class RestoreStudentRequest(BaseModel):
    group_ids: Optional[List[UUID]] = Field(None, max_length=RESTORE_GROUPS_MAX)


class ClosedGroupOut(BaseModel):
    group_id: str
    group_name: str


class SkippedGroupOut(BaseModel):
    """A group the return could not enrol the student in: ``reason`` is a machine code
    (``group_full`` / ``group_archived`` / ``not_found`` / ``lesson_in_progress`` / ``refused`` / ``busy``),
    ``message`` the Russian text for the administrator."""

    group_id: str
    group_name: Optional[str] = None
    reason: str
    message: str


class ArchiveStudentResponse(StudentResponse):
    closed_groups: List[ClosedGroupOut] = []
    # ROADMAP #89: unfinished diagnostic links switched off by this archiving, and the ones left running
    # (``in_progress``: the student may finish, the result stays on his card).
    diagnostic_invites_cancelled: int = 0
    diagnostic_invites_in_progress: int = 0


class RestoreStudentResponse(StudentResponse):
    restored_groups: List[ClosedGroupOut] = []
    skipped: List[SkippedGroupOut] = []
    warnings: List[Dict[str, str]] = []


# -------------------------------------------------------------------------------------------- helpers


def _utcnow_naive() -> datetime:
    """Naive UTC, the convention of ``student_groups.joined_at`` / ``left_at``."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def student_not_archived():
    """SQL condition for a query on ``StudentGroup``: the membership's student is not in the archive."""
    return StudentGroup.student.has(Student.status != ARCHIVED)


# The refusals of ``AuthService.add_student_to_group`` that a return can meet, as machine codes for the window
# (the Russian text travels along in ``message``).  A test pins every pair, so a reworded message cannot
# silently turn a code into "refused".
_SKIP_CODES = (
    ("достигнут лимит", "group_full"),
    ("архивную группу", "group_archived"),
    ("Группа не найдена", "not_found"),
    ("пока идёт урок", "lesson_in_progress"),
)


def skip_code(message: str) -> str:
    for needle, code in _SKIP_CODES:
        if needle in message:
            return code
    return "refused"


def archive_stamp(student: Student) -> Dict[str, Optional[str]]:
    """``archived_at`` / ``archive_reason`` of a ``StudentResponse`` (both null unless archived).

    Students archived before the action existed were stamped by the migration (``archived_at`` = their
    ``updated_at``, converted to naive UTC); a row that still has none (archived by an old image after
    the migration) answers ``None`` - the caller must tolerate it.
    """
    if student.status != ARCHIVED:
        return {"archived_at": None, "archive_reason": None}
    moment = student.archived_at
    return {
        "archived_at": moment.isoformat() if moment else None,
        "archive_reason": student.archive_reason,
    }


def guard_generic_status_update(student: Student, requested_status: Optional[str]) -> None:
    """``PUT /org/students/{id}`` neither archives a student nor brings him back (a status that does not
    change is not a change: an edit form may send the whole record)."""
    if requested_status is None or requested_status == student.status:
        return
    if requested_status == ARCHIVED or student.status == ARCHIVED:
        raise ArchiveStatusViolation(GENERIC_STATUS_MESSAGE)


def actor_role_for_audit(user: dict) -> str:
    """The value ``group_audit.actor_role`` accepts (owner / admin / superadmin)."""
    org_role = user.get("org_role")
    if org_role in ("owner", "admin"):
        return org_role
    if user.get("role") == "superadmin":
        return "superadmin"
    return "admin"


def _group_active_counts(db: Session, group_ids: Sequence[UUID]) -> Dict[UUID, int]:
    if not group_ids:
        return {}
    return {
        group_id: count
        for group_id, count in db.query(StudentGroup.group_id, func.count(StudentGroup.id))
        .filter(
            StudentGroup.group_id.in_(list(group_ids)),
            StudentGroup.is_active.is_(True),
            student_not_archived(),
        )
        .group_by(StudentGroup.group_id)
        .all()
    }


def archived_groups_of(db: Session, org_id: str, student: Student) -> List[ArchivedGroupOut]:
    """The groups the LAST archiving closed, for the return window (primary group first, then by name).

    Read from the audit rows of that very event (same instant as ``students.archived_at``), so a student
    who was archived, returned and archived again offers the groups of the second archiving only; one
    archived before the action existed (no stamp) offers nothing.
    """
    if student.status != ARCHIVED or student.archived_at is None:
        return []
    rows = (
        db.query(GroupAudit)
        .filter(
            GroupAudit.org_id == org_id,
            GroupAudit.action == ACTION_ARCHIVED,
            GroupAudit.snapshot["student_id"].astext == str(student.id),
            GroupAudit.created_at == student.archived_at,
        )
        .all()
    )
    if not rows:
        return []
    primary = {row.group_id for row in rows if (row.snapshot or {}).get("was_primary")}
    groups = {
        g.id: g
        for g in db.query(Group).filter(Group.org_id == org_id, Group.id.in_([r.group_id for r in rows])).all()
    }
    counts = _group_active_counts(db, list(groups))
    out = []
    for group in sorted(groups.values(), key=lambda g: (g.id not in primary, (g.name or "").lower(), str(g.id))):
        out.append(
            ArchivedGroupOut(
                group_id=str(group.id),
                group_name=group.name,
                group_status=group.status or "active",
                is_full=group.max_students is not None and counts.get(group.id, 0) >= group.max_students,
            )
        )
    return out


def archived_groups_payload(db: Session, org_id: str, student: Student) -> Optional[List[Dict[str, Any]]]:
    """Top level of the student profile: the groups of the last archiving (a list, maybe empty) for an
    archived student, ``None`` for everybody else."""
    if student.status != ARCHIVED:
        return None
    return [g.model_dump() for g in archived_groups_of(db, org_id, student)]


def attach_archived_groups(db: Session, org_id: str, student: Student, response: StudentResponse) -> StudentResponse:
    """GET /org/students/{id}: an archived student also lists the groups the return window offers."""
    if student.status == ARCHIVED:
        response.archived_groups = archived_groups_of(db, org_id, student)
    return response


# -------------------------------------------------------------------------------------------- service


class StudentArchiveService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # -- shared -------------------------------------------------------------------------------

    def _lock_student(self, org_id: str, student_id: str) -> Student:
        try:
            student_uuid = UUID(str(student_id))
        except (TypeError, ValueError):
            raise StudentNotFound("Ученик не найден")
        student = (
            self.db.query(Student)
            .filter(Student.id == student_uuid, Student.org_id == org_id)
            .with_for_update(key_share=True)  # FOR NO KEY UPDATE, see the module docstring
            .populate_existing()
            .first()
        )
        if not student:
            raise StudentNotFound("Ученик не найден")
        return student

    def _response(self, student: Student) -> StudentResponse:
        from .service import AuthService  # lazy: service imports this module

        return AuthService(self.db)._build_student_response(student)

    # -- archive ------------------------------------------------------------------------------

    def archive(
        self, org_id: str, student_id: str, *, actor_id: str, actor_role: str, reason: Optional[str] = None
    ) -> ArchiveStudentResponse:
        from .service import AuthService  # lazy: service imports this module

        now = _utcnow_naive()
        student = self._lock_student(org_id, student_id)
        if student.status == ARCHIVED:
            self.db.rollback()
            raise ArchiveConflict("Ученик уже в архиве")

        memberships = (
            self.db.query(StudentGroup)
            .filter(StudentGroup.student_id == student.id, StudentGroup.is_active.is_(True))
            .order_by(StudentGroup.id)
            .with_for_update(key_share=True)  # FOR NO KEY UPDATE: an FK insert (a payment) does not wait
            .populate_existing()
            .all()
        )
        group_ids = [m.group_id for m in memberships]
        service = AuthService(self.db)
        try:
            # The groups last, in id order (as a transfer does): archiving and "start the lesson" exclude
            # each other, and a roster never changes under a lesson that is in progress.
            service._lock_groups_for_membership_change(org_id, group_ids)
            service._assert_no_open_attendance_for_groups(org_id, group_ids)
        except ValueError as exc:
            self.db.rollback()
            raise ArchiveConflict(str(exc)) from exc
        groups = {
            g.id: g for g in self.db.query(Group).filter(Group.id.in_(group_ids)).all()
        } if group_ids else {}

        primary_group_id = student.group_id
        for membership in memberships:
            membership.is_active = False
            membership.left_at = now
        invites_cancelled, invites_in_progress = self._cancel_open_invites(org_id, student.id, now)
        student.status = ARCHIVED
        student.group_id = None
        student.archived_at = now
        student.archive_reason = reason

        ordered = sorted(
            memberships,
            key=lambda m: (m.group_id != primary_group_id, m.joined_at or datetime.min, str(m.id)),
        )
        full_name = f"{student.last_name} {student.first_name}".strip()
        for membership in ordered:
            group = groups.get(membership.group_id)
            self.db.add(
                GroupAudit(
                    org_id=org_id,
                    group_id=membership.group_id,
                    actor_id=UUID(str(actor_id)),
                    actor_role=actor_role,
                    action=ACTION_ARCHIVED,
                    assigned_teacher_id=group.teacher_id if group else None,
                    snapshot={
                        "student_id": str(student.id),
                        "student_name": full_name,
                        "display_id": student.display_id,
                        "membership_id": str(membership.id),
                        "was_primary": membership.group_id == primary_group_id,
                        "joined_at": membership.joined_at.isoformat() if membership.joined_at else None,
                        "archived_at": now.isoformat(),
                        "reason": reason,
                    },
                    created_at=now,
                )
            )
        self.db.commit()
        logger.info(
            "student archived student=%s groups=%d invites_cancelled=%d invites_in_progress=%d actor=%s",
            student.id, len(memberships), invites_cancelled, invites_in_progress, actor_id,
        )
        base = self._response(student)
        return ArchiveStudentResponse(
            **base.model_dump(),
            closed_groups=[
                ClosedGroupOut(
                    group_id=str(m.group_id),
                    group_name=groups[m.group_id].name if m.group_id in groups else "—",
                )
                for m in ordered
            ],
            diagnostic_invites_cancelled=invites_cancelled,
            diagnostic_invites_in_progress=invites_in_progress,
        )

    def _cancel_open_invites(self, org_id: str, student_id: UUID, now: datetime) -> tuple:
        """Switch off the student's unfinished diagnostic links; ``(cancelled, left_in_progress)``.

        One UPDATE with the status in its WHERE clause: an invite that a concurrent ``start`` just moved to
        ``in_progress`` (it holds the row lock; this UPDATE waits and re-reads the row) is left alone, and one
        that a concurrent ``confirm`` moved to ``confirmed`` is cancelled.  Runs in the archiving transaction.
        """
        cancelled = (
            self.db.query(DiagnosticInvite)
            .filter(
                DiagnosticInvite.org_id == org_id,
                DiagnosticInvite.student_id == student_id,
                DiagnosticInvite.status.in_(INVITE_CANCELLABLE),
            )
            .update(
                {
                    "status": "cancelled",
                    "cancelled_at": now,
                    "cancel_reason": INVITE_CANCEL_REASON,
                },
                synchronize_session=False,
            )
        )
        in_progress = (
            self.db.query(func.count(DiagnosticInvite.id))
            .filter(
                DiagnosticInvite.org_id == org_id,
                DiagnosticInvite.student_id == student_id,
                DiagnosticInvite.status == "in_progress",
            )
            .scalar()
            or 0
        )
        return int(cancelled or 0), int(in_progress)

    # -- restore ------------------------------------------------------------------------------

    def _status_after_return(self, org_id: str, student: Student) -> str:
        """``active`` with a public ID (he was enrolled once); without one ``diagnosed`` only if a completed
        diagnostic exists, otherwise ``new`` (he never got further than the lobby)."""
        if student.display_id:
            return "active"
        has_result = (
            self.db.query(StudentDiagnosticResult.id)
            .filter(StudentDiagnosticResult.student_id == student.id, StudentDiagnosticResult.org_id == org_id)
            .first()
        )
        return "diagnosed" if has_result else "new"

    def _write_restored_audit(
        self,
        org_id: str,
        student: Student,
        group_id: str,
        teacher_id: Optional[UUID],
        membership_id: Optional[str],
        actor_id: str,
        actor_role: str,
    ) -> None:
        now = _utcnow_naive()
        self.db.add(
            GroupAudit(
                org_id=org_id,
                group_id=UUID(group_id),
                actor_id=UUID(str(actor_id)),
                actor_role=actor_role,
                action=ACTION_RESTORED,
                assigned_teacher_id=teacher_id,
                snapshot={
                    "student_id": str(student.id),
                    "student_name": f"{student.last_name} {student.first_name}".strip(),
                    "display_id": student.display_id,
                    "membership_id": membership_id,
                    "restored_at": now.isoformat(),
                },
                created_at=now,
            )
        )
        self.db.commit()

    def restore(
        self,
        org_id: str,
        student_id: str,
        *,
        actor_id: str,
        actor_role: str,
        group_ids: Optional[Sequence[UUID]] = None,
    ) -> RestoreStudentResponse:
        from .service import AuthService  # lazy: service imports this module

        student = self._lock_student(org_id, student_id)
        if student.status != ARCHIVED:
            self.db.rollback()
            raise ArchiveConflict("Ученик не в архиве")
        student.status = self._status_after_return(org_id, student)
        student.archived_at = None
        student.archive_reason = None
        student.group_id = None  # the invariant of the archive; the first group enrolled below becomes primary
        self.db.commit()  # the student is back; each group below is its own "add to a group" transaction

        service = AuthService(self.db)
        restored: List[ClosedGroupOut] = []
        skipped: List[SkippedGroupOut] = []
        warnings: List[Dict[str, str]] = []
        seen: set = set()
        for raw_group_id in group_ids or ():
            group_id = str(raw_group_id)
            if group_id in seen:
                continue
            seen.add(group_id)
            group = self.db.query(Group).filter(Group.id == group_id, Group.org_id == org_id).first()
            group_name = group.name if group else None
            teacher_id = group.teacher_id if group else None
            try:
                # "add", not "transfer": the groups are independent; the first one becomes primary.
                result = service.add_student_to_group(
                    org_id, group_id, str(student.id), mode="add", actor_id=actor_id
                )
            except ValueError as exc:  # full / archived / not found / a lesson is open: the rest goes on
                self.db.rollback()
                skipped.append(
                    SkippedGroupOut(
                        group_id=group_id, group_name=group_name, reason=skip_code(str(exc)), message=str(exc)
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001  one broken group must not lose the groups already restored
                self.db.rollback()
                if isinstance(exc, OperationalError) and is_deadlock(exc):
                    # a lost lock race with another edit of this student: this group only
                    code, message = "busy", CONCURRENT_EDIT_MESSAGE
                else:
                    logger.exception(
                        "restore: enrolling student=%s in group=%s failed actor=%s", student.id, group_id, actor_id
                    )
                    code, message = "refused", RESTORE_FAILED_MESSAGE
                skipped.append(
                    SkippedGroupOut(group_id=group_id, group_name=group_name, reason=code, message=message)
                )
                continue
            restored.append(ClosedGroupOut(group_id=group_id, group_name=group_name or "—"))
            for warning in result.get("warnings", []):
                warnings.append({"group_id": group_id, **warning})
            # The journal row of THIS group, right after its commit: a later group that fails cannot lose it.
            try:
                self._write_restored_audit(
                    org_id, student, group_id, teacher_id, result.get("membership_id"), actor_id, actor_role
                )
            except Exception:  # noqa: BLE001  the student IS in the group; a missing journal row is logged
                self.db.rollback()
                logger.exception(
                    "restore: journal row for student=%s group=%s failed actor=%s", student.id, group_id, actor_id
                )

        logger.info(
            "student restored student=%s groups=%d skipped=%d actor=%s",
            student.id, len(restored), len(skipped), actor_id,
        )
        base = self._response(student)
        return RestoreStudentResponse(
            **base.model_dump(), restored_groups=restored, skipped=skipped, warnings=warnings
        )
