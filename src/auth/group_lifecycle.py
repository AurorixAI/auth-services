"""Archiving a group as an OPERATION (LOGIC-B O4 / F2, SPEC 1 "O4 + F2" and 4.4).

``GET /org/groups/{id}/archive-impact``, ``POST .../archive``, ``POST .../restore``.  A free ``status`` string in ``PUT /org/groups/{id}`` is gone.

Blockers (409 ``GROUP_ARCHIVE_BLOCKED``): ``ACTIVE_STUDENTS``, ``OPEN_LESSON`` (an in-progress lesson), ``MISSED_LESSONS`` (never-started past
lessons of the window - unless ``missedLessonsAction="cancel"``), ``LIVE_EXAMS`` (an exam that is active or has attempts).
Automatic: the untouched lobby / print exams go back to draft (exam-service, all or nothing), the future lessons stop being generated (an inactive
interval ``{from: now, to: null}`` in ``groups.inactive_intervals``).  An archived group is read-only (``GROUP_ARCHIVED`` 409 in the writers).

Atomicity (INV-O4-4): everything local is flushed first, then exam-service releases the exams, then the local commit; a refusal or an outage of
exam-service rolls the local part back (503 ``EXAM_SERVICE_UNAVAILABLE``, fail-closed like the cancellation of a lesson).
Restore: the open interval is closed at ``now`` (lessons between archiving and restoring never come to life, INV-O4-5); the students do not come back.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func

from . import business_clock as bc
from . import exam_bridge, lesson_substitutions, schedule_rules
from .lesson_teachers import TEACHING_ROLES, LessonTeachers, TeacherBook, _s, _uuid
from .models import (
    AttendanceExcuse,
    Group,
    GroupAttendance,
    GroupAttendanceAudit,
    GroupAudit,
    Student,
    StudentGroup,
    User,
)
from .ops_rules import (
    ERROR_CATALOGUE,
    OpsError,
    conflict,
    hhmm_to_minutes,
    not_found,
    short_name,
    unavailable,
)
from .student_archive import student_not_archived

logger = logging.getLogger(__name__)

FUTURE_HORIZON_DAYS = 56
CANCEL = "cancel"


def _blocker_reason(exam: Dict[str, Any]) -> str:
    """Why this exam blocks the archive: exam-service's reason; without one, by the status ("уже идёт" only for a running exam)."""
    return exam.get("blockerReason") or ("Экзамен уже идёт" if exam.get("status") == "active" else "У учеников уже есть попытки")


def _student_name(student: Student) -> str:
    last = (student.last_name or "").strip()
    first = (student.first_name or "").strip()
    return f"{last} {first[0]}." if last and first else (last or first or "Ученик")


