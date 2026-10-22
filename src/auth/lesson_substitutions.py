"""LOGIC-B2 "Замены": a teacher's request "I cannot teach this lesson" and the owner / admin decision (SPEC logicB2_spec.md).

* One request = one lesson (group, lesson id, business date); a range is a batch of requests with one ``batch_id`` (D-2).
* A request changes NOTHING until it is decided (D-4); the decision - substitute / reschedule / cancel / reject - is made once, under the group lock and the
  request row lock (D-7, D-16).  Lock order: member -> groups (ascending id) -> ``group_attendance`` rows -> ``lesson_substitution_requests`` -> User -> assignments.
* Substitute = ``LessonTeachers._put_locked`` (package B) in the same transaction; cancel = a ``cancelled`` row of the journal (the mechanics of B / A decision 3);
  reschedule = the original lesson cancelled + a new one-off lesson (D-5), atomically; reject = a reason the teacher sees.
* A request whose premise disappeared is closed (D-9): eager hooks in the operations of B / B2 + the lazy ``revalidate`` in list / detail / decide / withdraw
  + a ``before_flush`` listener on new journal rows (a lesson that got a row has started or was cancelled).
* Every status change = exactly one row of ``lesson_substitution_events`` (INV-B2-7); the journal is append-only.
* Time: ``business_clock`` only; ``starts_at`` / ``created_at`` / ``decided_at`` are UTC naive, lesson dates are ``YYYY-MM-DD``.
* Interface with package A (SPEC 3.5): ``moved_one_off_keys``; a cancelled / moved row is never ``completed`` so payroll never pays it.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from uuid import UUID, uuid4

from sqlalchemy import event, func, or_
from sqlalchemy.orm import Session

from src.models.auth import PayrollRate

from . import business_clock as bc
from . import exam_bridge, payroll_hooks, substitution_rules as R
from .lesson_teachers import (
    BusyItem,
    LessonTeachers,
    TeacherBook,
    _s,
    _uuid,
    effective_teacher,
)
from .models import (
    Group,
    GroupAttendance,
    GroupAttendanceAudit,
    GroupScheduleOneOffLesson,
    LessonSubstitutionEvent,
    LessonSubstitutionRequest as Req,
    LessonTeacherAssignment,
    OrganizationMember,
    StudentGroup,
    User,
)
from .ops_rules import OpsError, conflict, forbidden, hhmm_to_minutes, normalize_room, not_found, short_name, unprocessable
from .student_archive import student_not_archived
from .substitution_schemas import CLOSED_REASON_TEXT, REASON_LABELS

logger = logging.getLogger(__name__)

PENDING = "pending"
KIND = {"substituted": "substitute", "rescheduled": "reschedule", "cancelled": "cancel", "rejected": "reject", "withdrawn": "withdraw", "closed": "closed"}
STATUS_OF = {"substitute": "substituted", "reschedule": "rescheduled", "cancel": "cancelled", "reject": "rejected"}
LIST_DEFAULT_DAYS = 30
MINE_DAYS = 60
ASSIGNED_AHEAD_DAYS = 30
MOVE_STARTED_TEXT = "Урок уже начался или прошёл: перенести его нельзя"          # the one text of every «the lesson is no longer ahead» refusal of a move


def _next_event_time(db, request_id: Any, now: datetime) -> datetime:
    """The journal is ordered by ``created_at``: two events of one request in the same instant (one transaction, a frozen test clock) keep their order by a microsecond step."""
    last = db.query(func.max(LessonSubstitutionEvent.created_at)).filter(LessonSubstitutionEvent.request_id == request_id).scalar()
    return last + timedelta(microseconds=1) if last is not None and last >= now else now


class Ctx:
    """Everything the items of one answer need, loaded in batches."""

    def __init__(self) -> None:
        self.groups: Dict[UUID, Group] = {}
        self.users: Dict[str, User] = {}
        self.counts: Dict[UUID, int] = {}
        self.occ: Dict[Tuple[UUID, str, str], Dict[str, Any]] = {}
        self.previous: Dict[UUID, Req] = {}
        self.active_members: Set[UUID] = set()


class SubstitutionsBase:
    def __init__(self, svc) -> None:
        self.svc = svc
        self.db = svc.db
        self.teachers = LessonTeachers(svc)

    # ====================================================================================== small helpers
    @staticmethod
    def now() -> datetime:
        return bc.utc_now()

    def event(self, req: Req, action: str, actor_id: Any, snapshot: Optional[Dict[str, Any]] = None) -> None:
        self.db.add(LessonSubstitutionEvent(request_id=req.id, org_id=req.org_id, actor_id=_uuid(actor_id), action=action,
                                            snapshot=snapshot or {}, created_at=_next_event_time(self.db, req.id, self.now())))

    def _person(self, ctx: Ctx, user_id: Any) -> Optional[Dict[str, Any]]:
        if user_id is None:
            return None
        return {"id": str(user_id), "name": short_name(ctx.users.get(str(user_id)))}

    # ============================================================================================ item
    def ctx_for(self, reqs: Sequence[Req]) -> Ctx:
        ctx = Ctx()
        if not reqs:
            return ctx
        gids = {r.group_id for r in reqs}
        ctx.groups = {g.id: g for g in self.db.query(Group).filter(Group.id.in_(gids)).all()}
        prev_ids = {r.previous_request_id for r in reqs if r.previous_request_id}
        if prev_ids:
            ctx.previous = {p.id: p for p in self.db.query(Req).filter(Req.id.in_(prev_ids)).all()}
        user_ids: Set[Any] = set()
        for r in list(reqs) + list(ctx.previous.values()):
            user_ids.update({r.requester_id, r.suggested_teacher_id, r.decided_by, r.substitute_id})
        ctx.users = self.teachers.users_by_id(u for u in user_ids if u)
        ctx.counts = dict(
            self.db.query(StudentGroup.group_id, func.count(StudentGroup.id))
            .filter(StudentGroup.group_id.in_(gids), StudentGroup.is_active.is_(True), student_not_archived())
            .group_by(StudentGroup.group_id)
            .all()
        )
        suggested = {r.suggested_teacher_id for r in reqs if r.suggested_teacher_id}
        if suggested:
            ctx.active_members = {
                uid for (uid,) in self.db.query(OrganizationMember.user_id).join(User, User.id == OrganizationMember.user_id)
                .filter(OrganizationMember.user_id.in_(suggested), OrganizationMember.is_active.is_(True), User.is_active.isnot(False))
            }
        by_group: Dict[UUID, List[str]] = {}
        for r in reqs:
            by_group.setdefault(r.group_id, []).append(r.lesson_date)
        for gid, days in by_group.items():
            group = ctx.groups.get(gid)
            if group is None:
                continue
            d_from, d_to = date.fromisoformat(min(days)), date.fromisoformat(max(days))
            if (d_to - d_from).days > 400:
                continue
            for o in self.svc._resolved_occurrences_for_group(group, date_from=d_from, date_to=d_to):
                ctx.occ[(gid, o["id"], o["occurrenceDate"])] = o
        return ctx

    def item(self, r: Req, ctx: Ctx, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or self.now()
        group = ctx.groups.get(r.group_id)
        occ = ctx.occ.get((r.group_id, r.lesson_id, r.lesson_date))
        minutes = R.minutes_to_start(r.starts_at, now)
        pending = r.status == PENDING
        decision = None
        if not pending:
            moved = None
            if r.moved_to_date:
                moved = {"lessonId": r.moved_to_lesson_id, "date": r.moved_to_date, "startTime": r.moved_to_start, "endTime": r.moved_to_end}
            text = None
            if r.closed_reason:
                text = CLOSED_REASON_TEXT.get(r.closed_reason)
                if r.closed_reason == "GROUP_TEACHER_CHANGED" and r.decision_note:
                    text = f"{text}: урок ведёт {r.decision_note}"
            decision = {
                "kind": KIND[r.status], "by": self._person(ctx, r.decided_by), "at": r.decided_at.isoformat() if r.decided_at else r.updated_at.isoformat(),
                "via": r.decided_via, "note": None if r.status == "closed" else r.decision_note,
                "substitute": self._person(ctx, r.substitute_id), "movedTo": moved, "closedReason": r.closed_reason, "closedReasonText": text,
            }
        prev = None
        if r.previous_request_id and r.previous_request_id in ctx.previous:
            p = ctx.previous[r.previous_request_id]
            prev = {"id": str(p.id), "status": p.status, "teacherName": short_name(ctx.users.get(str(p.requester_id))),
                    "substituteName": short_name(ctx.users.get(str(p.substitute_id))) if p.substitute_id else None}
        sug = None
        if r.suggested_teacher_id:
            sug = {"id": str(r.suggested_teacher_id), "name": short_name(ctx.users.get(str(r.suggested_teacher_id))),
                   "active": r.suggested_teacher_id in ctx.active_members}
        return {
            "id": str(r.id), "batchId": str(r.batch_id), "status": r.status, "origin": r.origin,
            "group": {"id": str(r.group_id), "name": group.name if group else "", "studentCount": int(ctx.counts.get(r.group_id, 0))},
            "lesson": {"lessonId": r.lesson_id, "date": r.lesson_date, "startTime": r.start_time, "endTime": r.end_time,
                       "room": normalize_room(occ.get("room")) if occ else None, "format": (occ.get("format") if occ else None) or "offline",
                       "startsAtUtc": r.starts_at.isoformat(), "exists": occ is not None or r.closed_reason == "GROUP_ARCHIVED"},
            "teacher": self._person(ctx, r.requester_id), "reasonCode": r.reason_code, "reasonLabel": REASON_LABELS[r.reason_code],
            "comment": r.comment, "suggestedTeacher": sug, "createdAt": r.created_at.isoformat(),
            "minutesToStart": minutes, "urgent": bool(pending and R.is_urgent(minutes)), "overdue": bool(pending and R.is_overdue(minutes)),
            "decision": decision, "previous": prev,
        }

    def items(self, reqs: Sequence[Req]) -> List[Dict[str, Any]]:
        ctx = self.ctx_for(reqs)
        now = self.now()
        return [self.item(r, ctx, now) for r in reqs]

    def one(self, r: Req) -> Dict[str, Any]:
        return self.items([r])[0]

    def already_decided(self, r: Req, message: Optional[str] = None) -> OpsError:
        item = self.one(r)
        if message is None:
            message = "Учитель отозвал запрос" if r.status == "withdrawn" else (
                f"Запрос закрыт: {item['decision']['closedReasonText']}" if r.status == "closed" and item["decision"] and item["decision"]["closedReasonText"]
                else R.ERROR_CATALOGUE["SUBSTITUTION_ALREADY_DECIDED"][1])
        return R.err(409, "SUBSTITUTION_ALREADY_DECIDED", message, item=item)

    # ===================================================================================== locking, loading
    def _group_locked(self, org_id: Any, group_id: Any) -> Optional[Group]:
        return self.db.query(Group).filter(Group.id == group_id, Group.org_id == org_id).with_for_update().populate_existing().first()

    def _request(self, org_id: Any, request_id: Any) -> Req:
        rid = _uuid(request_id)
        r = self.db.query(Req).filter(Req.id == rid, Req.org_id == org_id).first() if rid else None
        if r is None:
            raise R.err(404, "SUBSTITUTION_NOT_FOUND")
        return r

    def _lock_request(self, org_id: Any, request_id: Any) -> Tuple[Group, Req]:
        """Group lock first, then the request row (D-16); the request is re-read under the lock."""
        r0 = self._request(org_id, request_id)
        group = self._group_locked(org_id, r0.group_id)
        if group is None:
            raise R.err(404, "SUBSTITUTION_NOT_FOUND")
        r = self.db.query(Req).filter(Req.id == r0.id).with_for_update().populate_existing().first()
        if r is None:
            raise R.err(404, "SUBSTITUTION_NOT_FOUND")
        return group, r

    # =========================================================================================== revalidate
    def _row(self, r: Req) -> Optional[GroupAttendance]:
        return self.db.query(GroupAttendance).filter(
            GroupAttendance.group_id == r.group_id, GroupAttendance.lesson_id == r.lesson_id, GroupAttendance.lesson_date == r.lesson_date
        ).first()

    def closure_reason(self, r: Req, group: Group, book: Optional[TeacherBook] = None) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """Why a pending request must be closed now (SPEC 3.4 order) and, when the lesson only changed its time, the new times.  Returns ``(reason, occurrence)``."""
        if group.status != "active":
            return "GROUP_ARCHIVED", None
        day = date.fromisoformat(r.lesson_date)
        occ = next((o for o in self.svc._resolved_occurrences_for_group(group, date_from=day, date_to=day) if o["id"] == r.lesson_id), None)
        if occ is None:
            return "LESSON_REMOVED", None
        row = self._row(r)
        if row is not None:
            return ("LESSON_CANCELLED" if row.status == "cancelled" else "LESSON_STARTED"), occ
        if day < bc.month_window_start(bc.business_today()):
            return "LESSON_OUTSIDE_WINDOW", occ
        member = (self.db.query(OrganizationMember.id).join(User, User.id == OrganizationMember.user_id)
                  .filter(OrganizationMember.org_id == r.org_id, OrganizationMember.user_id == r.requester_id,
                          OrganizationMember.is_active.is_(True), User.is_active.isnot(False)).first())
        if member is None:
            return "TEACHER_REMOVED", occ
        book = book or TeacherBook(self.db)
        try:
            end = hhmm_to_minutes(occ["endTime"])
        except (KeyError, ValueError, AttributeError):
            end = None
        if book.resolve(group, r.lesson_id, r.lesson_date, end).teacher_id != r.requester_id:
            return "GROUP_TEACHER_CHANGED", occ
        return None, occ

    def close(self, r: Req, reason: str, *, actor_id: Any = None, trigger: str = "revalidate", group: Optional[Group] = None) -> None:
        """Close a pending request because its premise is gone (status ``closed`` + one event)."""
        now = self.now()
        note = None
        if reason == "GROUP_TEACHER_CHANGED" and group is not None:
            day = date.fromisoformat(r.lesson_date)
            occ = next((o for o in self.svc._resolved_occurrences_for_group(group, date_from=day, date_to=day) if o["id"] == r.lesson_id), None)
            end = hhmm_to_minutes(occ["endTime"]) if occ else None
            new = TeacherBook(self.db).resolve(group, r.lesson_id, r.lesson_date, end).teacher_id
            note = short_name(self.db.query(User).filter(User.id == new).first()) if new else None
        r.status = "closed"
        r.closed_reason = reason
        r.decided_at = now
        r.decided_via = "system"
        r.decided_by = None
        r.decision_note = note
        r.updated_at = now
        self.event(r, "closed", actor_id, {"closedReason": reason, "trigger": trigger})

    @staticmethod
    def _sync_times(r: Req, occ: Dict[str, Any]) -> None:
        """The lesson was moved in the schedule after the request: the request follows (start / end / UTC start)."""
        r.start_time, r.end_time = occ["startTime"], occ["endTime"]
        r.starts_at = bc.lesson_instant_utc(date.fromisoformat(r.lesson_date), hhmm_to_minutes(occ["startTime"]))

    def revalidate(self, reqs: Sequence[Req], *, trigger: str = "revalidate", lock: bool = True) -> int:
        """Lazy pass over the PENDING requests (list / detail / decide / withdraw): close what is no longer valid, refresh the times of a moved lesson.  Writes.  Returns the closed count."""
        closed = 0
        touched = False
        by_group: Dict[UUID, List[Req]] = {}
        for r in reqs:
            if r.status == PENDING:
                by_group.setdefault(r.group_id, []).append(r)
        for gid, group_reqs in by_group.items():
            group = self.db.query(Group).filter(Group.id == gid).first()
            if group is None:
                continue
            book = TeacherBook(self.db)
            plan = []
            for r in group_reqs:
                reason, occ = self.closure_reason(r, group, book)
                times_changed = bool(occ) and (occ.get("startTime") != r.start_time or occ.get("endTime") != r.end_time)
                if reason or times_changed:
                    plan.append((r, reason, occ, times_changed))
            if not plan:
                continue
            if lock:
                group = self._group_locked(group.org_id, gid) or group
            for r, reason, occ, times_changed in plan:
                if lock:
                    fresh = self.db.query(Req).filter(Req.id == r.id).with_for_update().populate_existing().first()
                    if fresh is None or fresh.status != PENDING:
                        continue
                    r = fresh
                touched = True
                if reason:
                    self.close(r, reason, trigger=trigger, group=group)
                    closed += 1
                elif times_changed and occ:
                    self._sync_times(r, occ)
        if touched:                 # a read that found nothing to fix commits nothing
            self.db.commit()
        return closed

    # =============================================================================================== create
    def _suggested(self, org_id: Any, requester_id: Any, suggested: Optional[str]) -> Optional[UUID]:
        if suggested in (None, ""):
            return None
        uid = _uuid(suggested)
        if uid is None or uid == requester_id or self.teachers.teaching_member(org_id, uid) is None:
            raise R.err(422, "TARGET_NOT_TEACHER", "Выберите преподавателя центра")
        return uid

    def create(self, org_id: Any, actor_id: Any, body) -> Dict[str, Any]:
        """``POST /org/substitutions`` (a teacher; his own future lessons; up to 60; not further than 90 days ahead)."""
        actor = _uuid(actor_id)
        now, today = self.now(), bc.business_today()
        if len(body.lessons) > R.CREATE_MAX_LESSONS:
            raise R.err(422, "TOO_MANY_LESSONS")
        comment = (body.comment or "").strip() or None
        if body.reasonCode == "other" and (comment is None or len(comment) < 3):
            raise R.err(422, "COMMENT_REQUIRED")
        if comment is not None and len(comment) > 500:
            raise unprocessable("COMMENT_TOO_LONG", "Комментарий не длиннее 500 символов")
        refs, seen = [], set()
        for ref in body.lessons:
            key = (str(ref.groupId), ref.lessonId, ref.lessonDate.isoformat())
            if key not in seen:
                seen.add(key)
                refs.append(key)
        if any(date.fromisoformat(k[2]) > today + timedelta(days=R.CREATE_MAX_DAYS_AHEAD) for k in refs):
            raise R.err(422, "LESSON_TOO_FAR")
        suggested = self._suggested(org_id, actor, body.suggestedTeacherId)
        batch = uuid4()
        single = len(refs) == 1
        created: List[Req] = []
        already: List[Req] = []
        skipped: List[Dict[str, Any]] = []
        locked: Dict[str, Optional[Group]] = {}
        book = TeacherBook(self.db)                                    # one book (histories, assignments) for the whole batch
        for gid in sorted({k[0] for k in refs}):                       # groups in ascending id (D-16)
            locked[gid] = self._group_locked(org_id, gid) if _uuid(gid) else None
        for gid, lesson_id, day in sorted(refs, key=lambda k: (k[2], k[0], k[1])):
            try:
                kind, r = self._create_one(locked.get(gid), org_id, actor, lesson_id, day, body.reasonCode, comment, suggested, batch, now, book)
            except OpsError as exc:
                if single:
                    self.db.rollback()
                    raise
                skipped.append({"groupId": gid, "lessonId": lesson_id, "lessonDate": day, "code": exc.code, "message": exc.message})
                continue
            (created if kind == "created" else already).append(r)
        if not created and not already:
            self.db.rollback()
            raise R.err(409, "NO_LESSONS_REQUESTABLE", skipped=skipped)
        self.db.commit()
        # a pure repeat answers the batch of the rows it found (the contract: the same ``batchId`` as the first answer)
        out_batch = batch if created else already[0].batch_id
        return {"batchId": str(out_batch), "created": self.items(created), "alreadyRequested": self.items(already), "skipped": skipped}

    def _create_one(self, group: Optional[Group], org_id: Any, actor: UUID, lesson_id: str, day: str, reason: str, comment: Optional[str],
                    suggested: Optional[UUID], batch: UUID, now: datetime, book: Optional[TeacherBook] = None) -> Tuple[str, Req]:
        if group is None:
            raise not_found("LESSON_NOT_FOUND", "Урок не найден")
        existing = self.db.query(Req).filter(Req.group_id == group.id, Req.lesson_id == lesson_id, Req.lesson_date == day, Req.status == PENDING).first()
        if existing is not None and existing.requester_id == actor:
            return "already", existing
        if group.status != "active":
            raise conflict("GROUP_ARCHIVED", "Группа в архиве: действие недоступно")
        d = date.fromisoformat(day)
        occ = next((o for o in self.svc._resolved_occurrences_for_group(group, date_from=d, date_to=d) if o["id"] == lesson_id), None)
        if occ is None:
            raise not_found("LESSON_NOT_FOUND", "Урок не найден")
        row = self.db.query(GroupAttendance).filter(GroupAttendance.group_id == group.id, GroupAttendance.lesson_id == lesson_id,
                                                    GroupAttendance.lesson_date == day).with_for_update().first()
        if row is not None:
            if row.status == "cancelled":
                raise conflict("LESSON_CANCELLED", "Урок отменён")
            raise R.err(409, "LESSON_ALREADY_STARTED")
        start, end = hhmm_to_minutes(occ["startTime"]), hhmm_to_minutes(occ["endTime"])
        starts_at = bc.lesson_instant_utc(d, start)
        if starts_at <= now:
            raise R.err(409, "LESSON_ALREADY_STARTED")
        teacher = (book or TeacherBook(self.db)).resolve(group, lesson_id, day, end).teacher_id
        if teacher != actor:
            who = short_name(self.db.query(User).filter(User.id == teacher).first()) if teacher else None
            raise forbidden("NOT_LESSON_TEACHER", (f"Этот урок ведёт {who}" if who else "Этот урок ведёт другой преподаватель"))
        prev = (
            self.db.query(Req).filter(Req.group_id == group.id, Req.lesson_id == lesson_id, Req.lesson_date == day)
            .order_by(Req.created_at.desc()).first()
        )
        r = Req(org_id=org_id, group_id=group.id, lesson_id=lesson_id, lesson_date=day, start_time=occ["startTime"], end_time=occ["endTime"],
                starts_at=starts_at, requester_id=actor, origin="teacher", batch_id=batch, reason_code=reason, comment=comment,
                suggested_teacher_id=suggested, status=PENDING, previous_request_id=prev.id if prev is not None else None,
                created_at=now, updated_at=now)
        self.db.add(r)
        self.db.flush()
        self.event(r, "requested", actor, {"reasonCode": reason, "suggestedTeacherId": _s(suggested), "batchId": str(batch)})
        return "created", r

    # ============================================================================================ my lessons
    def my_lessons(self, org_id: Any, actor_id: Any, d_from: date, d_to: date) -> Dict[str, Any]:
        if d_to < d_from or (d_to - d_from).days > R.MY_LESSONS_MAX_DAYS:
            raise R.err(422, "PERIOD_TOO_LONG")
        actor = _uuid(actor_id)
        visible = self.teachers.groups_visible_to_teacher(org_id, actor, window_from=d_from, window_to=d_to, include_history=False)
        groups = self.db.query(Group).filter(Group.org_id == org_id, Group.status == "active", Group.id.in_(list(visible))).order_by(Group.name).all()
        occs = self.teachers.annotated_occurrences(groups, d_from, d_to, only_teacher=actor)
        ids = [g.id for g in groups]
        rows = {(str(r.group_id), r.lesson_id, r.lesson_date): r for r in self.db.query(GroupAttendance).filter(
            GroupAttendance.group_id.in_(ids), GroupAttendance.lesson_date >= d_from.isoformat(), GroupAttendance.lesson_date <= d_to.isoformat())} if ids else {}
        pending = {(str(r.group_id), r.lesson_id, r.lesson_date): r for r in self.db.query(Req).filter(
            Req.group_id.in_(ids), Req.status == PENDING, Req.lesson_date >= d_from.isoformat(), Req.lesson_date <= d_to.isoformat())} if ids else {}
        now = self.now()
        out = []
        for o in sorted(occs, key=lambda o: (o["occurrenceDate"], o["startTime"], o["groupName"])):
            key = (o["groupId"], o["id"], o["occurrenceDate"])
            row, req = rows.get(key), pending.get(key)
            starts = bc.lesson_instant_utc(date.fromisoformat(o["occurrenceDate"]), hhmm_to_minutes(o["startTime"]))
            if row is not None and row.status == "cancelled":
                status, can, block = "cancelled", False, "LESSON_CANCELLED"
            elif row is not None:
                status, can, block = ("started" if row.status == "in_progress" else "passed"), False, "LESSON_ALREADY_STARTED"
            elif req is not None:
                status, can, block = "requested", False, "ALREADY_REQUESTED"
            elif starts <= now:
                status, can, block = "passed", False, "LESSON_ALREADY_STARTED"
            else:
                status, can, block = "scheduled", True, None
            out.append({"groupId": o["groupId"], "groupName": o["groupName"], "lessonId": o["id"], "lessonDate": o["occurrenceDate"],
                        "startTime": o["startTime"], "endTime": o["endTime"], "room": normalize_room(o.get("room")), "format": o.get("format") or "offline",
                        "isSubstitution": bool(o.get("isSubstitution")), "status": status, "requestId": str(req.id) if req is not None else None,
                        "canRequest": can, "blockReason": block})
        return {"dateFrom": d_from.isoformat(), "dateTo": d_to.isoformat(), "lessons": out}

    # ====================================================================================================== mine
    def mine(self, org_id: Any, actor_id: Any, status: str) -> Dict[str, Any]:
        actor = _uuid(actor_id)
        q = self.db.query(Req).filter(Req.org_id == org_id, Req.requester_id == actor)
        if status == "pending":
            q = q.filter(Req.status == PENDING)
        else:
            since = (bc.business_today() - timedelta(days=MINE_DAYS)).isoformat()
            q = q.filter(or_(Req.status == PENDING, Req.lesson_date >= since))
        reqs = q.order_by(Req.created_at.desc(), Req.id).all()
        self.revalidate([r for r in reqs if r.status == PENDING])
        for r in reqs:
            self.db.refresh(r)
        pending_total = sum(1 for r in reqs if r.status == PENDING)
        return {"items": self.items(reqs), "pendingTotal": pending_total, "assignedToMe": self._assigned_to(org_id, actor)}

    def _assigned_to(self, org_id: Any, actor: UUID) -> List[Dict[str, Any]]:
        today = bc.business_today()
        rows = (
            self.db.query(LessonTeacherAssignment, Group)
            .join(Group, Group.id == LessonTeacherAssignment.group_id)
            .filter(LessonTeacherAssignment.org_id == org_id, LessonTeacherAssignment.teacher_id == actor, LessonTeacherAssignment.cancelled_at.is_(None),
                    LessonTeacherAssignment.lesson_date >= today.isoformat(),
                    LessonTeacherAssignment.lesson_date <= (today + timedelta(days=ASSIGNED_AHEAD_DAYS)).isoformat(), Group.status == "active")
            .order_by(LessonTeacherAssignment.lesson_date).all()
        )
        out = []
        book = TeacherBook(self.db)
        gone = ({(r.group_id, r.lesson_id, r.lesson_date) for r in self.db.query(GroupAttendance.group_id, GroupAttendance.lesson_id, GroupAttendance.lesson_date).filter(
            GroupAttendance.group_id.in_({g.id for _a, g in rows}), GroupAttendance.status == "cancelled",
            GroupAttendance.lesson_date.in_({a.lesson_date for a, _g in rows}))} if rows else set())
        for asg, group in rows:
            if (group.id, asg.lesson_id, asg.lesson_date) in gone:
                continue                                    # a cancelled / moved lesson is not shown as «Замена»
            d = date.fromisoformat(asg.lesson_date)
            occ = next((o for o in self.svc._resolved_occurrences_for_group(group, date_from=d, date_to=d) if o["id"] == asg.lesson_id), None)
            if occ is None:
                continue
            default = book.default_teacher(group, asg.lesson_date, hhmm_to_minutes(occ["endTime"]))
            user = self.db.query(User).filter(User.id == default).first() if default else None
            out.append({"groupId": str(group.id), "groupName": group.name, "lessonId": asg.lesson_id, "lessonDate": asg.lesson_date,
                        "startTime": occ["startTime"], "endTime": occ["endTime"],
                        "replacing": {"id": str(default), "name": short_name(user)} if default else None,
                        "assignedAt": asg.created_at.isoformat() if asg.created_at else None})
        return out

    # =================================================================================================== withdraw
    def withdraw(self, org_id: Any, actor_id: Any, request_id: Any) -> Dict[str, Any]:
        actor = _uuid(actor_id)
        r0 = self._request(org_id, request_id)
        if r0.requester_id != actor:
            raise R.err(403, "NOT_REQUEST_OWNER")
        group, r = self._lock_request(org_id, request_id)
        if r.status == PENDING:
            reason, _occ = self.closure_reason(r, group)
            if reason and reason not in ("LESSON_OUTSIDE_WINDOW",):
                self.close(r, reason, trigger="withdraw", group=group)
                self.db.commit()
        if r.status == "withdrawn" and r.decided_by == actor:
            self.db.commit()
            return self.one(r)
        if r.status != PENDING:
            self.db.commit()
            raise self.already_decided(r)
        now = self.now()
        r.status, r.decided_by, r.decided_at, r.decided_via, r.updated_at = "withdrawn", actor, now, "withdraw", now
        self.event(r, "withdrawn", actor)
        self.db.commit()
        return self.one(r)

    def withdraw_batch(self, org_id: Any, actor_id: Any, ids: Sequence[str]) -> Dict[str, Any]:
        done, skipped = [], []
        for rid in ids:
            try:
                done.append(self.withdraw(org_id, actor_id, rid))
            except OpsError as exc:
                self.db.rollback()
                skipped.append({"id": str(rid), "code": exc.code, "message": exc.message})
        return {"withdrawn": done, "skipped": skipped}

    # ================================================================================== summary / list / detail
    def summary(self, org_id: Any) -> Dict[str, int]:
        now = self.now()
        base = self.db.query(Req).filter(Req.org_id == org_id, Req.status == PENDING)
        pending = base.count()
        urgent = base.filter(Req.starts_at > now, Req.starts_at < now + timedelta(minutes=R.URGENT_MINUTES)).count()
        overdue = base.filter(Req.starts_at <= now).count()
        return {"pendingTotal": pending, "urgentTotal": urgent, "overdueTotal": overdue}

    def list(self, org_id: Any, *, status: str, group_id: Optional[str], teacher_id: Optional[str], date_from: Optional[date], date_to: Optional[date],
             urgent: Optional[bool], search: Optional[str], limit: int, offset: int) -> Dict[str, Any]:
        now, today = self.now(), bc.business_today()
        q = self.db.query(Req).filter(Req.org_id == org_id)
        if status == "pending":
            q = q.filter(Req.status == PENDING)
        elif status == "decided":
            q = q.filter(Req.status != PENDING)
        if status != "pending" and date_from is None and date_to is None:
            q = q.filter(Req.lesson_date >= (today - timedelta(days=LIST_DEFAULT_DAYS)).isoformat())
        if group_id and _uuid(group_id):
            q = q.filter(Req.group_id == _uuid(group_id))
        if teacher_id and _uuid(teacher_id):
            q = q.filter(Req.requester_id == _uuid(teacher_id))
        if date_from:
            q = q.filter(Req.lesson_date >= date_from.isoformat())
        if date_to:
            q = q.filter(Req.lesson_date <= date_to.isoformat())
        if urgent:
            q = q.filter(Req.status == PENDING, Req.starts_at > now, Req.starts_at < now + timedelta(minutes=R.URGENT_MINUTES))
        if search:
            like = f"%{search.strip()}%"
            q = q.join(Group, Group.id == Req.group_id).join(User, User.id == Req.requester_id).filter(
                or_(Group.name.ilike(like), User.last_name.ilike(like), User.first_name.ilike(like)))
        total = q.count()
        if status == "pending":
            q = q.order_by((Req.starts_at <= now).desc(), Req.starts_at, Req.id)
        elif status == "decided":
            q = q.order_by(Req.decided_at.desc(), Req.id)
        else:
            q = q.order_by((Req.status == PENDING).desc(), Req.starts_at, Req.id)
        reqs = q.offset(offset).limit(limit).all()
        # a lazy pass over what is being RETURNED (not over every pending request of the centre): a stale request must not be listed
        if self.revalidate([r for r in reqs if r.status == PENDING]):
            if status == "pending":
                reqs = q.offset(offset).limit(limit).all()       # the page refills with the next ones
                total = q.count()
            else:
                for r in reqs:
                    self.db.refresh(r)
        counts = self.summary(org_id)
        return {"items": self.items(reqs), "total": total, "pendingTotal": counts["pendingTotal"], "urgentTotal": counts["urgentTotal"]}

    def detail(self, org_id: Any, actor_id: Any, is_manager: bool, request_id: Any) -> Dict[str, Any]:
        r = self._request(org_id, request_id)
        if not is_manager and r.requester_id != _uuid(actor_id):
            raise R.err(403, "NOT_REQUEST_OWNER")
        if r.status == PENDING:
            self.revalidate([r])
            self.db.refresh(r)
        chain: List[Req] = []
        cur = r
        while cur.previous_request_id and len(chain) < 10:
            cur = self.db.query(Req).filter(Req.id == cur.previous_request_id).first()
            if cur is None:
                break
            chain.insert(0, cur)
        events = self.db.query(LessonSubstitutionEvent).filter(LessonSubstitutionEvent.request_id == r.id).order_by(
            LessonSubstitutionEvent.created_at, LessonSubstitutionEvent.id).all()
        names = self.teachers.users_by_id(e.actor_id for e in events if e.actor_id)
        return {
            "item": self.one(r), "chain": self.items(chain),
            "events": [{"action": e.action, "actor": {"id": str(e.actor_id), "name": short_name(names.get(str(e.actor_id)))} if e.actor_id else None,
                        "at": e.created_at.isoformat(), "snapshot": e.snapshot or {}} for e in events],
            "examBinding": self.exam_binding(r),
        }

    def exam_binding(self, r: Req) -> Dict[str, Any]:
        try:
            exams = exam_bridge.completion_check(r.org_id, r.group_id, r.lesson_id, r.lesson_date)
        except (exam_bridge.ExamBridgeUnavailable, exam_bridge.ExamBridgeConflict):
            return {"state": "unavailable", "title": None, "canCancel": False}
        if not exams:
            return {"state": "none", "title": None, "canCancel": True}
        first = exams[0]
        return {"state": "ok", "title": first.get("title"), "canCancel": first.get("status") != "active"}

    # ================================================================================================ candidates
    def rate_missing(self, org_id: Any, teacher_id: UUID, lesson_type: str, lesson_date: str) -> bool:
        month = date.fromisoformat(lesson_date[:7] + "-01")
        return (
            self.db.query(PayrollRate.id)
            .filter(PayrollRate.org_id == org_id, PayrollRate.teacher_id == teacher_id, PayrollRate.lesson_type == (lesson_type or "group"),
                    PayrollRate.valid_from <= month).first() is None
        )

    def rates_missing(self, org_id: Any, teacher_ids: Iterable[UUID], lesson_type: str, lesson_date: str) -> Set[UUID]:
        """The teachers (of ``teacher_ids``) without a payroll rate for the type of lessons in the month of the lesson - one query for all of them."""
        ids = list(teacher_ids)
        if not ids:
            return set()
        month = date.fromisoformat(lesson_date[:7] + "-01")
        have = {t for (t,) in self.db.query(PayrollRate.teacher_id).filter(
            PayrollRate.org_id == org_id, PayrollRate.teacher_id.in_(ids), PayrollRate.lesson_type == (lesson_type or "group"), PayrollRate.valid_from <= month)}
        return set(ids) - have

    def colleagues(self, org_id: Any, actor_id: Any) -> List[Dict[str, Any]]:
        """«Предложить коллегу»: the active staff of the centre who can teach (teacher / owner / admin) except the caller - id, name and role only (no e-mail, no phone)."""
        actor = _uuid(actor_id)
        roles = dict(self.db.query(OrganizationMember.user_id, OrganizationMember.org_role).filter(OrganizationMember.org_id == org_id).all())
        return [{"id": str(u.id), "name": short_name(u) or "", "role": roles.get(u.id, "teacher")}
                for u in self.teachers.staff_who_can_teach(org_id) if u.id != actor]

    def candidates(self, org_id: Any, is_owner: bool, request_id: Any) -> Dict[str, Any]:
        r = self._request(org_id, request_id)
        if r.status == PENDING:
            self.revalidate([r])
            self.db.refresh(r)
        group = self.db.query(Group).filter(Group.id == r.group_id).first()
        d = date.fromisoformat(r.lesson_date)
        start, end = hhmm_to_minutes(r.start_time), hhmm_to_minutes(r.end_time)
        book = TeacherBook(self.db)
        default = book.default_teacher(group, r.lesson_date, end)
        cand = BusyItem(r.lesson_date, start, end, r.start_time, r.end_time, str(group.id), group.name, r.lesson_id)
        pending_by_user: Dict[UUID, Set[Tuple[str, str, str]]] = {}
        for pr in self.db.query(Req).filter(Req.org_id == org_id, Req.status == PENDING, Req.lesson_date == r.lesson_date).all():
            pending_by_user.setdefault(pr.requester_id, set()).add((str(pr.group_id), pr.lesson_id, pr.lesson_date))
        out = []
        everybody = self.teachers.busy_map(org_id, d, d, exclude_keys={cand.key}, book=book)          # one pass for all the teachers
        roles = dict(self.db.query(OrganizationMember.user_id, OrganizationMember.org_role).filter(OrganizationMember.org_id == org_id).all())
        staff = self.teachers.staff_who_can_teach(org_id)
        no_rate = self.rates_missing(org_id, [u.id for u in staff], group.lesson_type, r.lesson_date) if is_owner else set()
        for user in staff:
            if user.id == r.requester_id:
                continue
            member = SimpleNamespace(org_role=roles.get(user.id, "teacher"))
            pairs = self.teachers.overlap_pairs([cand], everybody.get(user.id, []))
            other = pairs[0][1] if pairs else None
            out.append({
                "teacherId": str(user.id), "name": short_name(user) or user.email, "orgRole": member.org_role if member else "teacher",
                "suggested": user.id == r.suggested_teacher_id, "isDefaultTeacher": user.id == default, "busy": other is not None,
                "busyWith": ({"kind": "substitution" if other.substitution else "lesson", "groupName": other.group_name, "startTime": other.startTime,
                              "endTime": other.endTime, "pendingRequest": other.key in pending_by_user.get(user.id, set())} if other is not None else None),
                "rateMissing": (user.id in no_rate) if is_owner else None,
            })
        out.sort(key=lambda c: (0 if c["suggested"] else 1, 1 if c["busy"] and not c["suggested"] else 0, c["name"]))
        return {"requestId": str(r.id), "lesson": {"groupId": str(group.id), "groupName": group.name, "lessonId": r.lesson_id, "date": r.lesson_date,
                                                   "startTime": r.start_time, "endTime": r.end_time},
                "requesterId": str(r.requester_id), "candidates": out}


# ======================================================================================================================
# decisions
# ======================================================================================================================
def _label_text(r: Req) -> str:
    label = REASON_LABELS[r.reason_code]
    return f"{label}: {r.comment}" if r.comment else label


class Substitutions(SubstitutionsBase):
    """The four decisions of an administrator (substitute / reschedule / cancel / reject) and the bulk variant."""

    # ------------------------------------------------------------------------------------------- dispatch
    def decide(self, org_id: Any, actor_id: Any, actor_role: str, request_id: Any, body) -> Dict[str, Any]:
        actor = _uuid(actor_id)
        group, r = self._lock_request(org_id, request_id)
        if r.status == PENDING:
            reason, occ = self.closure_reason(r, group)
            if reason and reason != "LESSON_OUTSIDE_WINDOW":
                self.close(r, reason, trigger="decide", group=group)
                self.db.commit()
                raise self.already_decided(r)
            if occ and (occ.get("startTime") != r.start_time or occ.get("endTime") != r.end_time):
                self._sync_times(r, occ)           # the lesson was moved in the schedule: reschedule / cancel judge the CURRENT time, not the one of the request
        if r.status != PENDING:
            if self._same_decision(r, actor, body):
                item = self.one(r)
                self.db.commit()
                return {"item": item, "result": self._result_of(r)}
            item_error = self.already_decided(r)
            self.db.rollback()
            raise item_error
        try:
            if body.decision == "substitute":
                result = self._substitute(org_id, group, r, actor, actor_role, body)
            elif body.decision == "reject":
                result = self._reject(r, actor, body)
            elif body.decision == "cancel":
                result = self._cancel(group, r, actor, body)
            else:
                result = self._reschedule(group, r, actor, body)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return {"item": self.one(r), "result": result}

    # ---------------------------------------------------------------------------------------------- helpers
    def _finish(self, r: Req, status: str, actor: UUID, note: Optional[str], via: str = "request") -> None:
        now = self.now()
        r.status, r.decided_by, r.decided_at, r.decided_via, r.decision_note, r.updated_at = status, actor, now, via, note, now

    @staticmethod
    def _reason(body, low: int, high: int) -> str:
        reason = (body.reason or "").strip()
        if len(reason) < low:
            raise R.err(422, "REASON_REQUIRED")
        if len(reason) > high:
            raise unprocessable("REASON_TOO_LONG", f"Причина не длиннее {high} символов")
        return reason

    def _same_decision(self, r: Req, actor: UUID, body) -> bool:
        if STATUS_OF.get(body.decision) != r.status or r.decided_by != actor or r.decided_via != "request":
            return False
        if body.decision == "substitute":
            return _uuid(body.teacherId) == r.substitute_id
        if body.decision in ("cancel", "reject"):
            return (body.reason or "").strip() == (r.decision_note or "")
        return (body.newDate.isoformat() if body.newDate else None, body.startTime, body.endTime) == (r.moved_to_date, r.moved_to_start, r.moved_to_end)

    def _result_of(self, r: Req) -> Dict[str, Any]:
        if r.status == "substituted":
            user = self.db.query(User).filter(User.id == r.substitute_id).first()
            return {"kind": "substitute", "teacherId": _s(r.substitute_id), "teacherName": short_name(user), "isSubstitution": True,
                    "attendanceUpdated": False, "payrollSheetsAffected": []}
        if r.status == "cancelled":
            return {"kind": "cancel", "version": 1}
        if r.status == "rescheduled":
            return {"kind": "reschedule", "cancelledVersion": 1, "warnings": [],
                    "newLesson": {"id": r.moved_to_lesson_id, "groupId": str(r.group_id), "date": r.moved_to_date, "startTime": r.moved_to_start,
                                  "endTime": r.moved_to_end, "room": None, "format": "offline"}}
        return {"kind": "reject"}

    # -------------------------------------------------------------------------------------------- substitute
    def _substitute(self, org_id: Any, group: Group, r: Req, actor: UUID, actor_role: str, body) -> Dict[str, Any]:
        from .ops_schemas import LessonTeacherPut

        teacher = _uuid(body.teacherId)
        if teacher is None:
            raise R.err(422, "TEACHER_REQUIRED")
        if teacher == r.requester_id:
            raise R.err(422, "SUBSTITUTE_IS_REQUESTER")
        note = (body.note or "").strip() or None
        reason = (note or REASON_LABELS[r.reason_code])[:300]       # the admin's note; the teacher's own comment stays in the request only
        put = LessonTeacherPut(lessonDate=date.fromisoformat(r.lesson_date), lessonId=r.lesson_id, teacherId=str(teacher), reason=reason)
        out = self.teachers._put_locked(org_id, group, put, actor_id=actor, actor_role=actor_role, commit=False, skip_request_id=r.id)
        asg = self.teachers.last_assignment
        r.substitute_id = teacher
        r.assignment_id = asg.id if asg is not None else None
        self._finish(r, "substituted", actor, note)
        self.event(r, "substituted", actor, {"teacherId": [str(r.requester_id), str(teacher)], "assignmentId": _s(r.assignment_id), "via": "request"})
        return {"kind": "substitute", "teacherId": out["teacherId"], "teacherName": out["teacherName"], "isSubstitution": out["isSubstitution"],
                "attendanceUpdated": out["attendanceUpdated"], "payrollSheetsAffected": out["payrollSheetsAffected"]}

    # ---------------------------------------------------------------------------------------------- reject
    def _reject(self, r: Req, actor: UUID, body) -> Dict[str, Any]:
        reason = self._reason(body, 3, 300)
        self._finish(r, "rejected", actor, reason)
        self.event(r, "rejected", actor, {"reason": reason})
        return {"kind": "reject"}

    # ------------------------------------------------------------------------------------- exam (fail-closed)
    def _exam_check(self, r: Req, reason: str) -> None:
        try:
            self.svc._sync_exam_lesson_cancellation(phase="cancellation-check", org_id=str(r.org_id), group_id=str(r.group_id), lesson_id=r.lesson_id,
                                                    lesson_date=r.lesson_date, reason=reason)
        except RuntimeError as exc:
            raise R.err(503, "EXAM_SERVICE_UNAVAILABLE", "Не удалось проверить экзамены группы. Повторите попытку.") from exc
        except ValueError as exc:
            raise conflict("LESSON_HAS_LIVE_EXAM", str(exc)) from exc

    def _exam_release(self, r: Req, reason: str) -> None:
        try:
            self.svc._sync_exam_lesson_cancellation(phase="release", org_id=str(r.org_id), group_id=str(r.group_id), lesson_id=r.lesson_id,
                                                    lesson_date=r.lesson_date, reason=reason)
        except RuntimeError as exc:
            raise R.err(503, "EXAM_SERVICE_UNAVAILABLE", "Не удалось проверить экзамены группы. Повторите попытку.") from exc
        except ValueError as exc:
            raise conflict("LESSON_HAS_LIVE_EXAM", str(exc)) from exc

    def _cancelled_row(self, group: Group, r: Req, actor: UUID, text: str, details: Dict[str, Any]) -> GroupAttendance:
        day = date.fromisoformat(r.lesson_date)
        occ = next((o for o in self.svc._resolved_occurrences_for_group(group, date_from=day, date_to=day) if o["id"] == r.lesson_id), None)
        start = hhmm_to_minutes(r.start_time)
        past = r.starts_at <= self.now()
        key = (str(group.id), r.lesson_id, r.lesson_date)
        skip = self.db.info.setdefault("b2_skip", set())
        skip.add(key)
        try:
            row = GroupAttendance(
                org_id=group.org_id, group_id=group.id, teacher_id=self.svc._attendance_teacher_snapshot(group, r.lesson_id, day, occ),
                lesson_date=r.lesson_date, lesson_id=r.lesson_id, topic=(occ.get("topic") or occ.get("subject")) if occ else None, status="cancelled",
                records={}, roster_snapshot=self.svc._attendance_roster_at(group.id, bc.lesson_instant_utc(day, start)), marked_by=actor,
                started_at=None, cancellation_reason=text, version=1,
            )
            self.db.add(row)
            self.db.flush()
            self.db.add(GroupAttendanceAudit(
                attendance_id=row.id, org_id=group.org_id, group_id=group.id, actor_id=actor,
                action="reconciliation_cancelled" if past else "cancelled", before_status=None, after_status="cancelled", before_records=None,
                after_records={}, cancellation_reason=text, details={"substitution_request_id": str(r.id), **details}, created_at=self.now(),
            ))
            self.db.flush()
        finally:
            skip.discard(key)
        # the lesson is gone (cancelled / moved): a substitute assigned to it is released (no «Замена» for a lesson that will not happen)
        asg = (self.db.query(LessonTeacherAssignment)
               .filter(LessonTeacherAssignment.group_id == group.id, LessonTeacherAssignment.lesson_id == r.lesson_id,
                       LessonTeacherAssignment.lesson_date == r.lesson_date, LessonTeacherAssignment.cancelled_at.is_(None)).first())
        if asg is not None:
            self.teachers.cancel_assignment(asg, actor, group=group, reason="Урок отменён или перенесён: замена снята")
        return row

    # ---------------------------------------------------------------------------------------------- cancel
    def _cancel(self, group: Group, r: Req, actor: UUID, body) -> Dict[str, Any]:
        reason = self._reason(body, 3, 500)
        if r.starts_at <= self.now() and date.fromisoformat(r.lesson_date) < bc.month_window_start(bc.business_today()):
            raise R.err(422, "LESSON_OUTSIDE_WINDOW", "Урок старше окна незакрытых уроков: отменить его нельзя — можно только отклонить запрос")
        self._exam_check(r, reason)
        row = self._cancelled_row(group, r, actor, reason, {})
        self._exam_release(r, reason)
        self._finish(r, "cancelled", actor, reason)
        self.event(r, "cancelled", actor, {"reason": reason, "version": row.version})
        return {"kind": "cancel", "version": row.version}

    # ------------------------------------------------------------------------------------------- reschedule
    def _new_slot(self, group: Group, r: Req, body) -> Tuple[date, str, str, int, int]:
        if body.newDate is None or not body.startTime or not body.endTime:
            raise R.err(422, "MOVE_FIELDS_REQUIRED")
        try:
            start_m, end_m = self.svc._validate_lesson_time_range(body.startTime, body.endTime)
        except ValueError as exc:
            raise R.err(422, "MOVE_FIELDS_REQUIRED", str(exc)) from exc
        return body.newDate, body.startTime, body.endTime, start_m, end_m

    def conflicts_for_move(self, group: Group, r: Req, new_date: date, start_m: int, end_m: int, start: str, end: str):
        """``(teacher, teacher conflicts, group conflicts)`` of putting the lesson at the new slot."""
        iso = new_date.isoformat()
        book = TeacherBook(self.db)
        teacher_id = book.default_teacher(group, iso, end_m)
        orig_key = (str(group.id), r.lesson_id, r.lesson_date)
        cand = BusyItem(iso, start_m, end_m, start, end, str(group.id), group.name, "__new__")
        t_conf: List[Dict[str, Any]] = []
        if teacher_id is not None:
            t_conf = self.teachers.teacher_conflicts(group.org_id, teacher_id, [cand], ignore={orig_key}, exclude_groups={str(group.id)}, book=book)   # the group's own lessons: GROUP_BUSY below
        g_conf: List[Dict[str, Any]] = []
        rows = {(rw.lesson_id): rw for rw in self.db.query(GroupAttendance).filter(GroupAttendance.group_id == group.id, GroupAttendance.lesson_date == iso)}
        for occ in self.svc._resolved_occurrences_for_group(group, date_from=new_date, date_to=new_date):
            if (str(group.id), occ["id"], iso) == orig_key:
                continue
            row = rows.get(occ["id"])
            if row is not None and row.status == "cancelled":
                continue
            try:
                s2, e2 = hhmm_to_minutes(occ["startTime"]), hhmm_to_minutes(occ["endTime"])
            except (KeyError, ValueError, AttributeError):
                continue
            if start_m < e2 and end_m > s2:
                g_conf.append({"date": iso, "startTime": occ["startTime"], "endTime": occ["endTime"], "groupId": str(group.id),
                               "groupName": group.name, "withGroupName": group.name})
        return teacher_id, t_conf, g_conf

    def move_check(self, org_id: Any, request_id: Any, new_date: date, start: str, end: str) -> Dict[str, Any]:
        r = self._request(org_id, request_id)
        group = self.db.query(Group).filter(Group.id == r.group_id).first()
        return self._move_check(group, r, new_date, start, end)

    def _move_check(self, group: Group, r: Any, new_date: date, start: str, end: str) -> Dict[str, Any]:
        """``r`` is a request or a lesson reference (``org_id, group_id, lesson_id, lesson_date``)."""
        try:
            start_m, end_m = self.svc._validate_lesson_time_range(start, end)
        except ValueError as exc:
            raise R.err(422, "MOVE_FIELDS_REQUIRED", str(exc)) from exc
        teacher_id, t_conf, g_conf = self.conflicts_for_move(group, r, new_date, start_m, end_m, start, end)
        user = self.db.query(User).filter(User.id == teacher_id).first() if teacher_id else None
        occ = next((o for o in self.svc._resolved_occurrences_for_group(
            group, date_from=date.fromisoformat(r.lesson_date), date_to=date.fromisoformat(r.lesson_date)) if o["id"] == r.lesson_id), None)
        from .schedule_rules import ScheduleRules

        warnings = ScheduleRules(self.svc).warnings_for(group, "__new__", [new_date], {"startTime": start, "endTime": end, "room": occ.get("room") if occ else None,
                                                                                          "format": occ.get("format") if occ else None})
        return {"ok": not t_conf and not g_conf, "teacherId": _s(teacher_id), "teacherName": short_name(user), "teacherConflicts": t_conf[:20],
                "groupConflicts": g_conf[:20], "warnings": warnings}

    def _reschedule(self, group: Group, r: Req, actor: UUID, body) -> Dict[str, Any]:
        reason = self._reason(body, 3, 500)
        now, today = self.now(), bc.business_today()
        if r.starts_at <= now:
            raise R.err(409, "LESSON_ALREADY_STARTED", MOVE_STARTED_TEXT)
        new_date, start, end, start_m, end_m = self._new_slot(group, r, body)
        new_start = bc.lesson_instant_utc(new_date, start_m)
        if new_start <= now:
            raise R.err(422, "MOVE_IN_PAST")
        if new_date > today + timedelta(days=R.MOVE_MAX_DAYS_AHEAD):
            raise R.err(422, "MOVE_TOO_FAR")
        if new_date.isoformat() == r.lesson_date and start == r.start_time and end == r.end_time:
            raise R.err(422, "MOVE_SAME_SLOT")
        target = TeacherBook(self.db).default_teacher(group, new_date.isoformat(), end_m)
        if target is not None:
            # R3 #5: the teacher of the NEW slot is locked (the m1 pattern of PUT lesson-teacher) before his conflicts are read, so two moves of two groups of the same
            # teacher into one slot serialise: the second one sees the first one's lesson.  Lock order: group -> request -> User (SPEC B2, no cycle with the substitute path).
            self.db.query(User.id).filter(User.id == target).with_for_update(key_share=True).first()      # FOR NO KEY UPDATE: two moves serialise, but the FK key-share of the request row (requester_id) taken before does not deadlock them
        teacher_id, t_conf, g_conf = self.conflicts_for_move(group, r, new_date, start_m, end_m, start, end)
        if t_conf:
            user = self.db.query(User).filter(User.id == teacher_id).first()
            self.teachers.raise_if_busy(t_conf, short_name(user))
        if g_conf:
            raise R.err(409, "GROUP_BUSY", conflicts=g_conf[:20], total=len(g_conf))
        day = date.fromisoformat(r.lesson_date)
        occ = next((o for o in self.svc._resolved_occurrences_for_group(group, date_from=day, date_to=day) if o["id"] == r.lesson_id), None) or {}
        room = normalize_room(body.room) if body.room is not None else None
        fmt = body.format or occ.get("format") or "offline"
        text = f"Перенесён на {new_date.strftime('%d.%m')}, {start}–{end}: {reason}"[:500]
        payload = {"dayOfWeek": new_date.isoweekday(), "startTime": start, "endTime": end, "subject": "Математика", "room": room, "format": fmt,
                   "color": occ.get("color") or "blue", "topic": occ.get("topic")}
        try:
            self.svc._validate_schedule_payload(payload)         # the same rules as every one-off lesson (format, day, time range)
        except ValueError as exc:
            raise unprocessable("SCHEDULE_PAYLOAD_INVALID", str(exc)) from exc
        self._exam_check(r, text)
        row = self._cancelled_row(group, r, actor, text, {"moved_to": {"date": new_date.isoformat(), "startTime": start, "endTime": end}})
        new_id = f"one-off-{uuid4().hex[:16]}"
        self.db.add(GroupScheduleOneOffLesson(id=new_id, org_id=group.org_id, group_id=group.id, lesson_date=new_date.isoformat(), payload=payload, created_by=actor))
        self.db.flush()
        self._exam_release(r, text)
        from .schedule_rules import ScheduleRules

        warnings = ScheduleRules(self.svc).warnings_for(group, new_id, [new_date], payload)
        r.moved_to_lesson_id, r.moved_to_date, r.moved_to_start, r.moved_to_end = new_id, new_date.isoformat(), start, end
        self._finish(r, "rescheduled", actor, reason)
        self.event(r, "rescheduled", actor, {"reason": reason, "movedTo": {"lessonId": new_id, "date": new_date.isoformat(), "startTime": start, "endTime": end},
                                              "cancelledVersion": row.version})
        return {"kind": "reschedule", "cancelledVersion": row.version, "warnings": warnings,
                "newLesson": {"id": new_id, "groupId": str(group.id), "date": new_date.isoformat(), "startTime": start, "endTime": end, "room": room, "format": fmt}}

    # ------------------------------------------------------------------------- #124: a move without a request
    def _lesson_ref(self, group: Group, lesson_id: str, day: date, *, locked: bool = False) -> Tuple[SimpleNamespace, Dict[str, Any]]:
        """A reference to the lesson of the schedule (the fields of a request that the shared code reads) and its occurrence; 404 ``LESSON_NOT_FOUND``."""
        occ = next((o for o in self.svc._resolved_occurrences_for_group(group, date_from=day, date_to=day) if o["id"] == lesson_id), None)
        if occ is None:
            raise not_found("LESSON_NOT_FOUND", "Урок не найден")
        start_m = hhmm_to_minutes(occ["startTime"])
        ref = SimpleNamespace(id=None, org_id=group.org_id, group_id=group.id, lesson_id=lesson_id, lesson_date=day.isoformat(), start_time=occ["startTime"],
                              end_time=occ["endTime"], starts_at=bc.lesson_instant_utc(day, start_m))
        return ref, occ

    def lesson_move_check(self, org_id: Any, group_id: Any, lesson_id: str, day: date, new_date: date, start: str, end: str) -> Dict[str, Any]:
        gid = _uuid(group_id)
        group = self.db.query(Group).filter(Group.id == gid, Group.org_id == org_id).first() if gid else None
        if group is None:
            raise not_found("GROUP_NOT_FOUND", "Группа не найдена")
        refusal = self._origin_refusal(group, lesson_id, day)
        if refusal is not None:
            if refusal.status_code == 404:
                raise refusal
            return {"ok": False, "teacherId": None, "teacherName": None, "teacherConflicts": [], "groupConflicts": [], "warnings": [], "code": refusal.code,
                    "message": refusal.message}
        ref, _ = self._lesson_ref(group, lesson_id, day)
        return self._move_check(group, ref, new_date, start, end)

    def _origin_refusal(self, group: Group, lesson_id: str, day: date) -> Optional[OpsError]:
        """R3 #6: the refusal POST ``lesson-move`` would give for the ORIGIN lesson (same checks, same order, same codes and texts), without writing or locking anything:
        a pending request that would be closed / a lesson that has started -> the decided / started refusals; a decided move; an archived group; no such lesson (404);
        a cancelled lesson; a journal row (the lesson has started); the planned start passed; a lesson without a teacher.  ``None`` = the lesson can be moved."""
        iso = day.isoformat()
        now = self.now()
        reqs = (self.db.query(Req).filter(Req.group_id == group.id, Req.lesson_id == lesson_id, Req.lesson_date == iso)
                .order_by(Req.created_at.desc(), Req.id).all())
        pending = next((r for r in reqs if r.status == PENDING), None)
        if pending is not None:             # the decision path: ``decide`` revalidates, then ``_reschedule`` refuses a lesson that has started
            reason, occ = self.closure_reason(pending, group)
            if reason and reason != "LESSON_OUTSIDE_WINDOW":
                text = CLOSED_REASON_TEXT.get(reason)
                return R.err(409, "SUBSTITUTION_ALREADY_DECIDED", f"Запрос закрыт: {text}" if text else None)
            start = (occ or {}).get("startTime") or pending.start_time
            if bc.lesson_instant_utc(day, hhmm_to_minutes(start)) <= now:
                return R.err(409, "LESSON_ALREADY_STARTED", MOVE_STARTED_TEXT)
            return None
        done = next((r for r in reqs if r.status == "rescheduled"), None)
        if done is not None:
            return self.already_decided(done)
        if group.status != "active":
            return conflict("GROUP_ARCHIVED", "Группа в архиве: действие недоступно")
        try:
            ref, occ = self._lesson_ref(group, lesson_id, day)
        except OpsError as exc:
            return exc
        row = self._row(ref)
        if row is not None:
            if row.status == "cancelled":
                return conflict("LESSON_CANCELLED", "Урок отменён")
            return R.err(409, "LESSON_ALREADY_STARTED", MOVE_STARTED_TEXT)
        if ref.starts_at <= now:
            return R.err(409, "LESSON_ALREADY_STARTED", MOVE_STARTED_TEXT)
        teacher = TeacherBook(self.db).resolve(group, lesson_id, iso, hhmm_to_minutes(occ["endTime"])).teacher_id
        reason, _ = self.closure_reason(SimpleNamespace(**{**ref.__dict__, "requester_id": teacher}), group)
        if teacher is None or reason in ("TEACHER_REMOVED", "GROUP_TEACHER_CHANGED"):
            return R.err(422, "LESSON_HAS_NO_TEACHER")
        return None

    def lesson_exam_binding(self, org_id: Any, group_id: Any, lesson_id: str, day: date) -> Dict[str, Any]:
        """The precheck of «Отменить урок» for the lesson sheet: ``state`` / ``title`` as ``examBinding`` of the request detail; ``canCancel`` / ``message`` from the SAME
        ``cancellation-check`` of exam-service that refuses the cancellation itself (an exam that is launched OR has attempts blocks it), so what is allowed here is not refused later."""
        gid = _uuid(group_id)
        group = self.db.query(Group).filter(Group.id == gid, Group.org_id == org_id).first() if gid else None
        if group is None:
            raise not_found("GROUP_NOT_FOUND", "Группа не найдена")
        ref, _ = self._lesson_ref(group, lesson_id, day)
        out = dict(self.exam_binding(ref), message=None)
        if out["state"] == "unavailable":
            out["message"] = "Не удалось проверить экзамены группы. Повторите попытку."
            return out
        try:
            self._exam_check(ref, "precheck")
        except OpsError as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            out["canCancel"] = False
            out["message"] = detail.get("message") or str(exc)
            if detail.get("code") == "EXAM_SERVICE_UNAVAILABLE":
                out["state"] = "unavailable"
        return out

    def move_lesson(self, org_id: Any, actor_id: Any, actor_role: str, group_id: Any, body) -> Dict[str, Any]:
        """The administrator moves a future lesson without a teacher's request.  One path with the decision «Перенести урок»: a pending request of the lesson is decided
        by ``decide``; without a request a system record of the move is made (origin ``system``, the teacher of the lesson as requester) and decided in the SAME
        transaction, so the audit, the label of the student, the neutrality of package A and the payroll follow without a second code path."""
        actor, gid = _uuid(actor_id), _uuid(group_id)
        group = self._group_locked(org_id, gid) if gid else None
        if group is None:
            self.db.rollback()
            raise not_found("GROUP_NOT_FOUND", "Группа не найдена")
        day = body.lessonDate
        iso = day.isoformat()
        one = SimpleNamespace(decision="reschedule", reason=body.reason, newDate=body.newDate, startTime=body.startTime, endTime=body.endTime, room=body.room,
                              format=body.format, teacherId=None, note=None, expectedStatus=None)
        try:
            reqs = (self.db.query(Req).filter(Req.group_id == group.id, Req.lesson_id == body.lessonId, Req.lesson_date == iso)
                    .order_by(Req.created_at.desc(), Req.id).all())
            pending = next((r for r in reqs if r.status == PENDING), None)
            if pending is not None:
                # R3 #1: the sheet's expected time is judged against the CURRENT lesson of the schedule (the request keeps the old time until it is re-synced);
                # a lesson that no longer exists is closed by ``decide`` itself (its revalidation), so the request's own times are only the fallback
                now_occ = next((o for o in self.svc._resolved_occurrences_for_group(group, date_from=day, date_to=day) if o["id"] == body.lessonId), None)
                self._check_expected(now_occ["startTime"] if now_occ else pending.start_time, now_occ["endTime"] if now_occ else pending.end_time, body)
                return self.decide(org_id, actor, actor_role, pending.id, one)
            done = next((r for r in reqs if r.status == "rescheduled"), None)
            if done is not None:
                if self._same_decision(done, actor, one):
                    item = self.one(done)
                    self.db.commit()
                    return {"item": item, "result": self._result_of(done)}
                raise self.already_decided(done)
            if group.status != "active":
                raise conflict("GROUP_ARCHIVED", "Группа в архиве: действие недоступно")
            ref, occ = self._lesson_ref(group, body.lessonId, day)
            row = self.db.query(GroupAttendance).filter(GroupAttendance.group_id == group.id, GroupAttendance.lesson_id == body.lessonId,
                                                        GroupAttendance.lesson_date == iso).with_for_update().first()
            if row is not None:
                if row.status == "cancelled":
                    raise conflict("LESSON_CANCELLED", "Урок отменён")
                raise R.err(409, "LESSON_ALREADY_STARTED", MOVE_STARTED_TEXT)
            now = self.now()
            if ref.starts_at <= now:
                raise R.err(409, "LESSON_ALREADY_STARTED", MOVE_STARTED_TEXT)
            self._check_expected(ref.start_time, ref.end_time, body)
            teacher = TeacherBook(self.db).resolve(group, body.lessonId, iso, hhmm_to_minutes(occ["endTime"])).teacher_id
            reason, _ = self.closure_reason(SimpleNamespace(**{**ref.__dict__, "requester_id": teacher}), group)
            if teacher is None or reason in ("TEACHER_REMOVED", "GROUP_TEACHER_CHANGED"):
                raise R.err(422, "LESSON_HAS_NO_TEACHER")
            prev = reqs[0] if reqs else None
            r = Req(org_id=org_id, group_id=group.id, lesson_id=body.lessonId, lesson_date=iso, start_time=ref.start_time, end_time=ref.end_time, starts_at=ref.starts_at,
                    requester_id=teacher, origin="system", batch_id=uuid4(), reason_code="other", comment=(body.reason or "").strip()[:500] or None, status=PENDING,
                    previous_request_id=prev.id if prev is not None else None, created_at=now, updated_at=now)
            self.db.add(r)
            self.db.flush()
            self.event(r, "requested", actor, {"reasonCode": "other", "trigger": "manager_move", "batchId": str(r.batch_id)})
        except BaseException:
            self.db.rollback()
            raise
        return self.decide(org_id, actor, actor_role, r.id, one)

    @staticmethod
    def _check_expected(start: str, end: str, body) -> None:
        if (body.expectedStartTime and body.expectedStartTime != start) or (body.expectedEndTime and body.expectedEndTime != end):
            raise R.err(409, "LESSON_CHANGED", current={"startTime": start, "endTime": end})

    # ------------------------------------------------------------------------------------------------- bulk
    def decide_bulk(self, org_id: Any, actor_id: Any, actor_role: str, body) -> Dict[str, Any]:
        rows = self.db.query(Req).filter(Req.org_id == org_id, Req.id.in_([u for u in (_uuid(i) for i in body.ids) if u])).order_by(Req.starts_at, Req.id).all()
        order = [str(r.id) for r in rows] + [i for i in body.ids if i not in {str(r.id) for r in rows}]
        updated, items, skipped = 0, [], []
        one = DecideRequestLike(body)
        stopped: Optional[OpsError] = None
        for rid in order:
            if stopped is not None:         # exam-service is down: the rest would only repeat the failure (and each try waits for it)
                skipped.append({"id": str(rid), "code": stopped.code, "message": stopped.message})
                continue
            try:
                out = self.decide(org_id, actor_id, actor_role, rid, one)
                updated += 1
                items.append(out["item"])
            except OpsError as exc:
                self.db.rollback()
                entry = {"id": str(rid), "code": exc.code, "message": exc.message}
                if exc.code == "EXAM_SERVICE_UNAVAILABLE":
                    stopped = exc
                for key in ("conflicts", "total"):
                    if key in exc.extra:
                        entry[key] = exc.extra[key]
                skipped.append(entry)
        return {"updated": updated, "items": items, "skipped": skipped}


class DecideRequestLike:
    """The bulk body as a ``DecideRequest`` (substitute / cancel / reject only)."""

    def __init__(self, body) -> None:
        self.decision, self.teacherId, self.note, self.reason = body.decision, body.teacherId, body.note, body.reason
        self.newDate = self.startTime = self.endTime = self.room = self.format = None
        self.expectedStatus = None


# ======================================================================================================================
# hooks of the operations of package B / B2 (eager closing, D-9, D-10)
# ======================================================================================================================
def on_lesson_teacher_changed(svc, group: Group, lesson_id: str, lesson_date: str, *, cancelled_asg: Optional[LessonTeacherAssignment],
                              new_asg: Optional[LessonTeacherAssignment], want: Any, actor_id: Any, skip_request_id: Any = None) -> Optional[Dict[str, Any]]:
    """Called by ``PUT lesson-teacher`` (and by a decision, with ``skip_request_id``): a pending request of the lesson that the new teacher takes over is decided
    ``substituted`` (``decidedVia=lesson_teacher_put``); a request substituted through the assignment that has just been cancelled gets the event ``assignment_cleared``."""
    subs = Substitutions(svc)
    closed = None
    pending = svc.db.query(Req).filter(Req.group_id == group.id, Req.lesson_id == lesson_id, Req.lesson_date == lesson_date, Req.status == PENDING).first()
    if pending is not None and pending.id != skip_request_id and want is not None and want != pending.requester_id:
        now = bc.utc_now()
        pending.status, pending.substitute_id, pending.assignment_id = "substituted", want, new_asg.id if new_asg is not None else None
        pending.decided_by, pending.decided_at, pending.decided_via, pending.updated_at = _uuid(actor_id), now, "lesson_teacher_put", now
        subs.event(pending, "substituted", actor_id, {"teacherId": [str(pending.requester_id), str(want)], "assignmentId": _s(pending.assignment_id),
                                                      "via": "lesson_teacher_put"})
        closed = {"id": str(pending.id), "status": "substituted"}
    if cancelled_asg is not None:
        for sr in svc.db.query(Req).filter(Req.assignment_id == cancelled_asg.id, Req.status == "substituted").all():
            if sr.id != skip_request_id:
                subs.event(sr, "assignment_cleared", actor_id, {"teacherId": [str(cancelled_asg.teacher_id), _s(want)]})
    return closed


def on_assignment_cancelled(svc, asg: LessonTeacherAssignment, actor_id: Any) -> int:
    """An assignment was taken off by the operations of B (the group teacher became the substitute, the substitute was removed): every request that was substituted
    through it keeps its status and gets one event ``assignment_cleared``."""
    subs = Substitutions(svc)
    rows = svc.db.query(Req).filter(Req.assignment_id == asg.id, Req.status == "substituted").all()
    for r in rows:
        subs.event(r, "assignment_cleared", actor_id, {"teacherId": [str(asg.teacher_id), None], "trigger": "release"})
    return len(rows)


def on_group_teacher_changed(svc, group: Group, actor_id: Any = None, trigger: str = "group_teacher_changed") -> int:
    """After the teacher of a group changed (``update_group``, handover, restore): the pending requests whose requester no longer leads the lesson are closed
    ``GROUP_TEACHER_CHANGED`` in the same transaction (the caller commits)."""
    subs = Substitutions(svc)
    svc.db.flush()
    book = TeacherBook(svc.db)
    closed = 0
    for r in svc.db.query(Req).filter(Req.group_id == group.id, Req.status == PENDING).all():
        reason, _occ = subs.closure_reason(r, group, book)
        if reason == "GROUP_TEACHER_CHANGED":
            subs.close(r, reason, actor_id=actor_id, trigger=trigger, group=group)
            closed += 1
    return closed


def close_group_requests(svc, group: Group, reason: str = "GROUP_ARCHIVED", actor_id: Any = None, trigger: str = "group_archived") -> int:
    subs = Substitutions(svc)
    reqs = svc.db.query(Req).filter(Req.group_id == group.id, Req.status == PENDING).all()
    for r in reqs:
        subs.close(r, reason, actor_id=actor_id, trigger=trigger, group=None)
    return len(reqs)


def count_pending_of_group(db, group_id: Any) -> int:
    return db.query(Req).filter(Req.group_id == group_id, Req.status == PENDING).count()


def count_pending_of_teacher(db, org_id: Any, user_id: Any) -> int:
    return db.query(Req).filter(Req.org_id == org_id, Req.requester_id == user_id, Req.status == PENDING).count()


def on_member_removed(svc, org_id: Any, user_id: UUID, actor_id: Any, released: Sequence[Tuple[LessonTeacherAssignment, Group]]) -> Tuple[int, int]:
    """After a member was removed (the caller commits): his pending requests are closed ``TEACHER_REMOVED``; the requests whose assignment (HIM as the
    substitute) was just released are reopened as new system requests when the requester still leads the lesson (D-10); the requests that suggested him get one
    ``suggestion_unavailable`` event.  Returns ``(closed, reopened)``."""
    subs = Substitutions(svc)
    db = svc.db
    now = bc.utc_now()
    closed = 0
    for r in db.query(Req).filter(Req.org_id == org_id, Req.requester_id == user_id, Req.status == PENDING).all():
        subs.close(r, "TEACHER_REMOVED", actor_id=actor_id, trigger="member_removed")
        closed += 1
    for r in db.query(Req).filter(Req.org_id == org_id, Req.suggested_teacher_id == user_id, Req.status == PENDING).all():
        subs.event(r, "suggestion_unavailable", actor_id, {"teacherId": str(user_id)})
    db.flush()      # the sessions of production run with autoflush=False: the closings above must be visible to the "pending already on this lesson?" query
    reopened = 0
    removed_user = db.query(User).filter(User.id == user_id).first()
    book = TeacherBook(db)
    for asg, group in released:
        old = db.query(Req).filter(Req.assignment_id == asg.id, Req.status == "substituted").first()
        if old is None:
            continue
        d = date.fromisoformat(asg.lesson_date)
        occ = next((o for o in svc._resolved_occurrences_for_group(group, date_from=d, date_to=d) if o["id"] == asg.lesson_id), None)
        if occ is None or group.status != "active":
            continue
        end = hhmm_to_minutes(occ["endTime"])
        if book.default_teacher(group, asg.lesson_date, end) != old.requester_id:
            continue                                       # the requester no longer leads the lesson (a handover): nothing to reopen
        starts_at = bc.lesson_instant_utc(d, hhmm_to_minutes(occ["startTime"]))
        if starts_at <= now:
            continue
        if db.query(Req).filter(Req.group_id == group.id, Req.lesson_id == asg.lesson_id, Req.lesson_date == asg.lesson_date, Req.status == PENDING).first():
            continue
        if db.query(GroupAttendance.id).filter(GroupAttendance.group_id == group.id, GroupAttendance.lesson_id == asg.lesson_id,
                                               GroupAttendance.lesson_date == asg.lesson_date, GroupAttendance.status == "cancelled").first():
            continue                                       # the lesson was cancelled / moved meanwhile: nothing to reopen
        new = Req(org_id=org_id, group_id=group.id, lesson_id=asg.lesson_id, lesson_date=asg.lesson_date, start_time=occ["startTime"], end_time=occ["endTime"],
                  starts_at=starts_at, requester_id=old.requester_id, origin="system", batch_id=uuid4(), reason_code="other",
                  comment=f"Заменяющий {short_name(removed_user)} больше не работает в центре. Нужно новое решение.", suggested_teacher_id=None,
                  status=PENDING, previous_request_id=old.id, created_at=now, updated_at=now)
        db.add(new)
        db.flush()
        subs.event(new, "reopened", actor_id, {"previousRequestId": str(old.id), "removedTeacherId": str(user_id)})
        reopened += 1
    return closed, reopened


# ======================================================================================================================
# a new journal row closes the pending request of its lesson (R-5 / R-7: no edit of the four creation sites in service.py)
# ======================================================================================================================
@event.listens_for(Session, "before_flush")
def _close_requests_of_new_rows(session: Session, flush_context, instances) -> None:  # pragma: no cover - exercised through the tests of S9
    new_rows = [o for o in session.new if isinstance(o, GroupAttendance) and o.group_id is not None and o.lesson_date]
    if not new_rows:
        return
    skip = session.info.get("b2_skip") or set()
    now = bc.utc_now()
    for row in new_rows:
        if (str(row.group_id), row.lesson_id, row.lesson_date) in skip:
            continue
        pending = session.query(Req).filter(Req.group_id == row.group_id, Req.lesson_id == row.lesson_id, Req.lesson_date == row.lesson_date,
                                            Req.status == PENDING).all()
        for r in pending:
            reason = "LESSON_CANCELLED" if row.status == "cancelled" else "LESSON_STARTED"
            r.status, r.closed_reason, r.decided_at, r.decided_via, r.decided_by, r.updated_at = "closed", reason, now, "system", None, now
            session.add(LessonSubstitutionEvent(request_id=r.id, org_id=r.org_id, actor_id=row.marked_by, action="closed", created_at=now + timedelta(microseconds=5),
                                                snapshot={"closedReason": reason, "trigger": "journal_row"}))


# ======================================================================================================================
# interface with package A (SPEC 3.5)
# ======================================================================================================================
def moved_one_off_keys(db, group_ids: Iterable[Any]) -> Set[Tuple[str, str]]:
    """``{(group_id, one_off_lesson_id)}`` of the one-off lessons that a decision "reschedule" created.  Package A's ``ledger_service._lesson_days`` leaves them out of the
    counted days (the cancelled original is counted, A decision 3), so a move is neutral for the incomplete first period."""
    ids = [g for g in group_ids if g is not None]
    if not ids:
        return set()
    rows = db.query(Req.group_id, Req.moved_to_lesson_id).filter(Req.group_id.in_(ids), Req.status == "rescheduled", Req.moved_to_lesson_id.isnot(None)).all()
    return {(str(g), lid) for g, lid in rows}


# ======================================================================================================================
# D-1: what exam-service may ask about a substitute
# ======================================================================================================================
def lesson_access(svc, org_id: Any, actor_id: Any, group_id: Any) -> Dict[str, Any]:
    """The group (name, grade) and ONLY the lessons the actor substitutes in (active assignments with ``lesson_date >= today - 7``): nothing about the roster, the journal or
    the other lessons.  403 for anybody who has no such lesson (the group teacher goes through the group page, the managers see every group)."""
    from .ops_rules import teacher_lesson_window_start

    gid, actor = _uuid(group_id), _uuid(actor_id)
    group = svc.db.query(Group).filter(Group.id == gid, Group.org_id == org_id).first() if gid else None
    if group is None:
        raise not_found("GROUP_NOT_FOUND", "Группа не найдена")
    window_from = teacher_lesson_window_start(bc.business_today()).isoformat()
    rows = (
        svc.db.query(LessonTeacherAssignment)
        .filter(LessonTeacherAssignment.group_id == group.id, LessonTeacherAssignment.teacher_id == actor, LessonTeacherAssignment.cancelled_at.is_(None),
                LessonTeacherAssignment.lesson_date >= window_from)
        .order_by(LessonTeacherAssignment.lesson_date, LessonTeacherAssignment.lesson_id)
        .all()
    )
    if not rows:
        raise forbidden("NOT_LESSON_TEACHER", "В этой группе у вас нет замены")
    teacher = svc.db.query(User).filter(User.id == group.teacher_id).first() if group.teacher_id else None
    return {"id": str(group.id), "org_id": str(group.org_id), "name": group.name, "class_level": group.class_level, "teacher_id": _s(group.teacher_id),
            "teacher_name": f"{teacher.first_name or ''} {teacher.last_name or ''}".strip() or None if teacher else None, "status": group.status or "active", "substitution": True,
            "lessons": [{"lessonId": a.lesson_id, "lessonDate": a.lesson_date} for a in rows]}


# ======================================================================================================================
# additive marks of the existing answers (SPEC 4.5)
# ======================================================================================================================
def _moved(r: Req, with_lesson: bool = False) -> Optional[Dict[str, Any]]:
    if not r.moved_to_date:
        return None
    out = {"date": r.moved_to_date, "startTime": r.moved_to_start, "endTime": r.moved_to_end}
    if with_lesson:
        out["lessonId"] = r.moved_to_lesson_id
    return out


def moved_from_map(db, group_ids: Iterable[Any], lesson_ids: Iterable[str]) -> Dict[Tuple[str, str], Dict[str, str]]:
    """``{(group_id, new lesson id): movedFrom}`` for the one-off lessons that a move created (both paths leave a ``rescheduled`` record with ``moved_to_lesson_id``)."""
    gids = [g for g in group_ids if g is not None]
    ids = sorted({i for i in lesson_ids if i and str(i).startswith("one-off-")})
    if not gids or not ids:
        return {}
    rows = db.query(Req).filter(Req.group_id.in_(gids), Req.moved_to_lesson_id.in_(ids), Req.status == "rescheduled").order_by(Req.created_at, Req.id).all()
    return {(str(r.group_id), r.moved_to_lesson_id): {"lessonId": r.lesson_id, "date": r.lesson_date, "startTime": r.start_time, "endTime": r.end_time} for r in rows}


def annotate_occurrences(svc, user: Dict[str, Any], items: Sequence[Any]) -> Sequence[Any]:
    """``substitutionRequest`` (manager and the requester) and ``substitutionOutcome`` (everybody who sees the schedule) on the occurrences of ``GET /org/schedule/occurrences``."""
    if not items:
        return items
    from .attendance_access import is_org_manager

    manager, actor = is_org_manager(user), _uuid(user.get("user_id"))
    gids = {g for g in (_uuid(i.groupId) for i in items) if g}
    origin = moved_from_map(svc.db, gids, [i.id for i in items])
    for item in items:
        item.movedFrom = origin.get((str(item.groupId), item.id))
    days = [i.occurrenceDate for i in items]
    reqs = (
        svc.db.query(Req).filter(Req.group_id.in_(gids), Req.lesson_date >= min(days), Req.lesson_date <= max(days)).order_by(Req.created_at, Req.id).all()
    )
    by_key: Dict[Tuple[str, str, str], List[Req]] = {}
    for r in reqs:
        by_key.setdefault((str(r.group_id), r.lesson_id, r.lesson_date), []).append(r)
    for item in items:
        group = by_key.get((str(item.groupId), item.id, item.occurrenceDate), [])
        if not group:
            continue
        visible = [r for r in group if manager or r.requester_id == actor]
        shown = next((r for r in reversed(visible) if r.status == PENDING), None) or next(
            (r for r in reversed(visible) if r.status in ("substituted", "rescheduled", "cancelled", "rejected")), None)
        if shown is not None:
            item.substitutionRequest = {"id": str(shown.id), "status": shown.status, "reasonCode": shown.reason_code}
        done = next((r for r in reversed(group) if r.status in ("rescheduled", "cancelled")), None)
        if done is not None:
            item.substitutionOutcome = {"kind": "rescheduled" if done.status == "rescheduled" else "cancelled", "movedTo": _moved(done)}
    return items


def annotate_journal(svc, user: Dict[str, Any], group: Group, resp) -> Any:
    """``substitutionRequests`` of the journal: ``{<date>_<lessonId>: {id, status, reasonCode, teacherName}}`` for the manager and for the requester's own requests."""
    from .attendance_access import is_org_manager

    keys = {l.get("instanceKey"): l for l in resp.lessons if l.get("instanceKey")}
    if not keys:
        return resp
    origin = moved_from_map(svc.db, [group.id], [l.get("id") for l in keys.values()])
    for lesson in keys.values():
        if (str(group.id), lesson.get("id")) in origin:
            lesson["movedFrom"] = origin[(str(group.id), lesson.get("id"))]
    manager, actor = is_org_manager(user), _uuid(user.get("user_id"))
    dates = sorted({l["date"] for l in keys.values()})
    reqs = svc.db.query(Req).filter(Req.group_id == group.id, Req.lesson_date >= dates[0], Req.lesson_date <= dates[-1]).order_by(Req.created_at, Req.id).all()
    marks: Dict[str, Dict[str, Any]] = {}
    names = SubstitutionsBase(svc).teachers.users_by_id([r.requester_id for r in reqs])
    for r in reqs:
        key = f"{r.lesson_date}_{r.lesson_id}"
        if key not in keys or not (manager or r.requester_id == actor):
            continue
        if r.status in ("withdrawn", "closed"):
            continue
        if key in marks and marks[key]["status"] == PENDING and r.status != PENDING:
            continue
        marks[key] = {"id": str(r.id), "status": r.status, "reasonCode": r.reason_code, "teacherName": short_name(names.get(str(r.requester_id)))}
    resp.substitutionRequests = marks
    # the outcome of a cancelled / moved lesson on the lesson itself (the shape of ``schedule/occurrences``): the journal needs no parsing of cancellation reasons;
    # visible to everybody who reads the journal (no reason, no comment - D-12)
    done: Dict[str, Req] = {}
    for r in reqs:
        if r.status in ("rescheduled", "cancelled"):
            done[f"{r.lesson_date}_{r.lesson_id}"] = r
    for key, lesson in keys.items():
        r = done.get(key)
        if r is not None:
            lesson["substitutionOutcome"] = {"kind": "rescheduled" if r.status == "rescheduled" else "cancelled", "movedTo": _moved(r)}
    return resp


