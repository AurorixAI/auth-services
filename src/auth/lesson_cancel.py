"""W1 (27.10): THE cancellation of one lesson - one road for the lesson sheet, «Урока не было», the decision of a request, the bulk decision and a day off - and its undo.

Before this module four roads cancelled a lesson with four different traces (SPEC W1 S2).  Now every one of them ends in ``cancel_lesson``:

* the rules of the row: a lesson that is already cancelled / moved is refused (``ALREADY_CANCELLED`` / ``ALREADY_MOVED``, never a silent overwrite of the reason), a started
  lesson with marks or excuses is refused (``LESSON_HAS_MARKS``), the group is locked first (the same lock as the move: two cancels / a move and a cancel serialise,
  a lost race is a 409 ``LESSON_CHANGED``, never a 500);
* the kind: a lesson on a date that a LIVE day off of the centre holds is cancelled as ``holiday`` with ``day_off_id`` whoever cancels it and whatever the author chose (the
  day is a holiday: no deduction from the next payment); the answer carries the warning ``CANCELLED_AS_HOLIDAY``;
* the trace: the card of the student gets the note (a ``cancelled`` record of the journal «Замены», like a move: origin ``system``, the trigger says who - ``manager_cancel`` /
  ``teacher_cancel``), a request of a teacher that is waiting is decided by this very cancellation, a substitute set on the lesson is taken off (``assignment_cleared``,
  trigger ``cancel``).  A lesson cancelled by a DAY OFF has no record of its own: the day off is its journal and the note of the student (and «Отменить выходной» gives it back);
* the exams: checked before anything is written, released before the commit (the saga of ``AuthService._sync_exam_lesson_cancellation``).

``restore_lesson`` is the undo (W1 S5): a future lesson that was cancelled comes back (the cancelled row is deleted - what it was is recorded first), the deduction of the
recalculation disappears by itself because it is a function of the cancelled rows.

The mixin lives in ``Substitutions`` (``lesson_substitutions.py``): it uses its locks, its exam saga, its system records.  Nothing here commits - the caller does."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from src.models.auth import GroupAudit

from . import business_clock as bc
from .days_off import DaysOffService, HOLIDAY
from .lesson_teachers import TeacherBook, _uuid
from .models import AttendanceExcuse, Group, GroupAttendance, GroupAttendanceAudit, LessonSubstitutionRequest as Req, LessonTeacherAssignment, User
from .one_off_purpose import MAKEUP_EXISTS_TEXT, OneOffPurpose
from .ops_rules import OpsError, conflict, group_archived, hhmm_to_minutes, not_found, short_name
from .schemas import DEFAULT_CANCELLATION_KIND

MANAGER_CANCEL = "manager_cancel"                # ``trigger`` of the record a cancellation by an owner / admin made
TEACHER_CANCEL = "teacher_cancel"                # ... by a teacher («Урока не было» of a lesson that was never started)
MANAGER_RESTORE = "manager_restore"              # ... a lesson given back
RESTORED = "LESSON_RESTORED"                     # ``closed_reason`` of the record of a restore
KIND_CHANGED = "KIND_CHANGED"                    # ... of the record of a change of the kind of a cancellation (S17)
MANAGER_REASON_CHANGE = "manager_reason_change"        # ``trigger`` of the event
REASON_CHANGE_VIA = "reason_change"                      # ``decided_via`` of the record (the column is 20 characters)
EXAMS_NOT_RESTORED = "Экзамены, снятые с урока при его отмене, не вернутся — назначьте их заново"


def _dm(day: date) -> str:
    return day.strftime("%d.%m")


class LessonCancelMixin:
    """``cancel_lesson`` / ``restore_lesson`` of ``Substitutions``."""

    # ------------------------------------------------------------------------------------------------ cancel
    def cancel_lesson(self, org_id: Any, actor_id: Any, group_id: Any, lesson_id: str, day: date, *, reason: str, kind: str = DEFAULT_CANCELLATION_KIND,
                      by_manager: bool = True, allow_started: bool = False, day_off_id: Any = None, request: Optional[Req] = None, via: str = "request",
                      archived_ok: bool = False, archive: bool = False) -> Dict[str, Any]:
        """Cancel the lesson of ``group_id`` on ``day``.  ``kind``: what the author chose (``centre`` | ``holiday``); a date of a live day off turns it into ``holiday``.
        ``archive``: the cancellation of a missed lesson by the ARCHIVING of the group (W1 R3 10): the same road (row, audit, substitute released) with the kind it always had (none: neutral for the
        money), no record of «Замены», no exam call (the archive releases the exams of the group itself).  ``archived_ok``: the missed lessons of an archived group are resolved (the group refuses every new row otherwise).  ``allow_started``: a lesson that was started (an administrator's cancellation from the lesson sheet) may be cancelled when nothing is marked on it.  ``day_off_id``: the day
        off that cancels (its own lessons only: never a started one).  ``request``: the request that this cancellation decides (the decision of the «Замены» page); without one a
        request of the lesson that is waiting is decided by it, and without any a ``cancelled`` record is made.  Caller commits; any refusal rolls everything back.
        Returns ``{"version", "kind", "dayOffId", "warnings", "request"}``."""
        try:
            return self._cancel_locked(org_id, _uuid(actor_id), group_id, lesson_id, day, reason=reason, kind=kind, by_manager=by_manager, allow_started=allow_started,
                                       day_off_id=day_off_id, request=request, via=via, archived_ok=archived_ok, archive=archive)
        except BaseException:
            self.db.rollback()
            raise

    def _cancel_locked(self, org_id: Any, actor: Optional[Any], group_id: Any, lesson_id: str, day: date, *, reason: str, kind: str, by_manager: bool, allow_started: bool,
                       day_off_id: Any, request: Optional[Req], via: str, archived_ok: bool, archive: bool) -> Dict[str, Any]:
        iso = day.isoformat()
        by_day_off = day_off_id is not None             # a DAY OFF cancels this lesson (not a hand cancellation on a date a day off holds): the substitute it takes off is marked with it (#157 A4)
        group = self._group_locked(org_id, _uuid(group_id))
        if group is None:
            raise not_found("GROUP_NOT_FOUND", "Группа не найдена")
        row = (self.db.query(GroupAttendance).filter(GroupAttendance.group_id == group.id, GroupAttendance.lesson_id == lesson_id, GroupAttendance.lesson_date == iso)
               .with_for_update().first())
        if row is None and group.status != "active" and not archived_ok:
            raise group_archived()
        self._refuse_cancel(group, row, day, allow_started=allow_started, day_off_id=day_off_id)
        ref, occ = self._lesson_ref(group, lesson_id, day)
        warnings: List[Dict[str, str]] = []
        kind, day_off_id, converted = (None, None, None) if archive else self._kind_for(org_id, day, kind, day_off_id)
        if converted is not None:
            warnings.append({"code": "CANCELLED_AS_HOLIDAY", "message": f"{_dm(day)} — выходной день центра ({converted}): урок отменён как «выходной», вычета у учеников не будет"})
        record = None if archive else self._record_of(group, ref, request, reason, MANAGER_CANCEL if by_manager else TEACHER_CANCEL, actor, journal=day_off_id is None)
        if request is None and day_off_id is not None:       # a request that waits for a lesson of a DAY OFF is closed by it, with the day off in the event: «Отменить выходной» opens it again (Д28)
            waiting = next((r for r in self._lesson_requests(group, lesson_id, iso) if r.status == "pending"), None)
            if waiting is not None:
                self.close(self.db.query(Req).filter(Req.id == waiting.id).with_for_update().populate_existing().first(), "LESSON_CANCELLED", actor_id=actor,
                           trigger="day_off", group=group, extra={"dayOffId": str(day_off_id)})
            record = None
        if not archive:
            self._exam_check(ref, reason)
        if row is None:
            row = self._cancelled_row(group, ref, actor, reason, {"cancellation_kind": kind}, kind=kind, day_off_id=day_off_id, request=request)
        else:
            self._cancel_started_row(group, ref, row, actor, reason, kind)
        OneOffPurpose(self.svc).release(group, lesson_id, actor)          # #154: a cancelled make-up frees the lesson it made up for (the deduction comes back by itself)
        released = self._release_substitute(group, ref, actor, "cancel", {"dayOffId": str(day_off_id)} if by_day_off else None)
        if released:
            warnings.append({"code": "SUBSTITUTE_RELEASED", "message": f"Замена снята: {released}"})
        if not archive:
            self._exam_release(ref, reason)
        if record is None and day_off_id is None and not archive:        # a group without a teacher: no record of «Замены» (nobody to attribute it to) - the author stays in the history of the group
            self.db.add(GroupAudit(org_id=group.org_id, group_id=group.id, actor_id=actor, actor_role="admin" if by_manager else "teacher", action="lesson_cancelled",
                                   assigned_teacher_id=None, created_at=bc.utc_now(),
                                   snapshot={"lessonId": lesson_id, "lessonDate": iso, "cancellationKind": kind, "reason": reason, "version": row.version}))
        if record is not None:
            self._finish(record, "cancelled", actor, reason, via)
            self.event(record, "cancelled", actor, {"reason": reason, "version": row.version, "cancellationKind": kind})
        return {"version": row.version, "kind": kind, "dayOffId": day_off_id, "warnings": warnings, "request": record}

    def _refuse_cancel(self, group: Group, row: Optional[GroupAttendance], day: date, *, allow_started: bool, day_off_id: Any) -> None:
        """What the journal row of the lesson says against a cancellation (the one place of these refusals)."""
        if row is None:
            return
        if row.status == "cancelled":
            moved = (self.db.query(Req).filter(Req.group_id == group.id, Req.lesson_id == row.lesson_id, Req.lesson_date == row.lesson_date, Req.status == "rescheduled")
                     .order_by(Req.created_at.desc()).first())
            if moved is not None:
                where = f" на {_dm(date.fromisoformat(moved.moved_to_date))}" if moved.moved_to_date else ""
                raise conflict("ALREADY_MOVED", f"Урок уже перенесён{where}: отменить его нельзя. Отмените урок на новой дате")
            raise conflict("ALREADY_CANCELLED", "Урок уже отменён")
        if row.status == "completed" or not allow_started or day_off_id is not None:
            raise conflict("LESSON_ALREADY_STARTED", "Урок уже начат или проведён: отменить его нельзя" if row.status != "completed" else "Урок уже проведён: отменить его нельзя")
        if row.records or self.db.query(AttendanceExcuse.id).filter(AttendanceExcuse.attendance_id == row.id).first() is not None:
            raise conflict("LESSON_HAS_MARKS", "У урока есть отметки посещаемости или справки. Сначала снимите отметки")

    def _kind_for(self, org_id: Any, day: date, kind: str, day_off_id: Any):
        """``(kind, day_off_id, reason of the day off when the author's kind was changed)``: the day off that holds the date wins over what the author chose."""
        if day_off_id is not None:
            return HOLIDAY, day_off_id, None
        live = DaysOffService(self.svc).live_for_date(org_id, day)
        if live is None:
            return kind, None, None
        return HOLIDAY, live.id, (live.reason if kind != HOLIDAY else None)

    def _record_of(self, group: Group, ref: Any, request: Optional[Req], reason: str, trigger: str, actor: Any, *, journal: bool) -> Optional[Req]:
        """The record of the journal that this cancellation decides: the request it was asked for, a request of the lesson that is waiting, else a new system record of the
        main teacher of the lesson - none when the group has no teacher (nobody to attribute it to) or ``journal`` is false (a DAY OFF is the journal of its own lessons and the
        note of the student; a request that waits for such a lesson is closed by the day off itself, ``_cancel_locked``)."""
        if request is not None:
            return request
        reqs = self._lesson_requests(group, ref.lesson_id, ref.lesson_date)
        pending = next((r for r in reqs if r.status == "pending"), None)
        if pending is not None and journal:
            return self.db.query(Req).filter(Req.id == pending.id).with_for_update().populate_existing().first()
        main = TeacherBook(self.db).default_teacher(group, ref.lesson_date, hhmm_to_minutes(ref.end_time))
        if main is None or not journal:
            return None
        return self._system_request(group, ref, main, None, trigger, actor, reqs[0] if reqs else None)

    def _cancel_started_row(self, group: Group, ref: Any, row: GroupAttendance, actor: Any, reason: str, kind: str) -> None:
        """A lesson that was started and has no marks: the row becomes ``cancelled`` (the lesson sheet of an administrator)."""
        before = row.status
        reconciled = (self.db.query(GroupAttendanceAudit.id).filter(GroupAttendanceAudit.attendance_id == row.id, GroupAttendanceAudit.action == "reconciliation_started").first()
                      is not None)
        row.status, row.records, row.completed_at = "cancelled", {}, None
        row.cancellation_reason, row.cancellation_kind, row.marked_by = reason, kind, actor
        row.version = int(row.version or 1) + 1
        self.db.add(GroupAttendanceAudit(attendance_id=row.id, org_id=group.org_id, group_id=group.id, actor_id=actor, action="reconciliation_cancelled" if reconciled else "cancelled",
                                         before_status=before, after_status="cancelled", before_records={}, after_records={}, cancellation_reason=reason,
                                         details={"cancellation_kind": kind}))
        self.db.flush()

    def _release_substitute(self, group: Group, ref: Any, actor: Any, trigger: str, extra: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """The lesson is gone (cancelled / moved): the substitute assigned to it is released (no «Замена» for a lesson that will not happen).  Returns his name."""
        asg = (self.db.query(LessonTeacherAssignment).filter(LessonTeacherAssignment.group_id == group.id, LessonTeacherAssignment.lesson_id == ref.lesson_id,
                                                              LessonTeacherAssignment.lesson_date == ref.lesson_date, LessonTeacherAssignment.cancelled_at.is_(None)).first())
        if asg is None:
            return None
        who = short_name(self.db.query(User).filter(User.id == asg.teacher_id).first())
        self.teachers.cancel_assignment(asg, actor, group=group, reason="Урок отменён или перенесён: замена снята", trigger=trigger, extra=extra)
        return who or "преподаватель"

    # ------------------------------------------------------------------------------------------------ restore
    def restore_lesson(self, org_id: Any, actor_id: Any, actor_role: str, group_id: Any, lesson_id: str, day: date, reason: str) -> Dict[str, Any]:
        """W1 S5: «Вернуть отменённый урок» (owner / admin, a lesson that has not started, with a reason).  Refused: a lesson that is not cancelled, a MOVED original (cancel the
        copy instead), a lesson that has started, a date that a live day off holds (cancel the day off), a lesson the schedule no longer has, a teacher who is busy meanwhile.
        What was cancelled is recorded (the group audit + the record of the journal «Замены») BEFORE the row is deleted; the deduction of the recalculation is gone with the row."""
        actor = _uuid(actor_id)
        try:
            out = self._restore_locked(org_id, actor, actor_role, group_id, lesson_id, day, reason)
            self.db.commit()
            return out
        except BaseException:
            self.db.rollback()
            raise

    def _restore_locked(self, org_id: Any, actor: Any, actor_role: str, group_id: Any, lesson_id: str, day: date, reason: str) -> Dict[str, Any]:
        iso = day.isoformat()
        group = self._group_locked(org_id, _uuid(group_id))
        if group is None:
            raise not_found("GROUP_NOT_FOUND", "Группа не найдена")
        if group.status != "active":
            raise group_archived()
        row = (self.db.query(GroupAttendance).filter(GroupAttendance.group_id == group.id, GroupAttendance.lesson_id == lesson_id, GroupAttendance.lesson_date == iso)
               .with_for_update().first())
        if row is None or row.status != "cancelled":
            raise conflict("RESTORE_NOT_CANCELLED", "Урок не отменён: возвращать нечего")
        if row.made_up_by:                                                            # #154: a make-up is appointed - the lesson would be held twice
            raise conflict("MAKEUP_EXISTS", MAKEUP_EXISTS_TEXT)
        moved = (self.db.query(Req).filter(Req.group_id == group.id, Req.lesson_id == lesson_id, Req.lesson_date == iso, Req.status == "rescheduled")
                 .order_by(Req.created_at.desc()).first())
        if moved is not None:
            where = f" на {_dm(date.fromisoformat(moved.moved_to_date))}" if moved.moved_to_date else ""
            raise conflict("RESTORE_MOVED", f"Урок перенесён{where}: чтобы вернуть его, отмените урок на новой дате и перенесите его обратно")
        try:
            ref, occ = self._lesson_ref(group, lesson_id, day)
        except OpsError as exc:                                   # the series was stopped / changed after the cancellation
            raise conflict("RESTORE_LESSON_GONE", "Этого урока уже нет в расписании группы: вернуть его нельзя") from exc
        if ref.starts_at <= self.now():
            raise conflict("RESTORE_PAST", "Урок уже должен был начаться: вернуть его нельзя. Отметьте посещаемость или оставьте урок отменённым")
        held = DaysOffService(self.svc).live_for_date(org_id, day)
        if row.day_off_id is not None or held is not None:
            why = f" ({held.reason})" if held is not None else ""
            raise conflict("RESTORE_DAY_OFF", f"Дата — выходной день центра{why}: чтобы вернуть урок, отмените сам выходной")
        slot_warnings = OneOffPurpose(self.svc).restore_slot(group, lesson_id, day, occ)          # #154: the slot of a make-up / an additional lesson may be taken meanwhile: GROUP_BUSY / TEACHER_BUSY / STUDENT_BUSY
        if slot_warnings is None:
            self.svc._assert_teacher_time_is_available(group=group, lesson_id=lesson_id, occurrence_date=day, payload=occ)
        OneOffPurpose(self.svc).relink(group, lesson_id)             # #154: a cancelled make-up makes up for the same lesson again (409 ``RESTORE_MAKEUP_TARGET`` when that lesson changed meanwhile)
        names = {u.id: short_name(u) for u in self.db.query(User).filter(User.id.in_([i for i in (row.marked_by,) if i]))} if row.marked_by else {}
        trace = {"lessonId": lesson_id, "lessonDate": iso, "reason": reason, "cancellationKind": row.cancellation_kind, "cancellationReason": row.cancellation_reason,
                 "version": int(row.version or 1), "cancelledBy": names.get(row.marked_by), "cancelledAt": row.created_at.isoformat() if row.created_at else None}
        self.db.add(GroupAudit(org_id=group.org_id, group_id=group.id, actor_id=actor, actor_role=actor_role if actor_role in ("owner", "admin") else "admin", action="lesson_restored",
                               assigned_teacher_id=group.teacher_id, snapshot=trace, created_at=bc.utc_now()))
        main = TeacherBook(self.db).default_teacher(group, iso, hhmm_to_minutes(ref.end_time))
        reqs = self._lesson_requests(group, lesson_id, iso)
        cancelled = lesson_outcomes(list(reversed(reqs)))             # #157 A10: the record of THIS cancellation learns that the lesson is back (its chip stops being «Урок отменён»)
        if cancelled is not None and cancelled.status == "cancelled":
            self.event(cancelled, "lesson_restored", actor, {"reason": reason})
        if main is not None:
            record = self._system_request(group, ref, main, reason, MANAGER_RESTORE, actor, reqs[0] if reqs else None)
            self._close_restored(record, actor, trace)
        self.db.flush()
        lost = DaysOffService(self.svc)._substitutes_not_restored([row], {group.id: group})          # the substitute the cancellation took off does not come back (R3 12)
        self.db.delete(row)
        self.db.flush()
        return {"ok": True, "restored": True, "lessonId": lesson_id, "lessonDate": iso, "previousKind": trace["cancellationKind"], "examsNote": EXAMS_NOT_RESTORED,
                "warnings": [{"code": w.code, "message": w.message} for w in lost] + (slot_warnings or [])}

    def record_kind_change(self, group: Group, lesson_id: str, day: date, actor: Any, before: str, after: str, reason: str, trigger: str = MANAGER_REASON_CHANGE) -> None:
        """W1 S17 (Д10): «Изменить причину отмены» leaves a record in the journal «Замены» (``closed`` / ``KIND_CHANGED``, trigger ``manager_reason_change``: who, when, from what to what, why) -
        the deduction of the recalculation moves with it, so it must not be a silent edit.  A lesson that is no longer in the schedule or a group without a teacher: no record (the audit row of the
        cancellation has it).  The caller commits."""
        try:
            ref, _occ = self._lesson_ref(group, lesson_id, day)
        except OpsError:
            return
        main = TeacherBook(self.db).default_teacher(group, ref.lesson_date, hhmm_to_minutes(ref.end_time))
        if main is None:
            return
        reqs = self._lesson_requests(group, lesson_id, ref.lesson_date)
        record = self._system_request(group, ref, main, reason, trigger, actor, reqs[0] if reqs else None)
        now = self.now()
        record.status, record.closed_reason, record.decided_at, record.decided_via, record.decided_by, record.decision_note, record.updated_at = (
            "closed", KIND_CHANGED, now, REASON_CHANGE_VIA, actor, None, now)
        self.event(record, "closed", actor, {"closedReason": KIND_CHANGED, "trigger": trigger, "reason": reason, "cancellationKind": {"from": before, "to": after}})

    def _close_restored(self, record: Req, actor: Any, trace: Dict[str, Any]) -> None:
        """The record of a restore: ``closed`` / ``LESSON_RESTORED`` decided by the administrator, one ``closed`` event that carries what was cancelled."""
        now = self.now()
        record.status, record.closed_reason, record.decided_at, record.decided_via, record.decided_by, record.decision_note, record.updated_at = (
            "closed", RESTORED, now, MANAGER_RESTORE, actor, None, now)
        self.event(record, "closed", actor, {"closedReason": RESTORED, "trigger": MANAGER_RESTORE, "reason": trace["reason"], "previous": {k: trace[k] for k in (
            "cancellationKind", "cancellationReason", "version", "cancelledBy", "cancelledAt")}})


def lesson_outcomes(reqs: List[Req]) -> Optional[Req]:
    """The record that says what became of a lesson: the NEWEST of its ``rescheduled`` / ``cancelled`` records and its restore (a restore ends it: ``None``).  ``reqs`` oldest first."""
    last = None
    for r in reqs:
        if r.status in ("rescheduled", "cancelled") or (r.status == "closed" and r.closed_reason == RESTORED):
            last = r
    return None if last is None or last.status == "closed" else last
