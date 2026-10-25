"""Rules of the lesson journal that the operations package adds (LOGIC-B O5 / O6 / O7 / O8 and decision 7).

Used by the hooks in ``AuthService`` (start / save / reconcile / cancel-missed / open actions / the journal read).  Nothing here commits.

* lesson rights (O5): a teacher acts only on HIS lessons - the teacher frozen in the row, else the lesson-teacher resolver; owner / admin on any;
* decision 7: a teacher closes lessons with ``lesson_date >= today - 7`` (``LESSON_TOO_OLD`` 403); nobody (admin included) reconciles /
  cancels a never-started lesson older than the "unclosed lessons" window (first day of the previous month: ``LESSON_OUTSIDE_WINDOW`` 422);
* O7: ``expectedVersion`` against ``group_attendance.version``; an identical repeat is a 200 without a write (``ATTENDANCE_CONFLICT`` 409);
* O8: ending a lesson asks exam-service for live exams (``LESSON_HAS_LIVE_EXAM`` 409 until ``acknowledgeLiveExam``), fail-open.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from . import business_clock as bc
from . import exam_bridge
from .lesson_teachers import LessonTeachers, TeacherBook, effective_teacher
from .models import Group, GroupAttendance, GroupAttendanceAudit, User
from .ops_rules import (
    conflict,
    forbidden,
    hhmm_to_minutes,
    is_too_old_for_teacher,
    short_name,
    unprocessable,
)

logger = logging.getLogger(__name__)

COMPLETED = "completed"
IN_PROGRESS = "in_progress"
CANCELLED = "cancelled"
EXAM_UNAVAILABLE = "EXAM_SERVICE_UNAVAILABLE"


class AttendanceRules:
    def __init__(self, svc) -> None:
        self.svc = svc
        self.db = svc.db
        self.teachers = LessonTeachers(svc)

    # ------------------------------------------------------------------------------------------- rights
    def resolved_teacher(self, group: Group, lesson_id: str, lesson_date: str, occurrence: Optional[Dict[str, Any]],
                         row: Optional[GroupAttendance] = None) -> Optional[UUID]:
        end = hhmm_to_minutes(occurrence["endTime"]) if occurrence and occurrence.get("endTime") else None
        res = TeacherBook(self.db).resolve(group, lesson_id, lesson_date, end)
        return effective_teacher(row, res)

    def assert_lesson_rights(self, group: Group, row: Optional[GroupAttendance], occurrence: Optional[Dict[str, Any]], lesson_date: date,
                             lesson_id: str, *, actor_id: Any, is_org_admin: bool, check_age: bool = True) -> Optional[UUID]:
        """Who may act on this lesson (O5) and is it too old for him (decision 7).  Returns the lesson teacher."""
        teacher = self.resolved_teacher(group, lesson_id, lesson_date.isoformat(), occurrence, row)
        if is_org_admin:
            return teacher
        if teacher is None or str(teacher) != str(actor_id):
            names = self.teachers.users_by_id([teacher]) if teacher else {}
            who = short_name(names.get(str(teacher))) if teacher else None
            raise forbidden("NOT_LESSON_TEACHER", (f"Этот урок ведёт {who}" + ("" if who.endswith(".") else ".")) if who else "Этот урок ведёт другой преподаватель.")
        if check_age and is_too_old_for_teacher(lesson_date, bc.business_today()):
            raise forbidden("LESSON_TOO_OLD", "Урок старше 7 дней — его закрывает администратор или владелец")
        return teacher

    @staticmethod
    def assert_unclosed_window(lesson_date: date) -> None:
        """reconcile / cancel-missed: not before the first day of the previous month, for everybody."""
        if lesson_date < bc.month_window_start(bc.business_today()):
            raise unprocessable("LESSON_OUTSIDE_WINDOW", "Урок старше окна незакрытых уроков: разобрать его нельзя")

    # ----------------------------------------------------------------------------------- O7: version / no-op
    def _last_writer(self, row: GroupAttendance) -> Tuple[Optional[str], Optional[str]]:
        audit = (
            self.db.query(GroupAttendanceAudit)
            .filter(GroupAttendanceAudit.attendance_id == row.id, GroupAttendanceAudit.action != "excuse_reviewed")
            .order_by(GroupAttendanceAudit.created_at.desc(), GroupAttendanceAudit.id.desc())
            .first()
        )
        actor_id = audit.actor_id if audit else row.marked_by
        when = audit.created_at if audit else row.updated_at
        return (str(actor_id) if actor_id else None, when.isoformat() if when else None)

    def conflict_error(self, row: GroupAttendance):
        actor_id, when = self._last_writer(row)
        user = self.db.query(User).filter(User.id == actor_id).first() if actor_id else None
        name = short_name(user)
        hhmm = None
        if when:
            from datetime import datetime

            hhmm = bc.utc_to_business_wall(datetime.fromisoformat(when)).strftime("%H:%M")
        return conflict(
            "ATTENDANCE_CONFLICT",
            f"Отметки уже изменил(а) {name or 'другой сотрудник'}" + (f" в {hhmm}" if hhmm else ""),
            currentVersion=int(row.version or 1),
            updatedBy={"id": actor_id, "name": name},
            updatedAt=when,
            records={str(k): v for k, v in (row.records or {}).items()},
        )

    def version_gate(self, row: Optional[GroupAttendance], req) -> bool:
        """``True`` when the request changes nothing (an identical repeat: answered 200 without a write, also with a stale
        ``expectedVersion``); raises ``ATTENDANCE_CONFLICT`` when the version is stale and the request would change something."""
        if row is None:
            return False
        records = {str(k): v.value for k, v in req.records.items()}
        same = (
            req.status.value == row.status
            and records == dict(row.records or {})
            and (req.topic is None or req.topic == row.topic)
            and (req.status.value != CANCELLED or (req.cancellationReason or None) == (row.cancellation_reason or None))
        )
        if same:
            # a repeat that still has something to do for the excuses (a reason changed, a legacy "excused" mark without its row)
            # is a real save
            from .attendance_excuses import AttendanceExcuseService

            plan = AttendanceExcuseService(self.db).plan_save(row, records, req.excuseReasons)
            if not (plan.create or plan.update or plan.delete):
                return True
        expected = getattr(req, "expectedVersion", None)
        if expected is None:
            logger.info("attendance_save_without_version row=%s", row.id)
        elif int(expected) != int(row.version or 1):
            raise self.conflict_error(row)
        return False

    # ------------------------------------------------------------------------------------ O8: live exams
    def live_exam_gate(self, org_id: Any, group_id: Any, lesson_id: str, lesson_date: str,
                       acknowledged: bool) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        """Before an in-progress lesson is completed.  Returns ``(warning, exams)``: the warning is set when exam-service could not be asked
        (fail-open); raises ``LESSON_HAS_LIVE_EXAM`` when an exam is attached and the caller has not acknowledged it."""
        try:
            exams = exam_bridge.completion_check(org_id, group_id, lesson_id, lesson_date)
        except (exam_bridge.ExamBridgeUnavailable, exam_bridge.ExamBridgeConflict) as exc:
            logger.warning("exam completion-check unavailable: %s", exc)
            return EXAM_UNAVAILABLE, []
        if exams and not acknowledged:
            first = exams[0]
            if first.get("status") == "active":
                remaining = first.get("remainingSeconds")
                minutes = max(int((remaining or 0) // 60) + (1 if (remaining or 0) % 60 else 0), 1)
                message = (
                    f"Экзамен «{first.get('title')}» ещё идёт (осталось {minutes} мин). "
                    "Он продолжится по своему таймеру и закроется сам. Завершить урок?"
                )
            else:
                waiting = int(first.get("waitingCount") or 0)
                tail = f"; ожидают учеников: {waiting}" if waiting else ""
                message = (
                    f"Экзамен «{first.get('title')}» не запущен{tail}. "
                    "После завершения урока он вернётся в черновик. Завершить урок?"
                )
            raise conflict(
                "LESSON_HAS_LIVE_EXAM",
                message,
                exams=[
                    {
                        "examId": str(e.get("examId")),
                        "title": e.get("title") or "",
                        "status": e.get("status"),
                        "remainingSeconds": e.get("remainingSeconds"),
                        "waitingCount": int(e.get("waitingCount") or 0),
                    }
                    for e in exams
                ],
            )
        return None, exams

    def after_completion(self, org_id: Any, group_id: Any, lesson_id: str, lesson_date: str, actor_id: Any,
                         warning: Optional[str], exams: List[Dict[str, Any]]) -> Dict[str, Any]:
        """After the local commit: free the untouched lobby exams, leave the running ones.  Best effort (warning on failure)."""
        sync: Dict[str, Any] = {"released": [], "leftRunning": [], "warning": warning}
        if warning or not exams:
            return sync
        try:
            result = exam_bridge.lesson_completed(org_id, group_id, lesson_id, lesson_date, actor_id)
            sync["released"], sync["leftRunning"] = result["released"], result["leftRunning"]
        except (exam_bridge.ExamBridgeUnavailable, exam_bridge.ExamBridgeConflict) as exc:
            logger.warning("exam lesson-completed unavailable: %s", exc)
            sync["warning"] = EXAM_UNAVAILABLE
        return sync

    # ------------------------------------------------------------------------------ journal read: extras
    def journal_extras(self, group: Group, rows: List[GroupAttendance], lessons: List[Dict[str, Any]],
                       period_start: date, period_end: date) -> Tuple[Dict[str, int], Dict[str, Dict[str, Any]]]:
        """``versions`` and ``lessonTeachers`` of the journal, keyed ``<date>_<lessonId>``."""
        book = TeacherBook(self.db)
        book.load_history([group.id])
        book.load_assignments([group.id], period_start.isoformat(), period_end.isoformat())
        by_key = {f"{r.lesson_date}_{r.lesson_id or 'lesson_' + r.lesson_date}": r for r in rows}
        versions = {key: int(r.version or 1) for key, r in by_key.items()}
        infos: Dict[str, Dict[str, Any]] = {}
        user_ids: List[Any] = []
        for lesson in lessons:
            key = lesson.get("instanceKey")
            if not key:
                continue
            end = hhmm_to_minutes(lesson["endTime"]) if lesson.get("endTime") else None
            res = book.resolve(group, lesson["id"], lesson["date"], end)
            teacher = effective_teacher(by_key.get(key), res)
            infos[key] = {"teacher": teacher, "default": res.default_teacher_id}
            user_ids += [teacher, res.default_teacher_id]
        names = self.teachers.users_by_id(user_ids)
        out: Dict[str, Dict[str, Any]] = {}
        for key, item in infos.items():
            teacher, default = item["teacher"], item["default"]
            out[key] = {
                "teacherId": str(teacher) if teacher else None,
                "teacherName": short_name(names.get(str(teacher))) if teacher else None,
                "isSubstitution": bool(teacher is not None and teacher != default),
                "defaultTeacherId": str(default) if default else None,
                "defaultTeacherName": short_name(names.get(str(default))) if default else None,
            }
        return versions, out

    # ------------------------------------------------------------------------------------ restricted journal (LOGIC-B2, R3-5)
    def restrict_journal(self, group: Group, resp, actor_id: Any):
        """The journal as a teacher who does NOT lead the group sees it: only the lessons that are HIS (the teacher of the lesson by the resolver / the row, from
        ``today - 7``) - a substitute's lesson, or the lessons a former teacher taught before a handover - and only their marks.  No other lessons or months, no phones, no
        diagnostics, no transfer events; the students of those lessons come as ``lessonRoster`` (name, display id) because the group page is closed to him.
        The current teacher of the group (and the managers, who never reach this method) get the full journal."""
        actor = str(actor_id)
        if group.teacher_id is not None and str(group.teacher_id) == actor:
            return resp
        window_from = bc.business_today()
        from .ops_rules import teacher_lesson_window_start

        window_from = teacher_lesson_window_start(window_from).isoformat()
        allowed = {
            key for key, info in (resp.lessonTeachers or {}).items()
            if (info.get("teacherId") if isinstance(info, dict) else getattr(info, "teacherId", None)) == actor and key.split("_", 1)[0] >= window_from
        }
        by_key = {lesson.get("instanceKey"): lesson for lesson in resp.lessons}
        roster_ids: Dict[str, List[str]] = {}
        extra: Dict[str, Dict[str, Any]] = {}
        for key in sorted(allowed):
            lesson = by_key.get(key)
            if lesson is None:
                continue
            if key in resp.rosterStudentIds:
                roster_ids[key] = list(resp.rosterStudentIds[key])
            else:
                start = hhmm_to_minutes(lesson["startTime"]) if lesson.get("startTime") else 0
                snapshot = self.svc._attendance_roster_at(group.id, bc.lesson_instant_utc(date.fromisoformat(lesson["date"]), start))
                roster_ids[key] = [r["student_id"] for r in snapshot]
                for r in snapshot:
                    extra[r["student_id"]] = r
        ids = {i for lst in roster_ids.values() for i in lst}
        from .models import Student

        valid = []
        for i in ids:
            try:
                valid.append(UUID(i))
            except (TypeError, ValueError):
                continue
        students = {str(s.id): s for s in self.db.query(Student).filter(Student.id.in_(valid)).all()} if valid else {}
        roster = []
        for sid in sorted(ids, key=lambda i: ((students[i].last_name or "", students[i].first_name or "") if i in students else ("", ""), i)):
            s = students.get(sid)
            if s is None:
                continue
            roster.append({"id": sid, "first_name": s.first_name, "last_name": s.last_name,
                           "full_name": f"{s.last_name or ''} {s.first_name or ''}".strip() or "Ученик", "display_id": s.display_id, "class_level": s.class_level})

        def only(mapping):
            return {k: v for k, v in (mapping or {}).items() if k in allowed}

        resp.lessons = [lesson for lesson in resp.lessons if lesson.get("instanceKey") in allowed]
        resp.records, resp.statuses, resp.cancellationReasons = only(resp.records), only(resp.statuses), only(resp.cancellationReasons)
        resp.cancellationKinds = only(resp.cancellationKinds)
        resp.rosterStudentIds = {k: v for k, v in roster_ids.items() if k in resp.rosterStudentIds or True}
        resp.excuses, resp.corrections = only(resp.excuses), only(resp.corrections)
        resp.versions, resp.lessonTeachers = only(resp.versions), only(resp.lessonTeachers)
        resp.historicalStudents = [{k: h.get(k) for k in ("id", "first_name", "last_name", "full_name", "display_id")}      # name + display id only: no periods, no transfers
                                   for h in resp.historicalStudents if h.get("id") in ids]
        resp.membershipEvents, resp.diagnosticReportNotices = [], []
        resp.lessonRoster = roster
        return resp