def annotate_student_attendance(svc, org_id: Any, student_id: Any, summary: Dict[str, Any]) -> Dict[str, Any]:
    """The student card: ``lessons[].teacherName / isSubstitution`` and ``notices[]`` (a cancelled / moved lesson of his groups; no reasons, no comments - D-12)."""
    month = summary["period"]["month"]
    d_from, d_to = summary["period"]["date_from"], summary["period"]["date_to"]
    db = svc.db
    lessons = summary.get("lessons", [])
    if lessons:
        gids = {_uuid(l["group_id"]) for l in lessons}
        rows = {(str(r.group_id), r.lesson_id, r.lesson_date): r for r in db.query(GroupAttendance).filter(
            GroupAttendance.group_id.in_(gids), GroupAttendance.lesson_date >= d_from, GroupAttendance.lesson_date <= d_to)}
        groups = {g.id: g for g in db.query(Group).filter(Group.id.in_(gids)).all()}
        book = TeacherBook(db)
        book.load_history(list(gids))
        names = LessonTeachers(svc).users_by_id([r.teacher_id for r in rows.values() if r.teacher_id])
        origin = moved_from_map(db, gids, [l["lesson_id"] for l in lessons])
        for lesson in lessons:
            if (lesson["group_id"], lesson["lesson_id"]) in origin:
                lesson["movedFrom"] = origin[(lesson["group_id"], lesson["lesson_id"])]
            row = rows.get((lesson["group_id"], lesson["lesson_id"], lesson["date"]))
            group = groups.get(_uuid(lesson["group_id"]))
            teacher = row.teacher_id if row is not None else None
            lesson["teacherName"] = short_name(names.get(str(teacher))) if teacher else None
            default = book.default_teacher(group, lesson["date"], None) if group is not None else None
            lesson["isSubstitution"] = bool(teacher is not None and default is not None and teacher != default)
    sid = str(student_id)
    notices: List[Dict[str, Any]] = []
    own_groups = [gid for (gid,) in db.query(StudentGroup.group_id).filter(StudentGroup.student_id == student_id)]      # only the groups he is / was in
    reqs = (db.query(Req).filter(Req.org_id == org_id, Req.group_id.in_(own_groups), Req.status.in_(("cancelled", "rescheduled")),
                                 Req.lesson_date >= d_from, Req.lesson_date <= d_to).order_by(Req.lesson_date, Req.id).all()) if own_groups else []
    if reqs:
        row_of = {(x.group_id, x.lesson_id, x.lesson_date): x for x in db.query(GroupAttendance).filter(
            GroupAttendance.group_id.in_({r.group_id for r in reqs}), GroupAttendance.lesson_date >= d_from, GroupAttendance.lesson_date <= d_to)}
        group_of = {g.id: g for g in db.query(Group).filter(Group.id.in_({r.group_id for r in reqs})).all()}
        for r in reqs:
            row = row_of.get((r.group_id, r.lesson_id, r.lesson_date))
            snapshot = (row.roster_snapshot if row is not None else None) or []
            if sid not in {str(x.get("student_id")) for x in snapshot if isinstance(x, dict)}:
                continue
            group = group_of.get(r.group_id)
            notices.append({"lessonId": r.lesson_id, "date": r.lesson_date, "groupId": str(r.group_id), "groupName": group.name if group else "",
                            "kind": "rescheduled" if r.status == "rescheduled" else "cancelled", "movedTo": _moved(r, True)})
    summary["notices"] = notices
    return summary
