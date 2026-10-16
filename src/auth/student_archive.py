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

from .models import Group, GroupAudit, Student, StudentGroup
from .payments_service import CONCURRENT_EDIT_MESSAGE, is_deadlock
from .schemas import ArchivedGroupOut, StudentResponse

logger = logging.getLogger(__name__)

ARCHIVED = "archived"
ACTION_ARCHIVED = "student_archived"
ACTION_RESTORED = "student_restored"
REASON_MAX = 300
RESTORE_GROUPS_MAX = 50
GENERIC_STATUS_MESSAGE = "Используйте «В архив» / «Вернуть из архива»"


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

    A student archived before the action existed has no stamp: ``updated_at`` (the last change of the row,
    which was the archiving) is the best date there is.
    """
    if student.status != ARCHIVED:
        return {"archived_at": None, "archive_reason": None}
    moment = student.archived_at or student.updated_at
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
            "student archived student=%s groups=%d actor=%s", student.id, len(memberships), actor_id
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
        )

    # -- restore ------------------------------------------------------------------------------

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
        student.status = "active" if student.display_id else "diagnosed"
        student.archived_at = None
        student.archive_reason = None
        student.group_id = None  # the invariant of the archive; the first group enrolled below becomes primary
        self.db.commit()  # the student is back; each group below is its own "add to a group" transaction

        service = AuthService(self.db)
        restored: List[ClosedGroupOut] = []
        skipped: List[SkippedGroupOut] = []
        warnings: List[Dict[str, str]] = []
        audit_rows: List[Dict[str, Any]] = []
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
            except OperationalError as exc:
                if not is_deadlock(exc):
                    raise
                self.db.rollback()  # a lost lock race with another edit of this student: this group only
                skipped.append(
                    SkippedGroupOut(
                        group_id=group_id, group_name=group_name, reason="busy", message=CONCURRENT_EDIT_MESSAGE
                    )
                )
                continue
            restored.append(ClosedGroupOut(group_id=group_id, group_name=group_name or "—"))
            for warning in result.get("warnings", []):
                warnings.append({"group_id": group_id, **warning})
            audit_rows.append(
                {"group_id": group_id, "teacher_id": teacher_id, "membership_id": result.get("membership_id")}
            )

        if audit_rows:
            now = _utcnow_naive()
            full_name = f"{student.last_name} {student.first_name}".strip()
            for row in audit_rows:
                self.db.add(
                    GroupAudit(
                        org_id=org_id,
                        group_id=UUID(row["group_id"]),
                        actor_id=UUID(str(actor_id)),
                        actor_role=actor_role,
                        action=ACTION_RESTORED,
                        assigned_teacher_id=row["teacher_id"],
                        snapshot={
                            "student_id": str(student.id),
                            "student_name": full_name,
                            "display_id": student.display_id,
                            "membership_id": row["membership_id"],
                            "restored_at": now.isoformat(),
                        },
                        created_at=now,
                    )
                )
            self.db.commit()
        logger.info(
            "student restored student=%s groups=%d skipped=%d actor=%s",
            student.id, len(restored), len(skipped), actor_id,
        )
        base = self._response(student)
        return RestoreStudentResponse(
            **base.model_dump(), restored_groups=restored, skipped=skipped, warnings=warnings
        )