class GroupLifecycle:
    def __init__(self, svc) -> None:
        self.svc = svc
        self.db = svc.db
        self.teachers = LessonTeachers(svc)

    # ------------------------------------------------------------------------------------------- reading
    def _group(self, org_id: Any, group_id: Any, *, lock: bool = False) -> Group:
        q = self.db.query(Group).filter(Group.id == group_id, Group.org_id == org_id)
        if lock:
            q = q.with_for_update().populate_existing()
        group = q.first()
        if group is None:
            raise not_found("GROUP_NOT_FOUND", ERROR_CATALOGUE["GROUP_NOT_FOUND"][1])
        return group

    def active_students(self, group: Group) -> List[Dict[str, Any]]:
        rows = (
            self.db.query(Student)
            .join(StudentGroup, StudentGroup.student_id == Student.id)
            .filter(StudentGroup.group_id == group.id, StudentGroup.is_active.is_(True), student_not_archived())
            .order_by(Student.last_name, Student.first_name, Student.id)
            .all()
        )
        return [{"id": str(s.id), "name": _student_name(s), "displayId": s.display_id} for s in rows]

    def _open_actions(self, group: Group):
        items = self.svc.get_open_attendance_actions(
            group.org_id, actor_id=None, is_org_admin=True, only_group_id=str(group.id), now=bc.business_now()
        )
        opened = [i for i in items if i.actionType == "in_progress"]
        missed = [i for i in items if i.actionType == "missed"]
        return opened, missed

    def future_lessons(self, group: Group) -> int:
        """Lessons that stop being generated: occurrences within the next 56 days whose planned start is not before now."""
        today = bc.business_today()
        now_utc = bc.utc_now()
        count = 0
        for occ in self.svc._resolved_occurrences_for_group(group, date_from=today, date_to=today + timedelta(days=FUTURE_HORIZON_DAYS - 1)):
            try:
                start = bc.lesson_instant_utc(date.fromisoformat(occ["occurrenceDate"]), hhmm_to_minutes(occ["startTime"]))
            except (KeyError, ValueError, AttributeError):
                continue
            if start >= now_utc:
                count += 1
        return count

    def pending_excuses(self, group: Group) -> int:
        return (
            self.db.query(func.count(AttendanceExcuse.id))
            .join(GroupAttendance, GroupAttendance.id == AttendanceExcuse.attendance_id)
            .filter(GroupAttendance.group_id == group.id, AttendanceExcuse.status == "pending")
            .scalar()
            or 0
        )

    def _names(self, ids) -> Dict[str, User]:
        return self.teachers.users_by_id(ids)

    def impact(self, org_id: Any, group_id: Any) -> Dict[str, Any]:
        group = self._group(org_id, group_id)
        if group.exam_release_pending:
            self.retry_pending_releases(org_id, group.id)
        students = self.active_students(group)
        if group.status != "active":
            return {
                "groupId": str(group.id), "groupName": group.name, "status": group.status, "canArchive": False,
                "examService": "ok", "blockers": [], "autoActions": [], "warnings": [], "legacyStudentsLeft": len(students),
            }
        opened, missed = self._open_actions(group)
        names = self._names([i.teacherId for i in [*opened, *missed] if i.teacherId])

        def teacher_name(item) -> Optional[str]:
            return short_name(names.get(item.teacherId)) if item.teacherId else None

        blockers: List[Dict[str, Any]] = []
        if students:
            blockers.append({"code": "ACTIVE_STUDENTS", "count": len(students), "students": students})
        if opened:
            blockers.append({"code": "OPEN_LESSON", "count": len(opened), "lessons": [
                {"lessonId": i.lessonId, "lessonDate": i.lessonDate, "teacherName": teacher_name(i)} for i in opened]})
        if missed:
            blockers.append({"code": "MISSED_LESSONS", "count": len(missed), "lessons": [
                {"lessonId": i.lessonId, "lessonDate": i.lessonDate, "startTime": i.startTime, "endTime": i.endTime,
                 "teacherName": teacher_name(i)} for i in missed]})
        exam_state = "ok"
        releasable: List[Dict[str, Any]] = []
        try:
            exams = exam_bridge.group_archive_impact(group.org_id, group.id)
        except (exam_bridge.ExamBridgeUnavailable, exam_bridge.ExamBridgeConflict):
            exam_state = "unavailable"
            exams = []
        live = [e for e in exams if not e.get("canRelease", True)]
        releasable = [e for e in exams if e.get("canRelease", True)]
        if live:
            blockers.append({"code": "LIVE_EXAMS", "count": len(live), "exams": [
                {"examId": str(e.get("examId")), "title": e.get("title") or "", "status": e.get("status"),
                 "lessonDate": e.get("lessonDate"), "blockerReason": _blocker_reason(e)} for e in live]})
        auto: List[Dict[str, Any]] = []
        if releasable:
            auto.append({"code": "RELEASE_EXAMS", "count": len(releasable)})
        future = self.future_lessons(group)
        if future:
            auto.append({"code": "STOP_FUTURE_LESSONS", "count": future})
        warnings: List[Dict[str, Any]] = []
        pending_requests = lesson_substitutions.count_pending_of_group(self.db, group.id)
        if pending_requests:
            warnings.append({"code": "PENDING_SUBSTITUTIONS", "count": pending_requests,
                             "message": f"Запросов на замену: {pending_requests} (будут закрыты)"})
        excuses = self.pending_excuses(group)
        if excuses:
            warnings.append({"code": "PENDING_EXCUSES", "count": excuses,
                             "message": f"Справок на решении: {excuses} (останутся у администратора)"})
        return {
            "groupId": str(group.id), "groupName": group.name, "status": group.status,
            "canArchive": not blockers and exam_state == "ok", "examService": exam_state,
            "blockers": blockers, "autoActions": auto, "warnings": warnings, "legacyStudentsLeft": 0,
        }

    # ------------------------------------------------------------------------------------------- archive
    def archive(self, org_id: Any, group_id: Any, req, *, actor_id: Any, actor_role: str) -> Dict[str, Any]:
        group = self._group(org_id, group_id, lock=True)  # the same lock a start / a membership change takes: archive vs start is decided here
        if group.status != "active":
            raise conflict("ALREADY_ARCHIVED", ERROR_CATALOGUE["ALREADY_ARCHIVED"][1])
        cancel_missed = req.missedLessonsAction == CANCEL
        students = self.active_students(group)
        opened, missed = self._open_actions(group)
        blockers: List[Dict[str, Any]] = []
        if students:
            blockers.append({"code": "ACTIVE_STUDENTS", "count": len(students), "students": students})
        if opened:
            names = self._names([i.teacherId for i in opened if i.teacherId])
            blockers.append({"code": "OPEN_LESSON", "count": len(opened), "lessons": [
                {"lessonId": i.lessonId, "lessonDate": i.lessonDate,
                 "teacherName": short_name(names.get(i.teacherId)) if i.teacherId else None} for i in opened]})
        if missed and not cancel_missed:
            names = self._names([i.teacherId for i in missed if i.teacherId])
            blockers.append({"code": "MISSED_LESSONS", "count": len(missed), "lessons": [
                {"lessonId": i.lessonId, "lessonDate": i.lessonDate, "startTime": i.startTime, "endTime": i.endTime,
                 "teacherName": short_name(names.get(i.teacherId)) if i.teacherId else None} for i in missed]})
        try:
            exams = exam_bridge.group_archive_impact(group.org_id, group.id)
        except (exam_bridge.ExamBridgeUnavailable, exam_bridge.ExamBridgeConflict) as exc:
            logger.warning("archive: exam-service unavailable: %s", exc)
            self.db.rollback()
            raise unavailable("EXAM_SERVICE_UNAVAILABLE", ERROR_CATALOGUE["EXAM_SERVICE_UNAVAILABLE"][1])
        live = [e for e in exams if not e.get("canRelease", True)]
        if live:
            blockers.append({"code": "LIVE_EXAMS", "count": len(live), "exams": [
                {"examId": str(e.get("examId")), "title": e.get("title") or "", "status": e.get("status"),
                 "lessonDate": e.get("lessonDate"), "blockerReason": _blocker_reason(e)} for e in live]})
        if blockers:
            self.db.rollback()
            raise conflict("GROUP_ARCHIVE_BLOCKED", ERROR_CATALOGUE["GROUP_ARCHIVE_BLOCKED"][1], blockers=blockers)

        now = bc.utc_now()
        reason = req.reason
        future = self.future_lessons(group)
        pending_requests = lesson_substitutions.count_pending_of_group(self.db, group.id)     # counted BEFORE the missed lessons are cancelled: that closes some of them
        cancelled = 0
        if cancel_missed:
            cancelled = self._cancel_missed(group, missed, reason, actor_id)
        lesson_substitutions.close_group_requests(self.svc, group, "GROUP_ARCHIVED", actor_id=actor_id, trigger="group_archived")
        closed_requests = pending_requests
        group.status = "archived"
        group.archive_reason = reason
        schedule_rules.open_interval(group, now)
        self.db.add(
            GroupAudit(
                org_id=group.org_id, group_id=group.id, actor_id=_uuid(actor_id), actor_role=actor_role, action="group_archived",
                assigned_teacher_id=group.teacher_id,
                snapshot={"reason": reason, "cancelledMissedLessons": cancelled, "stoppedFutureLessons": future, "closedSubstitutionRequests": closed_requests,
                          "releasedExamIds": [str(e.get("examId")) for e in exams if e.get("canRelease", True)]},
                created_at=now,
            )
        )
        releasable = [str(e.get("examId")) for e in exams if e.get("canRelease", True)]
        group.exam_release_pending = bool(exams)
        # The archive is committed FIRST: exam-service is asked afterwards.  A failure leaves a durable marker (``exam_release_pending``)
        # that the next archive-related call / the sweep retries; the exams of an archived group are never left half-released silently.
        self.db.commit()
        released, pending = self._release(group, actor_id, reason)
        response = self.svc._build_group_response(group).model_dump()
        response.update({"releasedExamIds": released if not pending else [], "cancelledMissedLessons": cancelled,
                         "examReleasePending": pending, "closedSubstitutionRequests": closed_requests})
        return response

    # ------------------------------------------------------------------------------------ exam release (idempotent)
    def _release(self, group: Group, actor_id: Any, reason: Optional[str]) -> Tuple[List[str], bool]:
        """Ask exam-service to release the group's exams; clears the marker on success.  Returns ``(released ids, still pending)``."""
        try:
            released = exam_bridge.group_archive_release(group.org_id, group.id, actor_id, reason)
        except (exam_bridge.ExamBridgeConflict, exam_bridge.ExamBridgeUnavailable) as exc:
            logger.warning("archive: exam release of group %s is pending: %s", group.id, exc)
            return [], True
        group.exam_release_pending = False
        self.db.commit()
        return released, False

    def retry_pending_releases(self, org_id: Any = None, group_id: Any = None) -> int:
        """Retry the release of every ARCHIVED group with the marker (one group, one centre, or all).  Returns how many were cleared."""
        q = self.db.query(Group).filter(Group.exam_release_pending.is_(True), Group.status == "archived")
        if org_id is not None:
            q = q.filter(Group.org_id == org_id)
        if group_id is not None:
            q = q.filter(Group.id == group_id)
        cleared = 0
        for group in q.all():
            _released, pending = self._release(group, None, group.archive_reason)
            cleared += 0 if pending else 1
        return cleared

    def _cancel_missed(self, group: Group, missed, reason: Optional[str], actor_id: Any) -> int:
        """The explicit decision of the administrator: every missed lesson becomes a cancelled row (teacher by the resolver).  W1 R3 10: through THE cancellation (``cancel_lesson``,
        ``archive=True``): the same row and audit as before, the kind it always had (none), and the substitute assigned to the lesson is released."""
        from .lesson_substitutions import Substitutions  # lazy: it imports this module's neighbours

        text = f"Группа закрыта: {reason}" if reason else "Группа закрыта"
        subs = Substitutions(self.svc)
        count = 0
        for item in missed:
            day = date.fromisoformat(item.lessonDate)
            occurrence = next((o for o in self.svc._resolved_occurrences_for_group(group, date_from=day, date_to=day)
                               if o["id"] == item.lessonId), None)
            if occurrence is None:
                continue
            subs.cancel_lesson(group.org_id, actor_id, group.id, item.lessonId, day, reason=text, kind=None, archive=True, archived_ok=True)
            count += 1
        return count

    # -------------------------------------------------------------------------------------------- restore
    def restore(self, org_id: Any, group_id: Any, req, *, actor_id: Any, actor_role: str) -> Dict[str, Any]:
        group = self._group(org_id, group_id, lock=True)
        if group.status != "archived":
            raise conflict("NOT_ARCHIVED", ERROR_CATALOGUE["NOT_ARCHIVED"][1])
        provided = req is not None and "teacherId" in req.model_fields_set
        old_teacher = group.teacher_id
        new_teacher = old_teacher
        if provided:
            new_teacher = _uuid(req.teacherId)
            if req.teacherId is not None:
                if new_teacher is None or self.teachers.teaching_member(group.org_id, new_teacher) is None or not self._is_group_teacher(group.org_id, new_teacher):
                    raise OpsError(422, "TARGET_NOT_TEACHER", ERROR_CATALOGUE["TARGET_NOT_TEACHER"][1])
        elif old_teacher is not None and not self._is_group_teacher(group.org_id, old_teacher):
            user = self.db.query(User).filter(User.id == old_teacher).first()
            raise conflict("TEACHER_INACTIVE", ERROR_CATALOGUE["TEACHER_INACTIVE"][1], teacherName=short_name(user))
        now = bc.utc_now()
        group.exam_release_pending = False  # the group is back: a release that was still pending is moot
        archived_from = schedule_rules.archived_at(group)
        group.status = "active"
        group.archive_reason = None
        schedule_rules.close_open_interval(group, now, fallback_from=archived_from)
        self.db.flush()
        if new_teacher is not None:
            # the teacher (new or the same one) must be free for the lessons that come back to life: he may have got another lesson while the group slept
            try:
                self.teachers.check_new_group_teacher(group.org_id, {group.id: (group, new_teacher)}, include_unchanged=True)
            except OpsError:
                self.db.rollback()
                raise
        try:
            self.teachers.check_substitutes_of_group(group.org_id, group)          # W1 S23: the substitutes that come back with the lessons are free then too
        except OpsError:
            self.db.rollback()
            raise
        group.teacher_id = new_teacher
        if new_teacher is not None and new_teacher != old_teacher:
            self.db.add(
                GroupAudit(
                    org_id=group.org_id, group_id=group.id, actor_id=_uuid(actor_id), actor_role=actor_role, action="teacher_changed",
                    assigned_teacher_id=new_teacher, snapshot={"teacher_id": [_s(old_teacher), _s(new_teacher)], "via": "restore"},
                    created_at=now,
                )
            )
        elif provided and new_teacher is None and old_teacher is not None:
            self.db.add(
                GroupAudit(
                    org_id=group.org_id, group_id=group.id, actor_id=_uuid(actor_id), actor_role=actor_role, action="teacher_changed",
                    assigned_teacher_id=None, snapshot={"teacher_id": [_s(old_teacher), None], "via": "restore"}, created_at=now,
                )
            )
        self.db.add(
            GroupAudit(
                org_id=group.org_id, group_id=group.id, actor_id=_uuid(actor_id), actor_role=actor_role, action="group_restored",
                assigned_teacher_id=group.teacher_id, snapshot={"teacher_id": _s(group.teacher_id)}, created_at=now,
            )
        )
        self.db.commit()
        from .days_off import DaysOffService  # lazy: it imports the service layer

        warnings = DaysOffService(self.svc).apply_to_group(group.org_id, actor_id, group.id)      # W1 S10 (Д7) + R3 11: the group meets the live days off; a failure is a warning + ``incomplete``, never silent
        out = self.svc._build_group_response(group).model_dump()
        out["warnings"] = warnings
        return out

    def _is_group_teacher(self, org_id: Any, user_id: Any) -> bool:
        from .models import OrganizationMember

        return (
            self.db.query(OrganizationMember.id)
            .join(User, User.id == OrganizationMember.user_id)
            .filter(
                OrganizationMember.org_id == org_id, OrganizationMember.user_id == user_id, OrganizationMember.org_role.in_(TEACHING_ROLES),
                OrganizationMember.is_active.is_(True), User.is_active.isnot(False),
            )
            .first()
            is not None
        )
