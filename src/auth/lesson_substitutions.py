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
* P1.1 / P1.2: what the administrator does on a lesson sheet with no request (a move, a substitution) is a ``system`` record decided in the same transaction, so the journal
  "Замены" shows every move and every substitution; taking a substitution off closes its record ``ASSIGNMENT_CLEARED``.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from uuid import UUID, uuid4

from sqlalchemy import event, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from src.models.auth import OrgDayOff

from . import business_clock as bc
from . import exam_bridge, substitution_rules as R
from .days_off import DaysOffService
from .lesson_cancel import LessonCancelMixin, RESTORED, lesson_outcomes
from .lesson_teachers import (
    TEACHING_ROLES,
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
    Student,
    StudentGroup,
    User,
)
from .one_off_purpose import MOVED, OneOffPurpose, apply_marks, copy_fields, one_off_of
from .ops_rules import OpsError, conflict, forbidden, hhmm_to_minutes, normalize_room, not_found, short_name, unprocessable
from .membership_periods import periods_by_membership
from .student_archive import student_not_archived
from .schemas import DEFAULT_CANCELLATION_KIND
from .substitution_schemas import CLOSED_REASON_TEXT, REASON_LABELS

logger = logging.getLogger(__name__)

PENDING = "pending"
KIND = {"substituted": "substitute", "rescheduled": "reschedule", "cancelled": "cancel", "rejected": "reject", "withdrawn": "withdraw", "closed": "closed"}
STATUS_OF = {"substitute": "substituted", "reschedule": "rescheduled", "cancel": "cancelled", "reject": "rejected"}
LIST_DEFAULT_DAYS = 30
MINE_DAYS = 60
SUMMARY_REVALIDATE_MAX = 200                 # W1 R3 13: how many pending requests the badge revalidates at most
CLEARED_DAYS = 14                            # W1 S22: how long a substitute sees «снято» in ``mine``
EXPIRY_WARNING_DAYS = 3                      # W1 S20: «истекают» is shown this many days before the 1st
ASSIGNED_AHEAD_DAYS = 30
TEACHER_MOVE = "teacher_move"                # ``decided_via`` of a lesson moved by the teacher of the lesson himself (P1.1); the administrator's decision is ``request``
ASSIGN_TRIGGER = "manager_assign"           # ``trigger`` of the ``requested`` event of a record the lesson sheet made (P1.2): the only mark of its creation, nothing else is guessed from ``origin`` / ``decided_via``
PUT_VIA = "lesson_teacher_put"               # ``decided_via`` of what the administrator did on the lesson sheet (PUT lesson-teacher): a request it decided, a substitution it set without one (P1.2), one it took off
MOVE_STARTED_TEXT = "Урок уже начался или прошёл: перенести его нельзя"          # the one text of every «the lesson is no longer ahead» refusal of a move
DETAIL_LINKS = 10                            # #157 A5: how many records before / after the one opened the history shows ...
DETAIL_EVENTS = 60                           # ... and how many events (the newest); more = ``truncated: true``
MERGE_SECONDS = 2                            # the two events one act of an administrator writes (the old record's «taken off» and the new one's «substituted») are not further apart than this
MANAGER = "manager"                          # the viewer of an owner / admin; a teacher (the requester) is viewed by his user id: every answer says WHOSE eyes it is built for (#157 A1)

# #157 A1: what an event of the journal may show of its ``snapshot`` (anything else is cut for EVERYBODY - the money and the ids of the machinery never leave; LESSONS 45).  ``_MANAGER_ONLY`` is
# what only an owner / admin sees: the kind of a cancellation and the reason of a restore are the centre's business, not the teacher's.  A new key = a new line here + the test that bounds the keys.
SNAPSHOT_KEYS: Dict[str, Tuple[str, ...]] = {
    "requested": ("reasonCode", "trigger"), "substituted": ("teacherId", "via"), "rescheduled": ("reason", "via", "movedTo"), "cancelled": ("reason", "cancellationKind"),
    "rejected": ("reason",), "withdrawn": (), "closed": ("closedReason", "trigger", "dayOffId", "reason", "cancellationKind", "previous"), "reopened": ("cause", "dayOffId", "byDayOff"),
    "assignment_cleared": ("teacherId", "trigger", "dayOffId", "byDayOff"), "suggestion_unavailable": (), "lesson_restored": ("reason", "previous"),
    "day_off_voided": ("dayOffId", "byDayOff", "reopened", "subject", "needsDecision"),
}
SNAPSHOT_KEYS["closed"] = SNAPSHOT_KEYS["closed"] + ("byDayOff",)
# ``dayOffId`` is the machinery for a teacher: he reads ``byDayOff: true`` instead (derived in ``clean_snapshot``, never stored); ``subject`` / ``needsDecision`` of ``day_off_voided`` are written with the event
_MANAGER_ONLY: Dict[str, Tuple[str, ...]] = {"cancelled": ("cancellationKind",), "closed": ("reason", "cancellationKind", "previous", "dayOffId"), "lesson_restored": ("reason", "previous"),
                                              "reopened": ("dayOffId",), "assignment_cleared": ("dayOffId",), "day_off_voided": ("dayOffId",)}
CLOSED_OUTCOME = {          # #157 A11: ``closed_reason`` -> the code of ``outcome`` (``LESSON_CANCELLED`` is told apart by the closing trigger: a day off)
    "LESSON_OUTSIDE_WINDOW": "not_resolved_in_time", "LESSON_STARTED": "lesson_started", "LESSON_REMOVED": "lesson_removed", "GROUP_ARCHIVED": "group_archived",
    "GROUP_TEACHER_CHANGED": "group_teacher_changed", "TEACHER_REMOVED": "teacher_removed", "ROLE_CHANGED": "role_changed", "ASSIGNMENT_CLEARED": "substitution_cleared",
    "LESSON_RESTORED": "lesson_restored", "KIND_CHANGED": "kind_changed", "ONE_OFF_ADDED": "one_off_added",
}
_PREVIOUS_KEYS = ("cancellationKind", "cancellationReason", "cancelledBy", "cancelledAt")        # the inside of ``closed.previous`` (a restore): what was cancelled, by whom


def _next_event_time(db, request_id: Any, now: datetime) -> datetime:
    """The journal is ordered by ``created_at``: two events of one request in the same instant (one transaction, a frozen test clock) keep their order by a microsecond step."""
    last = db.query(func.max(LessonSubstitutionEvent.created_at)).filter(LessonSubstitutionEvent.request_id == request_id).scalar()
    return last + timedelta(microseconds=1) if last is not None and last >= now else now


def viewer_for(actor: Any, *, teacher: bool) -> Any:
    """THE rule of whose eyes an answer is built for: the teacher's own (``actor``) when a teacher acts, else a manager's."""
    return actor if teacher else MANAGER


class Ctx:
    """Everything the items of one answer need, loaded in batches."""

    def __init__(self, viewer: Any) -> None:
        self.viewer = viewer                                     # ``MANAGER`` or the user id of the teacher the answer is built for (#157 A1): what he may not see is cut in ``item``
        self.groups: Dict[UUID, Group] = {}
        self.users: Dict[str, User] = {}
        self.roles: Dict[str, str] = {}                          # user id -> the org role in THIS centre (owner / admin / teacher): the front names the person by it
        self.counts: Dict[UUID, int] = {}
        self.occ: Dict[Tuple[UUID, str, str], Dict[str, Any]] = {}
        self.previous: Dict[UUID, Req] = {}
        self.active_members: Set[UUID] = set()
        self.triggers: Dict[UUID, str] = {}                      # request id -> the trigger of its ``requested`` event (a record the system made)
        self.cleared: Dict[UUID, LessonSubstitutionEvent] = {}   # request id -> its last ``assignment_cleared`` event
        self.closed_trigger: Dict[UUID, str] = {}                # request id -> the trigger of its last ``closed`` event (#157 A9)
        self.restored: Set[UUID] = set()                         # requests whose lesson was given back (``lesson_restored``, #157 A10)
        self.voided: Set[UUID] = set()                           # requests a day off that was cancelled left behind (``day_off_voided``, #157 A4)
        self.needs_decision: Dict[UUID, bool] = {}               # request id -> ``needsDecision`` of its LAST ``day_off_voided`` (same query as the rest of the events)
        self.last_event: Dict[UUID, datetime] = {}               # request id -> the time of its newest event of any kind (#157 A8)


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
        self.db.flush()             # the sessions run with autoflush off: the next event of this request must see this one to keep the order

    def _person(self, ctx: Ctx, user_id: Any) -> Optional[Dict[str, Any]]:
        if user_id is None:
            return None
        return {"id": str(user_id), "name": short_name(ctx.users.get(str(user_id))), "role": getattr(ctx, "roles", {}).get(str(user_id))}

    def _author(self, ctx: Ctx, user_id: Any) -> Optional[Dict[str, Any]]:
        """The person who DID something (decided, took a substitution off).  A teacher reads another one by the role only (#157 A1): the name and the id of an administrator are not his."""
        person = self._person(ctx, user_id)
        if person is None or ctx.viewer == MANAGER or person["id"] == str(ctx.viewer):
            return person
        return {"id": None, "name": None, "role": person["role"]}

    def roles_of(self, org_ids: Iterable[Any], user_ids: Iterable[Any]) -> Dict[str, str]:
        """``{user id: org role}`` for the people of one answer in ONE query (never one per person)."""
        users, orgs = {u for u in user_ids if u}, {o for o in org_ids if o}
        if not users or not orgs:
            return {}
        rows = self.db.query(OrganizationMember.user_id, OrganizationMember.org_role).filter(OrganizationMember.org_id.in_(orgs), OrganizationMember.user_id.in_(users)).all()
        return {str(uid): role for uid, role in rows}

    # ============================================================================================ item
    def ctx_for(self, reqs: Sequence[Req], viewer: Any) -> Ctx:
        ctx = Ctx(viewer)
        if not reqs:
            return ctx
        gids = {r.group_id for r in reqs}
        ctx.groups = {g.id: g for g in self.db.query(Group).filter(Group.id.in_(gids)).all()}
        prev_ids = {r.previous_request_id for r in reqs if r.previous_request_id}
        if prev_ids:
            ctx.previous = {p.id: p for p in self.db.query(Req).filter(Req.id.in_(prev_ids)).all()}
        self._load_events(ctx, reqs)
        user_ids: Set[Any] = set()
        for r in list(reqs) + list(ctx.previous.values()):
            user_ids.update({r.requester_id, r.suggested_teacher_id, r.decided_by, r.substitute_id})
        for e in ctx.cleared.values():
            user_ids.update({e.actor_id, _uuid(self._cleared_to(e))})
        ctx.users = self.teachers.users_by_id(u for u in user_ids if u)
        ctx.roles = self.roles_of({r.org_id for r in reqs}, user_ids)
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
                .filter(OrganizationMember.user_id.in_(suggested), OrganizationMember.is_active.is_(True), OrganizationMember.org_role.in_(TEACHING_ROLES),
                        User.is_active.isnot(False))          # W1 round 2: «available» = active AND a teacher (one who became an administrator no longer leads lessons)
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

    def _load_events(self, ctx: Ctx, reqs: Sequence[Req]) -> None:
        """ONE query for the page: how a record the system made was created and, for a substitution that stopped, the last ``assignment_cleared`` (when, by whom, to whom, why)."""
        ctx.last_event = dict(self.db.query(LessonSubstitutionEvent.request_id, func.max(LessonSubstitutionEvent.created_at))
                              .filter(LessonSubstitutionEvent.request_id.in_([r.id for r in reqs])).group_by(LessonSubstitutionEvent.request_id).all())          # #157 A8: one more query, for the whole page
        ids = [r.id for r in reqs if r.origin == "system" or r.status != PENDING]
        if not ids:
            return
        rows = (self.db.query(LessonSubstitutionEvent).filter(LessonSubstitutionEvent.request_id.in_(ids),
                                                              LessonSubstitutionEvent.action.in_(("requested", "assignment_cleared", "closed", "lesson_restored", "day_off_voided")))
                .order_by(LessonSubstitutionEvent.created_at, LessonSubstitutionEvent.id).all())
        for e in rows:
            if e.action == "assignment_cleared":
                ctx.cleared[e.request_id] = e
            elif e.action == "closed":
                ctx.closed_trigger[e.request_id] = (e.snapshot or {}).get("trigger")
            elif e.action == "lesson_restored":
                ctx.restored.add(e.request_id)
            elif e.action == "day_off_voided":
                ctx.voided.add(e.request_id)
                ctx.needs_decision[e.request_id] = bool((e.snapshot or {}).get("needsDecision"))
            elif (e.snapshot or {}).get("trigger"):
                ctx.triggers[e.request_id] = e.snapshot["trigger"]

    @staticmethod
    def _cleared_to(event: LessonSubstitutionEvent) -> Optional[str]:
        """The teacher who took the lesson over (second id of ``teacherId``), ``None`` when nobody did (the main teacher leads again)."""
        pair = (event.snapshot or {}).get("teacherId") or [None, None]
        return pair[1] if len(pair) > 1 else None

    def _cleared(self, r: Req, ctx: Ctx) -> Dict[str, Any]:
        """P1.2: the substitution of a record that was later taken off, replaced, released or whose lesson was cancelled / moved - the status of the record does not change."""
        e = ctx.cleared.get(r.id)
        if e is None:
            return {"clearedAt": None, "clearedBy": None, "clearedTo": None, "clearedTrigger": None}
        return {"clearedAt": e.created_at.isoformat(), "clearedBy": self._author(ctx, e.actor_id), "clearedTo": self._person(ctx, self._cleared_to(e)),
                "clearedTrigger": (e.snapshot or {}).get("trigger")}

    @staticmethod
    def outcome_of(r: Req, ctx: Ctx) -> str:
        """#157 A11: THE one code of what became of a record (P157_API.md §2) - the list, the detail and ``mine`` all read it; the texts and colours are the front's.  By the last event:
        a day off that was cancelled, a lesson given back, the fate of a substitution, then the status / the reason of the closing."""
        if r.status == PENDING:
            return "pending"
        if r.id in ctx.voided:
            return "day_off_voided"
        if r.status == "substituted":
            e = ctx.cleared.get(r.id)
            if e is None:
                return "substituted"
            snap = e.snapshot or {}
            if snap.get("trigger") == "cancel":
                return "cancelled_by_day_off" if snap.get("dayOffId") else "lesson_later_cancelled"
            if snap.get("trigger") == "move":
                return "lesson_later_moved"
            return "substitution_replaced" if SubstitutionsBase._cleared_to(e) else "substitution_cleared"
        if r.status == "cancelled":
            if r.id in ctx.restored:
                return "lesson_restored"
            return "lesson_not_held" if (ctx.triggers.get(r.id) == "teacher_cancel" or r.decided_via == "teacher_cancel") else "cancelled"
        if r.status == "closed":
            if r.closed_reason == "LESSON_CANCELLED":
                return "cancelled_by_day_off" if ctx.closed_trigger.get(r.id) == "day_off" else "lesson_already_cancelled"
            return CLOSED_OUTCOME.get(r.closed_reason or "", "closed")
        return r.status          # rescheduled | rejected | withdrawn

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
                if r.closed_reason == "GROUP_TEACHER_CHANGED":          # #157 (owner 28.10): the name of the new teacher is the manager's; the one who asked is told only that the group was handed over
                    text = ((f"Группу передали: {r.decision_note}" if r.decision_note else "Группу передали другому преподавателю") if ctx.viewer == MANAGER
                            else "Запрос закрыт: группу передали другому преподавателю")
            decision = {
                "kind": KIND[r.status], "by": self._author(ctx, r.decided_by), "at": r.decided_at.isoformat() if r.decided_at else r.updated_at.isoformat(),
                "via": r.decided_via, "note": None if r.status == "closed" else r.decision_note,
                "substitute": self._person(ctx, r.substitute_id), "movedTo": moved, "closedReason": r.closed_reason, "closedReasonText": text,
            }
        prev = None
        if r.previous_request_id and r.previous_request_id in ctx.previous and (ctx.viewer == MANAGER or ctx.previous[r.previous_request_id].requester_id == ctx.viewer):
            p = ctx.previous[r.previous_request_id]            # #157 A1: a teacher reads the record that came before ONLY when it is his own (the name of another teacher / his substitute is not his)
            prev = {"id": str(p.id), "status": p.status, "teacherName": short_name(ctx.users.get(str(p.requester_id))),
                    "substituteName": short_name(ctx.users.get(str(p.substitute_id))) if p.substitute_id else None}
        # #157 A2: what an administrator wrote on an action of his (a cancel, a return, a kind change, a move, a substitution - the records with a ``trigger``) is not for the teacher of the lesson;
        # the text of a record the system REOPENED stays: it is the explanation (LESSONS 46)
        comment = None if (r.origin == "system" and ctx.viewer != MANAGER and ctx.triggers.get(r.id)) else r.comment
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
            "comment": comment, "suggestedTeacher": sug, "createdAt": r.created_at.isoformat(),
            "minutesToStart": minutes, "urgent": bool(pending and R.is_urgent(minutes)), "overdue": bool(pending and R.is_overdue(minutes)),
            "decision": decision, "previous": prev, "trigger": ctx.triggers.get(r.id) if r.origin == "system" else None, **self._cleared(r, ctx),
            "outcome": self.outcome_of(r, ctx), "needsDecision": ctx.needs_decision.get(r.id, False), "closedTrigger": ctx.closed_trigger.get(r.id) if r.status == "closed" else None,
            "lastActivityAt": max(t for t in (r.decided_at or r.created_at, ctx.last_event.get(r.id)) if t is not None).isoformat(),
        }

    def items(self, reqs: Sequence[Req], viewer: Any) -> List[Dict[str, Any]]:
        """``viewer``: ``MANAGER`` or the user id of the teacher the answer is for - required, so that a new call site has to say whose eyes it is (#157 A1)."""
        ctx = self.ctx_for(reqs, viewer)
        now = self.now()
        return [self.item(r, ctx, now) for r in reqs]

    def one(self, r: Req, viewer: Any) -> Dict[str, Any]:
        return self.items([r], viewer)[0]

    def already_decided(self, r: Req, viewer: Any, message: Optional[str] = None) -> OpsError:
        item = self.one(r, viewer)
        if message is None:
            message = "Учитель отозвал запрос" if r.status == "withdrawn" else (
                (item['decision']['closedReasonText'] if item['decision']['closedReasonText'].startswith("Запрос закрыт") else f"Запрос закрыт: {item['decision']['closedReasonText']}") if r.status == "closed" and item["decision"] and item["decision"]["closedReasonText"]
                else R.ERROR_CATALOGUE["SUBSTITUTION_ALREADY_DECIDED"][1])
        return R.err(409, "SUBSTITUTION_ALREADY_DECIDED", message, item=item if viewer == MANAGER or r.requester_id == viewer else None)      # a teacher gets the record of ANOTHER one as a refusal only

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

    def close(self, r: Req, reason: str, *, actor_id: Any = None, trigger: str = "revalidate", group: Optional[Group] = None, via: str = "system",
              extra: Optional[Dict[str, Any]] = None) -> None:
        """Close a request because its premise is gone (status ``closed`` + one event).  ``via`` ``system``: nobody did it; otherwise ``actor_id`` is the author (P1.2: the administrator
        took the substitution off)."""
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
        r.decided_via = via
        r.decided_by = None if via == "system" else _uuid(actor_id)
        r.decision_note = note
        r.updated_at = now
        self.event(r, "closed", actor_id, {"closedReason": reason, "trigger": trigger, **(extra or {})})

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
                    self.close(r, reason, trigger="window_expired" if reason == "LESSON_OUTSIDE_WINDOW" else trigger, group=group)      # W1 S20: the month border says so in the journal
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
        return {"batchId": str(out_batch), "created": self.items(created, actor), "alreadyRequested": self.items(already, actor), "skipped": skipped}

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
        return {"items": self.items(reqs, actor), "pendingTotal": pending_total, "assignedToMe": self._assigned_to(org_id, actor), "clearedForMe": self._cleared_for(org_id, actor)}

    def _cleared_for(self, org_id: Any, actor: UUID) -> List[Dict[str, Any]]:
        """W1 S22 (D30): the lessons where the actor WAS the substitute and the assignment was taken off in the last 14 days - who took it off and when (the substitute used to just lose it)."""
        since = self.now() - timedelta(days=CLEARED_DAYS)
        rows = (self.db.query(LessonTeacherAssignment, Group).join(Group, Group.id == LessonTeacherAssignment.group_id)
                .filter(LessonTeacherAssignment.org_id == org_id, LessonTeacherAssignment.teacher_id == actor, LessonTeacherAssignment.cancelled_at.isnot(None),
                        LessonTeacherAssignment.cancelled_at >= since).order_by(LessonTeacherAssignment.cancelled_at.desc()).all())
        users = self.teachers.users_by_id(a.cancelled_by for a, _ in rows if a.cancelled_by)
        roles = self.roles_of([org_id], [a.cancelled_by for a, _ in rows])
        ctx = SimpleNamespace(users=users, roles=roles, viewer=actor)          # #157 R3: the substitute reads who took it off by the ROLE only (like every author in the journal)
        out = []
        for asg, group in rows:
            day = date.fromisoformat(asg.lesson_date)
            occ = next((o for o in self.svc._resolved_occurrences_for_group(group, date_from=day, date_to=day) if o["id"] == asg.lesson_id), None)
            out.append({"groupId": str(group.id), "groupName": group.name, "lessonId": asg.lesson_id, "lessonDate": asg.lesson_date,
                        "startTime": occ["startTime"] if occ else None, "endTime": occ["endTime"] if occ else None, "clearedAt": asg.cancelled_at.isoformat(),
                        "clearedBy": self._author(ctx, asg.cancelled_by) if asg.cancelled_by else None})
        return out

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
        roles = self.roles_of([org_id], [o["replacing"]["id"] for o in out if o["replacing"]])      # one query for all the rows
        for o in out:
            if o["replacing"]:
                o["replacing"]["role"] = roles.get(o["replacing"]["id"])
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
            return self.one(r, actor)
        if r.status != PENDING:
            self.db.commit()
            raise self.already_decided(r, actor)
        now = self.now()
        r.status, r.decided_by, r.decided_at, r.decided_via, r.updated_at = "withdrawn", actor, now, "withdraw", now
        self.event(r, "withdrawn", actor)
        self.db.commit()
        return self.one(r, actor)

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
    def summary(self, org_id: Any, *, revalidate: bool = True) -> Dict[str, int]:
        """The badge of «Замены».  W1 S18 (А-8): the same lazy pass as the list (a revised / stopped series, a month border) runs first, so the counts are not those of requests that are already void.
        ``expiringTotal`` (S20, D25): pending requests of the previous month that fall out of the window of the unclosed lessons on the 1st - non-zero only in the last 3 days before it."""
        if revalidate:      # (R3 13) one pass per request, bounded (the list does it itself over its page + the same set and asks the counts with ``revalidate=False``)
            self.revalidate(self._badge_batch(org_id, []))
        now, today = self.now(), bc.business_today()
        base = self.db.query(Req).filter(Req.org_id == org_id, Req.status == PENDING)
        pending = base.count()
        urgent = base.filter(Req.starts_at > now, Req.starts_at < now + timedelta(minutes=R.URGENT_MINUTES)).count()
        overdue = base.filter(Req.starts_at <= now).count()
        this_month = today.replace(day=1)
        next_month = (this_month + timedelta(days=32)).replace(day=1)
        expiring = base.filter(Req.lesson_date < this_month.isoformat()).count() if (next_month - today).days <= EXPIRY_WARNING_DAYS else 0
        one_off = OneOffPurpose(self.svc).pending_count(org_id)          # #154: the decisions of the money of additional lessons are in the same badge (the 1st-of-the-month window and «urgent» do not apply to them)
        return {"pendingTotal": pending, "urgentTotal": urgent, "overdueTotal": overdue, "expiringTotal": expiring, "oneOffDecisionsTotal": one_off, "waitingTotal": pending + one_off}

    def _badge_batch(self, org_id: Any, extra: Sequence[Req]) -> List[Req]:
        """The pending requests to revalidate before counting: the oldest ``SUMMARY_REVALIDATE_MAX`` of the centre + ``extra`` (the page of the list), each once."""
        batch = {r.id: r for r in self.db.query(Req).filter(Req.org_id == org_id, Req.status == PENDING).order_by(Req.starts_at, Req.id).limit(SUMMARY_REVALIDATE_MAX).all()}
        batch.update({r.id: r for r in extra})
        return list(batch.values())

    @staticmethod
    def _activity():
        """#157 A8: SQL twin of ``lastActivityAt`` - the later of the decision (the creation of a waiting record) and the newest event of the record."""
        newest = select(func.max(LessonSubstitutionEvent.created_at)).where(LessonSubstitutionEvent.request_id == Req.id).correlate(Req).scalar_subquery()
        return func.greatest(func.coalesce(Req.decided_at, Req.created_at), newest)

    def list(self, org_id: Any, *, status: str, group_id: Optional[str], teacher_id: Optional[str], date_from: Optional[date], date_to: Optional[date],
             urgent: Optional[bool], search: Optional[str], limit: int, offset: int) -> Dict[str, Any]:
        now = self.now()
        q = self.db.query(Req).filter(Req.org_id == org_id)
        if status == "pending":
            q = q.filter(Req.status == PENDING)
        elif status == "decided":
            q = q.filter(Req.status != PENDING)
        activity = self._activity()
        if status != "pending" and date_from is None and date_to is None:          # #157 A8: «за 30 дней» = what HAPPENED in the last 30 days (the time of the newest event), not the date of the lesson
            since = now - timedelta(days=LIST_DEFAULT_DAYS)
            q = q.filter(activity >= since) if status == "decided" else q.filter(or_(Req.status == PENDING, activity >= since))
        if group_id and _uuid(group_id):
            q = q.filter(Req.group_id == _uuid(group_id))
        if teacher_id and _uuid(teacher_id):            # P1.2: both teachers of a record - the one who was replaced and the one who replaces
            q = q.filter(or_(Req.requester_id == _uuid(teacher_id), Req.substitute_id == _uuid(teacher_id)))
        if date_from:
            q = q.filter(Req.lesson_date >= date_from.isoformat())
        if date_to:
            q = q.filter(Req.lesson_date <= date_to.isoformat())
        if urgent:
            q = q.filter(Req.status == PENDING, Req.starts_at > now, Req.starts_at < now + timedelta(minutes=R.URGENT_MINUTES))
        if search:
            like = f"%{search.strip()}%"
            substitute = aliased(User)
            q = q.join(Group, Group.id == Req.group_id).join(User, User.id == Req.requester_id).outerjoin(substitute, substitute.id == Req.substitute_id).filter(
                or_(Group.name.ilike(like), User.last_name.ilike(like), User.first_name.ilike(like), substitute.last_name.ilike(like), substitute.first_name.ilike(like)))
        total = q.count()
        if status == "pending":
            q = q.order_by((Req.starts_at <= now).desc(), Req.starts_at, Req.id)
        elif status == "decided":
            q = q.order_by(activity.desc(), Req.id)          # #157 A8: the newest event of the row, not only the decision - a substitution taken off just now lifts its row
        else:
            q = q.order_by((Req.status == PENDING).desc(), Req.starts_at, Req.id)
        reqs = q.offset(offset).limit(limit).all()
        # ONE lazy pass over what is being RETURNED plus the bounded set the badge counts (W1 R3 13): a stale request must not be listed nor counted; each request is looked at once
        if self.revalidate(self._badge_batch(org_id, [r for r in reqs if r.status == PENDING])):
            if status == "pending":
                reqs = q.offset(offset).limit(limit).all()       # the page refills with the next ones
                total = q.count()
            else:
                for r in reqs:
                    self.db.refresh(r)
        counts = self.summary(org_id, revalidate=False)
        return {"items": self.items(reqs, MANAGER), "total": total, "pendingTotal": counts["pendingTotal"], "urgentTotal": counts["urgentTotal"], "expiringTotal": counts["expiringTotal"]}

    def detail(self, org_id: Any, actor_id: Any, is_manager: bool, request_id: Any) -> Dict[str, Any]:
        """The record with its WHOLE history (#157 A5): ``chain`` (before), ``next`` (after), ``events`` of all of them, each with ``requestId``.  A GET writes nothing (A6: no revalidate - the
        list and the decision keep the record fresh); the exam-service is asked only for a waiting record and only for a manager (A7).  A requester reads his OWN records only (W1 S19, A1)."""
        r = self._request(org_id, request_id)
        actor = _uuid(actor_id)
        if not is_manager and r.requester_id != actor:
            raise R.err(404, "SUBSTITUTION_NOT_FOUND")          # #157 R3: a record of another teacher is not «forbidden» - for him it does not exist (no probing of ids)
        viewer = viewer_for(actor, teacher=not is_manager)
        back, back_cut = self._links_back(r)
        ahead, ahead_cut = self._links_ahead(r)
        chain, nxt = ([c for c in part if c.requester_id == actor] if not is_manager else part for part in (back, ahead))      # W1 S19 (D14): the requester sees his OWN requests only - the reason and the comment of another teacher are not his
        events, events_cut = self._journal([*chain, r, *nxt], viewer)
        return {
            "item": self.one(r, viewer), "chain": self.items(chain, viewer), "next": self.items(nxt, viewer), "truncated": bool(back_cut or ahead_cut or events_cut),
            "events": events,
            "examBinding": self.exam_binding(r) if (is_manager and r.status == PENDING) else None,
        }

    def _links_back(self, r: Req) -> Tuple[List[Req], bool]:
        """The records before ``r`` (oldest first, at most ``DETAIL_LINKS``); ``True`` when there were more."""
        out: List[Req] = []
        cur = r
        while cur.previous_request_id and len(out) < DETAIL_LINKS:
            cur = self.db.query(Req).filter(Req.id == cur.previous_request_id, Req.org_id == r.org_id).first()
            if cur is None:
                return out, False
            out.insert(0, cur)
        return out, bool(cur is not None and cur.previous_request_id)

    def _links_ahead(self, r: Req) -> Tuple[List[Req], bool]:
        """The records that came after ``r`` - those that point to it and, in turn, those that point to them (breadth first, at most ``DETAIL_LINKS``), oldest first; ``True`` when there were more.
        A record can have two followers (a reopened request points to the OLD one): the list is flat, the history by time."""
        out: List[Req] = []
        frontier = [r.id]
        while frontier:
            rows = self.db.query(Req).filter(Req.previous_request_id.in_(frontier), Req.org_id == r.org_id).order_by(Req.created_at, Req.id).all()
            frontier = []
            for row in rows:
                if len(out) >= DETAIL_LINKS:
                    return sorted(out, key=lambda x: (x.created_at, str(x.id))), True
                out.append(row)
                frontier.append(row.id)
        return sorted(out, key=lambda x: (x.created_at, str(x.id))), False

    def _journal(self, linked: Sequence[Req], viewer: Any) -> Tuple[List[Dict[str, Any]], bool]:
        """The events of ``linked`` (the chain, the record, what came after) as ONE history for ``viewer``: ``(created_at, position of the record in ``linked``, id)``.  What ONE act of an
        administrator wrote on two records is ONE event (``mergedRequestId``: the other record): «assignment taken off the old record» + «substituted on the new one» (a substitution replaced by another:
        the old record's event stays, with ``substituteName`` = the new teacher), «lesson given back» of the cancelled record + «closed / LESSON_RESTORED» of the record of the return (the cancelled
        record's event stays and carries what was cancelled).  At most ``DETAIL_EVENTS`` (the newest), ``True`` when cut."""
        position = {r.id: i for i, r in enumerate(linked)}
        rows = (self.db.query(LessonSubstitutionEvent).filter(LessonSubstitutionEvent.request_id.in_(list(position))).order_by(LessonSubstitutionEvent.created_at, LessonSubstitutionEvent.id).all())
        dropped: Set[UUID] = set()                  # events folded into another one
        folded: Dict[UUID, UUID] = {}               # the event that absorbed one -> the id of the other record
        extra: Dict[UUID, Dict[str, Any]] = {}      # snapshot keys an absorbing event takes over
        for cleared in rows:
            snap = cleared.snapshot or {}
            if cleared.action == "assignment_cleared" and snap.get("trigger") == "put" and self._cleared_to(cleared) is not None:
                other = self._partner(rows, linked, cleared, lambda e, r: (
                    e.action == "substituted" and r.previous_request_id == cleared.request_id and (e.snapshot or {}).get("via") in (PUT_VIA, "request")
                    and ((e.snapshot or {}).get("teacherId") or [None, None])[1] == self._cleared_to(cleared)), dropped)
            elif cleared.action == "lesson_restored":
                other = self._partner(rows, linked, cleared, lambda e, r: (
                    e.action == "closed" and (e.snapshot or {}).get("closedReason") == RESTORED), dropped)
                if other is not None:
                    extra[cleared.id] = {"previous": (other[0].snapshot or {}).get("previous")}
            else:
                continue
            if other is not None:
                dropped.add(other[0].id)
                folded[cleared.id] = other[1].id
        rows = [e for e in rows if e.id not in dropped]
        rows.sort(key=lambda e: (e.created_at, position[e.request_id], str(e.id)))
        cut = len(rows) > DETAIL_EVENTS
        rows = rows[-DETAIL_EVENTS:]
        pairs = {e.id: (e.snapshot or {}).get("teacherId") or [None, None] for e in rows if e.action in ("substituted", "assignment_cleared")}
        subs = {e.id: pairs[e.id][1] for e in rows if e.id in pairs and len(pairs[e.id]) > 1 and pairs[e.id][1]}
        people = [e.actor_id for e in rows if e.actor_id] + [_uuid(u) for u in subs.values()]
        names = self.teachers.users_by_id(u for u in people if u)
        roles = self.roles_of([linked[0].org_id], [e.actor_id for e in rows])
        out = []
        for e in rows:
            item = {"requestId": str(e.request_id), **self.event_out(e, names, roles, viewer, {**(e.snapshot or {}), **extra.get(e.id, {})})}
            if e.id in subs:
                item["substituteName"] = short_name(names.get(str(subs[e.id])))          # «Новая замена: Сидоров В.» / «Заменена на Сидоров В.»: the front only prefixes it
            if e.id in folded:
                item["mergedRequestId"] = str(folded[e.id])
            out.append(item)
        return out, cut

    @staticmethod
    def _partner(rows: Sequence[LessonSubstitutionEvent], linked: Sequence[Req], first: LessonSubstitutionEvent, fits, dropped: Set[UUID]) -> Optional[Tuple[LessonSubstitutionEvent, Req]]:
        """The event of ANOTHER record that the same act wrote next to ``first``: the same author, not further than ``MERGE_SECONDS``, accepted by ``fits(event, record)``."""
        by_id = {r.id: r for r in linked}
        for e in rows:
            r = by_id.get(e.request_id)
            if (r is not None and e.id not in dropped and e.request_id != first.request_id and e.actor_id == first.actor_id and fits(e, r)
                    and abs((e.created_at - first.created_at).total_seconds()) <= MERGE_SECONDS):
                return e, r
        return None

    def exam_binding(self, r: Req) -> Dict[str, Any]:
        try:
            exams = exam_bridge.completion_check(r.org_id, r.group_id, r.lesson_id, r.lesson_date)
        except (exam_bridge.ExamBridgeUnavailable, exam_bridge.ExamBridgeConflict):
            return {"state": "unavailable", "title": None, "canCancel": False}
        if not exams:
            return {"state": "none", "title": None, "canCancel": True}
        first = exams[0]
        return {"state": "ok", "title": first.get("title"), "canCancel": first.get("status") != "active"}

    @staticmethod
    def clean_snapshot(action: str, snapshot: Optional[Dict[str, Any]], viewer: Any) -> Dict[str, Any]:
        """#157 A1: the keys of ``SNAPSHOT_KEYS[action]`` and nothing else (for everybody); a teacher also loses what is the centre's business (``_MANAGER_ONLY``)."""
        hidden = set() if viewer == MANAGER else set(_MANAGER_ONLY.get(action, ()))
        out = {k: v for k, v in (snapshot or {}).items() if k in SNAPSHOT_KEYS.get(action, ()) and k not in hidden}
        if (snapshot or {}).get("dayOffId") and "byDayOff" in SNAPSHOT_KEYS.get(action, ()):
            out["byDayOff"] = True                    # #157 R3: the fact («a day off did it») for everybody, the id of the day off for a manager only
        if isinstance(out.get("previous"), dict):
            out["previous"] = {k: v for k, v in out["previous"].items() if k in _PREVIOUS_KEYS}
        return out

    def event_out(self, e: LessonSubstitutionEvent, names: Dict[str, User], roles: Dict[str, str], viewer: Any, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """One event as the viewer reads it: a manager sees who did it; a teacher sees the ROLE only (no name, no id - #157 A1)."""
        actor = None
        if e.actor_id:
            role = roles.get(str(e.actor_id))
            actor = {"id": str(e.actor_id), "name": short_name(names.get(str(e.actor_id))), "role": role} if viewer == MANAGER else {"role": role}
        return {"action": e.action, "actor": actor, "at": e.created_at.isoformat(), "snapshot": self.clean_snapshot(e.action, snapshot if snapshot is not None else e.snapshot, viewer)}

    # ================================================================================================ candidates
    def rate_missing(self, org_id: Any, teacher_id: UUID, lesson_type: str, lesson_date: str) -> bool:
        return teacher_id in self.teachers.rates_missing(org_id, [teacher_id], lesson_type, lesson_date)

    def rates_missing(self, org_id: Any, teacher_ids: Iterable[UUID], lesson_type: str, lesson_date: str) -> Set[UUID]:
        return self.teachers.rates_missing(org_id, teacher_ids, lesson_type, lesson_date)

    def colleagues(self, org_id: Any, actor_id: Any) -> List[Dict[str, Any]]:
        """«Предложить коллегу»: the active teachers of the centre (only a teacher leads lessons) except the caller - id, name and role only (no e-mail, no phone)."""
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
        from .payroll_estimate import substitute_pay_estimates  # lazy: it imports the payments and payroll services

        pay = substitute_pay_estimates(self.svc, org_id, group, r.lesson_id, r.lesson_date, start, [u.id for u in staff], is_owner=is_owner)
        no_rate = self.rates_missing(org_id, [u.id for u in staff], self.teachers.lesson_type_on(group, r.lesson_date), r.lesson_date)         # W1 (F5): a yes / no for the administrator too - the rates stay the owner's
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
                "rateMissing": user.id in no_rate,
                "substitutePayEstimate": pay[user.id]["amount"], "substitutePayEstimateReason": pay[user.id]["reason"],
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


class Substitutions(LessonCancelMixin, SubstitutionsBase):
    """The four decisions of an administrator (substitute / reschedule / cancel / reject) and the bulk variant."""

    # ------------------------------------------------------------------------------------------- dispatch
    def decide(self, org_id: Any, actor_id: Any, actor_role: str, request_id: Any, body) -> Dict[str, Any]:
        actor, via = _uuid(actor_id), self._via(actor_role)
        viewer = viewer_for(actor, teacher=via == TEACHER_MOVE)          # a teacher who moves his own lesson is answered as a teacher (#157 A1)
        group, r = self._lock_request(org_id, request_id)
        if r.status == PENDING:
            reason, occ = self.closure_reason(r, group)
            if reason and reason != "LESSON_OUTSIDE_WINDOW":
                self.close(r, reason, trigger="decide", group=group)
                self.db.commit()
                raise self.already_decided(r, viewer)
            if occ and (occ.get("startTime") != r.start_time or occ.get("endTime") != r.end_time):
                self._sync_times(r, occ)           # the lesson was moved in the schedule: reschedule / cancel judge the CURRENT time, not the one of the request
        if r.status != PENDING:
            if self._same_decision(r, actor, via, body):
                item = self.one(r, viewer)
                self.db.commit()
                return {"item": item, "result": self._result_of(r)}
            item_error = self.already_decided(r, viewer)
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
                result = self._reschedule(group, r, actor, body, via)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return {"item": self.one(r, viewer), "result": result}

    # ---------------------------------------------------------------------------------------------- helpers
    def _finish(self, r: Req, status: str, actor: UUID, note: Optional[str], via: str = "request") -> None:
        now = self.now()
        r.status, r.decided_by, r.decided_at, r.decided_via, r.decision_note, r.updated_at = status, actor, now, via, note, now

    def _substituted(self, r: Req, teacher: UUID, asg: Optional[LessonTeacherAssignment], actor: Any, note: Optional[str], via: str) -> None:
        """The one place a request becomes ``substituted``: the decision of the Замены page (``request``) and the assignment on the lesson sheet (``PUT_VIA``)."""
        r.substitute_id = teacher
        r.assignment_id = asg.id if asg is not None else None
        self._finish(r, "substituted", _uuid(actor), note, via)
        self.event(r, "substituted", actor, {"teacherId": [str(r.requester_id), str(teacher)], "assignmentId": _s(r.assignment_id), "via": via})

    @staticmethod
    def _reason(body, low: int, high: int) -> str:
        reason = (body.reason or "").strip()
        if len(reason) < low:
            raise R.err(422, "REASON_REQUIRED")
        if len(reason) > high:
            raise unprocessable("REASON_TOO_LONG", f"Причина не длиннее {high} символов")
        return reason

    @staticmethod
    def _via(actor_role: str) -> str:
        """Only a teacher's own move is decided by a teacher (``move_lesson``); every other decision is an administrator's."""
        return TEACHER_MOVE if actor_role == "teacher" else "request"

    def _same_decision(self, r: Req, actor: UUID, via: str, body) -> bool:
        if STATUS_OF.get(body.decision) != r.status or r.decided_by != actor or r.decided_via != via:
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
            row = self._row(r)           # the REAL version and kind of the cancellation (a repeat of the decision answers what was written)
            return {"kind": "cancel", "version": int(row.version or 1) if row is not None else 1, "cancellationKind": row.cancellation_kind if row is not None else None, "warnings": []}
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
        self._substituted(r, teacher, self.teachers.last_assignment, actor, note, "request")
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

    def _cancelled_row(self, group: Group, ref: Any, actor: UUID, text: str, details: Dict[str, Any], kind: Optional[str] = None,
                       day_off_id: Any = None, request: Optional[Req] = None) -> GroupAttendance:
        """The ``cancelled`` row of the journal of a lesson that has none (``ref``: a request or a lesson reference) + its audit row.  A second writer of the same lesson loses the
        race with a 409 (``LESSON_CHANGED``), never a 500.  The substitute of the lesson is the caller's business (``_release_substitute``)."""
        day = date.fromisoformat(ref.lesson_date)
        occ = next((o for o in self.svc._resolved_occurrences_for_group(group, date_from=day, date_to=day) if o["id"] == ref.lesson_id), None)
        start = hhmm_to_minutes(ref.start_time)
        past = ref.starts_at <= self.now()
        key = (str(group.id), ref.lesson_id, ref.lesson_date)
        skip = self.db.info.setdefault("b2_skip", set())
        skip.add(key)
        try:
            row = GroupAttendance(
                org_id=group.org_id, group_id=group.id, teacher_id=self.svc._attendance_teacher_snapshot(group, ref.lesson_id, day, occ),
                lesson_date=ref.lesson_date, lesson_id=ref.lesson_id, topic=(occ.get("topic") or occ.get("subject")) if occ else None, status="cancelled",
                records={}, roster_snapshot=self.svc._attendance_roster_at(group.id, bc.lesson_instant_utc(day, start)), marked_by=actor,
                started_at=None, cancellation_reason=text, version=1, cancellation_kind=kind, day_off_id=day_off_id,
            )
            try:
                self.db.add(row)
                self.db.flush()
            except IntegrityError as exc:
                raise conflict("LESSON_CHANGED", "Урок уже изменили: обновите страницу и повторите") from exc
            self.db.add(GroupAttendanceAudit(
                attendance_id=row.id, org_id=group.org_id, group_id=group.id, actor_id=actor,
                action="reconciliation_cancelled" if past else "cancelled", before_status=None, after_status="cancelled", before_records=None,
                after_records={}, cancellation_reason=text, details={**({"substitution_request_id": str(request.id)} if request is not None else {}), **details},
                created_at=self.now(),
            ))
            self.db.flush()
        finally:
            skip.discard(key)
        return row

    # ---------------------------------------------------------------------------------------------- cancel
    def _cancel(self, group: Group, r: Req, actor: UUID, body) -> Dict[str, Any]:
        """The decision «Отменить урок» = THE cancellation (``cancel_lesson``) of the lesson of the request; the administrator chooses the kind (default: «по вине центра» - the
        sick teacher is the centre's fault, the students are deducted when the centre deducts cancelled lessons; a MOVE has no kind: neutral for the money)."""
        reason = self._reason(body, 3, 500)
        day = date.fromisoformat(r.lesson_date)
        if r.starts_at <= self.now() and day < bc.month_window_start(bc.business_today()):
            raise R.err(422, "LESSON_OUTSIDE_WINDOW", "Урок старше окна незакрытых уроков: отменить его нельзя — можно только отклонить запрос")
        out = self.cancel_lesson(r.org_id, actor, group.id, r.lesson_id, day, reason=reason, kind=getattr(body, "cancellationKind", None) or DEFAULT_CANCELLATION_KIND,
                                 request=r, via="request")
        return {"kind": "cancel", "version": out["version"], "cancellationKind": out["kind"], "warnings": out["warnings"]}

    # ------------------------------------------------------------------------------------------- reschedule
    def _new_slot(self, group: Group, r: Req, body) -> Tuple[date, str, str, int, int]:
        if body.newDate is None or not body.startTime or not body.endTime:
            raise R.err(422, "MOVE_FIELDS_REQUIRED")
        try:
            start_m, end_m = self.svc._validate_lesson_time_range(body.startTime, body.endTime)
        except ValueError as exc:
            raise R.err(422, "MOVE_FIELDS_REQUIRED", str(exc)) from exc
        return body.newDate, body.startTime, body.endTime, start_m, end_m

    def _substitute_of(self, group: Group, ref: Any) -> Optional[LessonTeacherAssignment]:
        """The active assignment (substitute) of the lesson, if any."""
        return (self.db.query(LessonTeacherAssignment).filter(LessonTeacherAssignment.group_id == group.id, LessonTeacherAssignment.lesson_id == ref.lesson_id,
                                                              LessonTeacherAssignment.lesson_date == ref.lesson_date, LessonTeacherAssignment.cancelled_at.is_(None)).first())

    def conflicts_for_move(self, group: Group, r: Req, new_date: date, start_m: int, end_m: int, start: str, end: str):
        """``(teacher, teacher conflicts, group conflicts)`` of putting the lesson at the new slot.  W1 S12 (D19): a lesson that has a substitute keeps him at the new slot, so it is HIS
        time that is checked (not the main teacher's)."""
        iso = new_date.isoformat()
        book = TeacherBook(self.db)
        sub = self._substitute_of(group, r)
        teacher_id = sub.teacher_id if sub is not None else book.default_teacher(group, iso, end_m)
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

    def _move_check(self, group: Group, r: Any, new_date: date, start: str, end: str, *, detail: bool = True) -> Dict[str, Any]:
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
        warnings += DaysOffService(self.svc).warnings_for_date(group.org_id, new_date)       # a move onto a day off of the centre: a warning, never a block
        warnings += self._student_busy(group, new_date, start_m, end_m, detail=detail)         # W1 S12: a student who has another lesson then: a warning, never a block
        return {"ok": not t_conf and not g_conf, "teacherId": _s(teacher_id), "teacherName": short_name(user), "teacherConflicts": t_conf[:20],
                "groupConflicts": g_conf[:20], "warnings": warnings}

    def _reschedule(self, group: Group, r: Req, actor: UUID, body, via: str) -> Dict[str, Any]:
        reason = self._reason(body, 3, 500)
        now = self.now()
        if r.starts_at <= now:
            raise R.err(409, "LESSON_ALREADY_STARTED", MOVE_STARTED_TEXT)
        if via == TEACHER_MOVE and self._teacher_too_late(r.starts_at, now):
            raise R.err(409, "MOVE_TOO_LATE")
        new_date, start, end, start_m, end_m = self._new_slot(group, r, body)
        refusal = self._slot_refusal(r, new_date, start, end, start_m, now) or self._series_refusal(group, r, new_date)
        if refusal is not None:
            raise refusal
        target = TeacherBook(self.db).default_teacher(group, new_date.isoformat(), end_m)
        if via == TEACHER_MOVE and target != actor:
            raise R.err(403, "NOT_LESSON_TEACHER")          # a teacher moves his lesson only to a day he still leads the group; another person's busy time is none of his business
        sub = self._substitute_of(group, r)                 # W1 S12 (D19): the substitute of the lesson stays on the moved lesson, his time is what is checked
        sub_id = sub.teacher_id if sub is not None else None
        target = sub_id or target
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
        room = normalize_room(body.room) if body.room is not None else normalize_room(occ.get("room"))       # W1 S11 (D5): the decision keeps the room of the lesson, like the lesson sheet
        fmt = body.format or occ.get("format") or "offline"
        text = f"Перенесён на {new_date.strftime('%d.%m')}, {start}–{end}: {reason}"[:500]
        payload = {"dayOfWeek": new_date.isoweekday(), "startTime": start, "endTime": end, "subject": "Математика", "room": room, "format": fmt,
                   "color": occ.get("color") or "blue", "topic": occ.get("topic")}
        try:
            self.svc._validate_schedule_payload(payload)         # the same rules as every one-off lesson (format, day, time range)
        except ValueError as exc:
            raise unprocessable("SCHEDULE_PAYLOAD_INVALID", str(exc)) from exc
        self._exam_check(r, text)
        row = self._cancelled_row(group, r, actor, text, {"moved_to": {"date": new_date.isoformat(), "startTime": start, "endTime": end}}, request=r)
        self._release_substitute(group, r, actor, "move")
        new_id = f"one-off-{uuid4().hex[:16]}"
        source = one_off_of(self.db, group.id, r.lesson_id)       # #154: a copy keeps what the lesson is for (a make-up stays a make-up - the pointer of the cancelled lesson goes to the copy; an additional lesson keeps its money and its author)
        inherited = copy_fields(source) if source is not None else {"purpose": MOVED, "money": None, "made_up_lesson_id": None, "made_up_date": None, "created_by": actor}
        self.db.add(GroupScheduleOneOffLesson(id=new_id, org_id=group.org_id, group_id=group.id, lesson_date=new_date.isoformat(), payload=payload, **inherited))
        self.db.flush()
        if source is not None:
            OneOffPurpose(self.svc).repoint(group, source, new_id)
        self._exam_release(r, text)
        from .schedule_rules import ScheduleRules

        warnings = ScheduleRules(self.svc).warnings_for(group, new_id, [new_date], payload)
        warnings += DaysOffService(self.svc).warnings_for_date(group.org_id, new_date)
        warnings += self._student_busy(group, new_date, start_m, end_m, detail=via != TEACHER_MOVE)
        kept = self._keep_substitute(group, sub_id, new_id, new_date, actor)
        r.moved_to_lesson_id, r.moved_to_date, r.moved_to_start, r.moved_to_end = new_id, new_date.isoformat(), start, end
        self._finish(r, "rescheduled", actor, reason, via)
        self.event(r, "rescheduled", actor, {"reason": reason, "via": via, "movedTo": {"lessonId": new_id, "date": new_date.isoformat(), "startTime": start, "endTime": end},
                                              "cancelledVersion": row.version})
        return {"kind": "reschedule", "cancelledVersion": row.version, "warnings": warnings, **kept,
                "newLesson": {"id": new_id, "groupId": str(group.id), "date": new_date.isoformat(), "startTime": start, "endTime": end, "room": room, "format": fmt}}

    # ------------------------------------------------------------------------- #124: a move without a request
    def _keep_substitute(self, group: Group, sub_id: Optional[UUID], new_id: str, new_date: date, actor: UUID) -> Dict[str, Any]:
        """W1 S12 (D19): the substitute of the moved lesson leads the new one (the same assignment path as the lesson sheet's «Назначить»).  Returns the fields of the answer."""
        if sub_id is None:
            return {}
        from .ops_schemas import LessonTeacherPut

        put = LessonTeacherPut(lessonDate=new_date, lessonId=new_id, teacherId=str(sub_id), reason="Перенос урока: замена сохранена")
        out = self.teachers._put_locked(group.org_id, group, put, actor_id=actor, commit=False)
        return {"teacherId": out["teacherId"], "teacherName": out["teacherName"], "isSubstitution": out["isSubstitution"]}

    def _student_busy(self, group: Group, new_date: date, start_m: int, end_m: int, *, detail: bool = True) -> List[Dict[str, Any]]:
        """W1 S12 (D30, decision 4): the students of the group who have ANOTHER lesson of the centre at the new time - one warning ``STUDENT_BUSY`` naming them (never a block).  ``detail=False`` (a TEACHER moves his own lesson, W1 R3 14): the other groups of the centre are none of his business - only HIS students' names and the count,
        no group name, no time."""
        mine = {sid for (sid,) in self.db.query(StudentGroup.student_id)
                .filter(StudentGroup.group_id == group.id, StudentGroup.is_active.is_(True), student_not_archived())}
        if not mine:
            return []
        other_ids: Dict[UUID, Set[UUID]] = {}
        for sid, gid in self.db.query(StudentGroup.student_id, StudentGroup.group_id).filter(StudentGroup.student_id.in_(list(mine)), StudentGroup.group_id != group.id,
                                                                                            StudentGroup.is_active.is_(True)):
            other_ids.setdefault(gid, set()).add(sid)
        if not other_ids:
            return []
        iso = new_date.isoformat()
        groups = self.db.query(Group).filter(Group.id.in_(list(other_ids)), Group.org_id == group.org_id, Group.status == "active").all()
        cancelled = {(row.group_id, row.lesson_id) for row in self.db.query(GroupAttendance.group_id, GroupAttendance.lesson_id).filter(
            GroupAttendance.group_id.in_([g.id for g in groups]), GroupAttendance.lesson_date == iso, GroupAttendance.status == "cancelled")} if groups else set()
        busy: Dict[UUID, Tuple[str, str, str]] = {}
        for other in sorted(groups, key=lambda g: g.name):
            for occ in self.svc._resolved_occurrences_for_group(other, date_from=new_date, date_to=new_date):
                if (other.id, occ["id"]) in cancelled:
                    continue
                try:
                    s2, e2 = hhmm_to_minutes(occ["startTime"]), hhmm_to_minutes(occ["endTime"])
                except (KeyError, ValueError, AttributeError):
                    continue
                if start_m < e2 and end_m > s2:
                    for sid in other_ids[other.id]:
                        busy.setdefault(sid, (other.name, occ["startTime"], occ["endTime"]))
        if not busy:
            return []
        students = {st.id: st for st in self.db.query(Student).filter(Student.id.in_(list(busy))).all()}
        ordered = sorted(busy.items(), key=lambda kv: short_name(students.get(kv[0])) or "")
        if not detail:
            names = [short_name(students.get(sid)) or "Ученик" for sid, _ in ordered]
            more_names = f" и ещё {len(names) - 5}" if len(names) > 5 else ""
            return [{"code": "STUDENT_BUSY", "message": f"У {len(names)} {'ученика' if len(names) % 10 == 1 and len(names) % 100 != 11 else 'учеников'} в это время другой урок: {', '.join(names[:5])}{more_names}",
                     "students": [{"id": str(sid), "name": name} for (sid, _), name in zip(ordered[:20], names)]}]
        listed = [{"id": str(sid), "name": short_name(students.get(sid)) or "Ученик", "groupName": info[0], "startTime": info[1], "endTime": info[2]} for sid, info in ordered]
        head = "; ".join(f"{x['name']} — «{x['groupName']}» {x['startTime']}–{x['endTime']}" for x in listed[:5])
        more = f" и ещё {len(listed) - 5}" if len(listed) > 5 else ""
        return [{"code": "STUDENT_BUSY", "message": f"В это время у учеников есть другой урок: {head}{more}", "students": listed[:20]}]

    def _lesson_ref(self, group: Group, lesson_id: str, day: date, *, locked: bool = False) -> Tuple[SimpleNamespace, Dict[str, Any]]:
        """A reference to the lesson of the schedule (the fields of a request that the shared code reads) and its occurrence; 404 ``LESSON_NOT_FOUND``."""
        occ = next((o for o in self.svc._resolved_occurrences_for_group(group, date_from=day, date_to=day) if o["id"] == lesson_id), None)
        if occ is None:
            raise not_found("LESSON_NOT_FOUND", "Урок не найден")
        return self._ref_of(group, lesson_id, day, occ), occ

    @staticmethod
    def _ref_of(group: Group, lesson_id: str, day: date, occ: Dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(id=None, org_id=group.org_id, group_id=group.id, lesson_id=lesson_id, lesson_date=day.isoformat(), start_time=occ["startTime"],
                               end_time=occ["endTime"], starts_at=bc.lesson_instant_utc(day, hhmm_to_minutes(occ["startTime"])))

    def _lesson_requests(self, group: Group, lesson_id: str, iso: str) -> List[Req]:
        """Every record of one lesson, the newest first."""
        return (self.db.query(Req).filter(Req.group_id == group.id, Req.lesson_id == lesson_id, Req.lesson_date == iso)
                .order_by(Req.created_at.desc(), Req.id).all())

    def _system_request(self, group: Group, ref: Any, requester: UUID, comment: Optional[str], trigger: str, actor: UUID, prev: Optional[Req]) -> Req:
        """The record the system makes for what the administrator (or the teacher of the lesson) does on a lesson without a request of a teacher: origin ``system``, the teacher of
        the lesson as requester, the ``trigger`` in the event.  The caller decides it in the SAME transaction (a move: ``decide``; a substitution: ``_substituted``)."""
        now = self.now()
        r = Req(org_id=group.org_id, group_id=group.id, lesson_id=ref.lesson_id, lesson_date=ref.lesson_date, start_time=ref.start_time, end_time=ref.end_time, starts_at=ref.starts_at,
                requester_id=requester, origin="system", batch_id=uuid4(), reason_code="other", comment=(comment or "").strip()[:500] or None, status=PENDING,
                previous_request_id=prev.id if prev is not None else None, created_at=now, updated_at=now)
        self.db.add(r)
        self.db.flush()
        self.event(r, "requested", actor, {"reasonCode": "other", "trigger": trigger, "batchId": str(r.batch_id)})
        return r

    def assigned_without_request(self, group: Group, lesson_id: str, lesson_date: str, occ: Dict[str, Any], *, requester: UUID, teacher: UUID,
                                 asg: Optional[LessonTeacherAssignment], actor: UUID, reason: str) -> None:
        """P1.2: a substitution the administrator set on the lesson sheet with no request of the teacher is a record of the journal like a move without a request: made
        and decided ``substituted`` in the transaction of the assignment.  The newest record of the lesson stays what it was (the history); the new one points to it."""
        ref = self._ref_of(group, lesson_id, date.fromisoformat(lesson_date), occ)
        history = self._lesson_requests(group, lesson_id, lesson_date)
        r = self._system_request(group, ref, requester, reason, ASSIGN_TRIGGER, actor, history[0] if history else None)
        self._substituted(r, teacher, asg, actor, None, PUT_VIA)

    def lesson_move_check(self, org_id: Any, actor_id: Any, actor_role: str, group_id: Any, lesson_id: str, day: date, new_date: date, start: str, end: str) -> Dict[str, Any]:
        gid = _uuid(group_id)
        group = self.db.query(Group).filter(Group.id == gid, Group.org_id == org_id).first() if gid else None
        if group is None:
            raise not_found("GROUP_NOT_FOUND", "Группа не найдена")
        actor = _uuid(actor_id)
        teacher = actor if self._via(actor_role) == TEACHER_MOVE else None
        if teacher is not None:
            self._teacher_gate(group, lesson_id, day, actor)
        refusal = self._origin_refusal(group, lesson_id, day, teacher)
        ref = None
        if refusal is None:
            ref, _ = self._lesson_ref(group, lesson_id, day)
            refusal = self._check_refusal(group, ref, new_date, start, end, teacher)
        if refusal is None:
            return self._move_check(group, ref, new_date, start, end, detail=teacher is None)
        if refusal.status_code == 404:
            raise refusal
        return {"ok": False, "teacherId": None, "teacherName": None, "teacherConflicts": [], "groupConflicts": [], "warnings": [], "code": refusal.code,
                "message": refusal.message}

    @staticmethod
    def _slot_refusal(r: Any, new_date: date, start: str, end: str, start_m: int, now: datetime) -> Optional[OpsError]:
        """The rules of the NEW slot, in one place for the move and its live check: in the past / too far ahead / the same slot (``r``: a request or a lesson reference)."""
        if bc.lesson_instant_utc(new_date, start_m) <= now:
            return R.err(422, "MOVE_IN_PAST")
        if new_date > bc.business_today() + timedelta(days=R.MOVE_MAX_DAYS_AHEAD):
            return R.err(422, "MOVE_TOO_FAR")
        if new_date.isoformat() == r.lesson_date and start == r.start_time and end == r.end_time:
            return R.err(422, "MOVE_SAME_SLOT")
        return None

    def _series_refusal(self, group: Group, ref: Any, new_date: date) -> Optional[OpsError]:
        """W1 S16 (А-10): a lesson of a series that has been stopped is not moved to a date on or after the stop (the copy would be a lesson of a series that no longer exists)."""
        if str(ref.lesson_id).startswith("one-off-"):
            return None
        end = self.svc._recurring_end_override(group, ref.lesson_id)
        if end is not None and new_date.isoformat() >= end.effective_date:
            return R.err(422, "SERIES_ENDED", f"Постоянный урок прекращается с {date.fromisoformat(end.effective_date).strftime('%d.%m')} — перенесите на более раннюю дату")
        return None

    def _check_refusal(self, group: Group, ref: Any, new_date: date, start: str, end: str, teacher: Optional[UUID]) -> Optional[OpsError]:
        """What the move would refuse about the lesson in time and about the NEW slot, read only (the live check); answered BEFORE anything of another person is looked up, so a
        refusal carries no conflicts of a stranger.  ``teacher``: the teacher who moves his own lesson (his 60-minute line, his own days); ``None``: a manager."""
        now = self.now()
        if teacher is not None and self._teacher_too_late(ref.starts_at, now):
            return R.err(409, "MOVE_TOO_LATE")
        try:
            start_m, end_m = self.svc._validate_lesson_time_range(start, end)
        except ValueError as exc:
            raise R.err(422, "MOVE_FIELDS_REQUIRED", str(exc)) from exc
        refusal = self._slot_refusal(ref, new_date, start, end, start_m, now) or self._series_refusal(group, ref, new_date)
        if refusal is None and teacher is not None and TeacherBook(self.db).default_teacher(group, new_date.isoformat(), end_m) != teacher:
            return R.err(403, "NOT_LESSON_TEACHER")
        return refusal

    def _teacher_gate(self, group: Group, lesson_id: str, day: date, actor: Optional[UUID]) -> None:
        """P1.1: a teacher moves only the lesson he leads by the schedule; the substitute of one lesson does not (403 ``NOT_LESSON_TEACHER``; 404 ``LESSON_NOT_FOUND``)."""
        _, occ = self._lesson_ref(group, lesson_id, day)
        resolved = TeacherBook(self.db).resolve(group, lesson_id, day.isoformat(), hhmm_to_minutes(occ["endTime"]))
        if actor is None or resolved.teacher_id != actor or resolved.substitution:
            raise R.err(403, "NOT_LESSON_TEACHER")

    @staticmethod
    def _rejected_for(reqs: Sequence[Req], teacher: UUID) -> bool:
        """W1 S4 (D16): the administrator rejected a request of THIS teacher for the lesson - he does not move it himself afterwards."""
        return any(r.status == "rejected" and r.requester_id == teacher for r in reqs)

    @staticmethod
    def _teacher_too_late(starts_at: datetime, now: datetime) -> bool:
        return starts_at - now <= timedelta(minutes=R.TEACHER_MOVE_LEAD_MINUTES)

    def _origin_refusal(self, group: Group, lesson_id: str, day: date, teacher: Optional[UUID] = None) -> Optional[OpsError]:
        """R3 #6: the refusal POST ``lesson-move`` would give for the ORIGIN lesson (same checks, same order, same codes and texts), without writing or locking anything:
        a pending request that would be closed / a lesson that has started -> the decided / started refusals; a decided move; an archived group; no such lesson (404);
        a cancelled lesson; a journal row (the lesson has started); the planned start passed; a lesson without a teacher.  ``teacher``: the teacher who moves his own lesson (a request of somebody else waits -> refused).  ``None`` = the lesson can be moved."""
        iso = day.isoformat()
        now = self.now()
        reqs = self._lesson_requests(group, lesson_id, iso)
        if teacher is not None and self._rejected_for(reqs, teacher):
            return R.err(409, "REQUEST_REJECTED")
        pending = next((r for r in reqs if r.status == PENDING), None)
        if pending is not None:             # the decision path: ``decide`` revalidates, then ``_reschedule`` refuses a lesson that has started
            if teacher is not None and pending.requester_id != teacher:
                return R.err(409, "FOREIGN_REQUEST_PENDING")
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
            return self.already_decided(done, viewer_for(teacher, teacher=teacher is not None))
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

    def lesson_exam_binding(self, org_id: Any, group_id: Any, lesson_id: str, day: date, actor_id: Any = None, actor_role: Optional[str] = None) -> Dict[str, Any]:
        """The precheck of «Отменить урок» for the lesson sheet: ``state`` / ``title`` as ``examBinding`` of the request detail; ``canCancel`` / ``message`` from the SAME
        ``cancellation-check`` of exam-service that refuses the cancellation itself (an exam that is launched OR has attempts blocks it), so what is allowed here is not refused later."""
        gid = _uuid(group_id)
        group = self.db.query(Group).filter(Group.id == gid, Group.org_id == org_id).first() if gid else None
        if group is None:
            raise not_found("GROUP_NOT_FOUND", "Группа не найдена")
        if self._via(actor_role or "") == TEACHER_MOVE:          # the teacher's move dialog asks too (P1.1 + the exam warning of a move): only for HIS lesson
            self._teacher_gate(group, lesson_id, day, _uuid(actor_id))
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
        """The administrator - or, P1.1, the teacher who leads the lesson (a lesson more than 60 minutes ahead) - moves a future lesson without a request.  One path with
        the decision «Перенести урок»: a pending request of the lesson is decided by ``decide``; without a request a system record of the move is made (origin ``system``,
        the teacher of the lesson as requester) and decided in the SAME transaction, so the audit, the label of the student, the neutrality of package A and the payroll
        follow without a second code path.  The author is the decider (``decided_by``) and ``decided_via`` says whether he moved it as the teacher."""
        actor, gid = _uuid(actor_id), _uuid(group_id)
        group = self._group_locked(org_id, gid) if gid else None
        if group is None:
            self.db.rollback()
            raise not_found("GROUP_NOT_FOUND", "Группа не найдена")
        day = body.lessonDate
        iso = day.isoformat()
        via = self._via(actor_role)
        one = SimpleNamespace(decision="reschedule", reason=body.reason, newDate=body.newDate, startTime=body.startTime, endTime=body.endTime, room=body.room,
                              format=body.format, teacherId=None, note=None, expectedStatus=None)
        try:
            if via == TEACHER_MOVE:
                self._teacher_gate(group, body.lessonId, day, actor)
            reqs = self._lesson_requests(group, body.lessonId, iso)
            if via == TEACHER_MOVE and self._rejected_for(reqs, actor):
                raise R.err(409, "REQUEST_REJECTED")
            pending = next((r for r in reqs if r.status == PENDING), None)
            if pending is not None:
                if via == TEACHER_MOVE and pending.requester_id != actor:
                    raise R.err(409, "FOREIGN_REQUEST_PENDING")
                # R3 #1: the sheet's expected time is judged against the CURRENT lesson of the schedule (the request keeps the old time until it is re-synced);
                # a lesson that no longer exists is closed by ``decide`` itself (its revalidation), so the request's own times are only the fallback
                now_occ = next((o for o in self.svc._resolved_occurrences_for_group(group, date_from=day, date_to=day) if o["id"] == body.lessonId), None)
                self._check_expected(now_occ["startTime"] if now_occ else pending.start_time, now_occ["endTime"] if now_occ else pending.end_time, body)
                return self.decide(org_id, actor, actor_role, pending.id, one)
            done = next((r for r in reqs if r.status == "rescheduled"), None)
            if done is not None:
                if self._same_decision(done, actor, via, one):
                    item = self.one(done, viewer_for(actor, teacher=via == TEACHER_MOVE))
                    self.db.commit()
                    return {"item": item, "result": self._result_of(done)}
                raise self.already_decided(done, viewer_for(actor, teacher=via == TEACHER_MOVE))
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
            r = self._system_request(group, ref, teacher, body.reason, "teacher_move" if via == TEACHER_MOVE else "manager_move", actor, reqs[0] if reqs else None)
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
        updated, items, skipped, results = 0, [], [], []
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
                res = out["result"]
                results.append({"id": str(rid), "kind": res["kind"], "cancellationKind": res.get("cancellationKind"), "warnings": list(res.get("warnings") or [])})        # W1 R3 (D)
            except OpsError as exc:
                self.db.rollback()
                entry = {"id": str(rid), "code": exc.code, "message": exc.message}
                if exc.code == "EXAM_SERVICE_UNAVAILABLE":
                    stopped = exc
                for key in ("conflicts", "total"):
                    if key in exc.extra:
                        entry[key] = exc.extra[key]
                skipped.append(entry)
            except Exception:                # W1 S24 (D10): whatever else goes wrong with ONE request is that request's refusal - the answer is not lost after the others were committed
                self.db.rollback()
                logger.exception("bulk decision: request %s failed", rid)
                skipped.append({"id": str(rid), "code": "DECISION_FAILED", "message": "Не удалось обработать запрос. Повторите попытку"})
        return {"updated": updated, "items": items, "skipped": skipped, "results": results, "warningsTotal": sum(len(r["warnings"]) for r in results)}


class DecideRequestLike:
    """The bulk body as a ``DecideRequest`` (substitute / cancel / reject only)."""

    def __init__(self, body) -> None:
        self.decision, self.teacherId, self.note, self.reason = body.decision, body.teacherId, body.note, body.reason
        self.cancellationKind = body.cancellationKind
        self.newDate = self.startTime = self.endTime = self.room = self.format = None
        self.expectedStatus = None


# ======================================================================================================================
# hooks of the operations of package B / B2 (eager closing, D-9, D-10)
# ======================================================================================================================
def _made_by_sheet(db, requests: Sequence[Req]) -> Set[UUID]:
    """The records of ``requests`` that the lesson sheet made (P1.2): the ``requested`` event says so.  A request the system reopened (chain 6) and an administrator then decided
    from the sheet is NOT one: it keeps the decision it got."""
    ids = [r.id for r in requests]
    if not ids:
        return set()
    return {rid for (rid,) in db.query(LessonSubstitutionEvent.request_id).filter(
        LessonSubstitutionEvent.request_id.in_(ids), LessonSubstitutionEvent.action == "requested",
        LessonSubstitutionEvent.snapshot["trigger"].astext == ASSIGN_TRIGGER)}


def on_lesson_teacher_changed(svc, group: Group, lesson_id: str, lesson_date: str, *, cancelled_asg: Optional[LessonTeacherAssignment],
                              new_asg: Optional[LessonTeacherAssignment], assigned: bool, want: Any, default: Any, occ: Optional[Dict[str, Any]], reason: str,
                              actor_id: Any, skip_request_id: Any = None) -> Optional[Dict[str, Any]]:
    """Called by ``PUT lesson-teacher`` (and by a decision, with ``skip_request_id``).  Every substitution is in the journal (P1.2):
    * a pending request of the lesson that the new teacher takes over is decided ``substituted`` (``decidedVia=lesson_teacher_put``);
    * a substitution SET (``assigned``) with no request of the lesson is a system record of the main teacher (``default``), decided ``substituted`` in the same transaction;
      a lesson that has no main teacher has nobody to replace: no record;
    * a request substituted through the assignment that has just been cancelled gets the event ``assignment_cleared`` (``clearedTo``: the new teacher, ``None`` = the main
      teacher leads again); when nobody takes the place a record the sheet made is closed ``ASSIGNMENT_CLEARED``, by the administrator; a substitute replaced by another
      stays as the history and the new one points to it."""
    subs = Substitutions(svc)
    closed = None
    pending = svc.db.query(Req).filter(Req.group_id == group.id, Req.lesson_id == lesson_id, Req.lesson_date == lesson_date, Req.status == PENDING).first()
    if pending is not None and pending.id != skip_request_id and want is not None and want != pending.requester_id:
        subs._substituted(pending, want, new_asg, actor_id, None, PUT_VIA)
        closed = {"id": str(pending.id), "status": "substituted"}
    elif pending is None and assigned and occ is not None and default is not None:
        subs.assigned_without_request(group, lesson_id, lesson_date, occ, requester=default, teacher=want, asg=new_asg, actor=_uuid(actor_id), reason=reason)
    if cancelled_asg is not None:
        stood = [sr for sr in svc.db.query(Req).filter(Req.assignment_id == cancelled_asg.id, Req.status == "substituted").all() if sr.id != skip_request_id]
        by_sheet = _made_by_sheet(svc.db, stood) if new_asg is None else set()
        for sr in stood:
            subs.event(sr, "assignment_cleared", actor_id, {"teacherId": [str(cancelled_asg.teacher_id), _s(want) if new_asg is not None else None], "trigger": "put"})
            if sr.id in by_sheet:
                subs.close(sr, "ASSIGNMENT_CLEARED", actor_id=actor_id, trigger=PUT_VIA, via=PUT_VIA)
    return closed


def on_assignment_cancelled(svc, asg: LessonTeacherAssignment, actor_id: Any, trigger: str = "release", extra: Optional[Dict[str, Any]] = None) -> int:
    """An assignment was taken off by the operations of B (the group teacher became the substitute, the substitute was removed, the lesson was cancelled / moved): every
    request that was substituted through it keeps its status and gets one event ``assignment_cleared`` whose ``trigger`` says why."""
    subs = Substitutions(svc)
    rows = svc.db.query(Req).filter(Req.assignment_id == asg.id, Req.status == "substituted").all()
    for r in rows:
        subs.event(r, "assignment_cleared", actor_id, {"teacherId": [str(asg.teacher_id), None], "trigger": trigger, **(extra or {})})      # ``extra``: ``dayOffId`` when a DAY OFF took it off (#157 A4)
    return len(rows)


def on_schedule_changed(svc, group: Group, lesson_id: str, from_date: date, actor_id: Any, *, ended: bool = False) -> Dict[str, Any]:
    """W1 S8 / S9 (ДЫРА-3, А-7, А-9): the schedule of a weekly lesson was revised / stopped from ``from_date`` (the caller has flushed the new rule and commits after this):
    * a request of a teacher that is waiting for a date that is no lesson any more is closed ``LESSON_REMOVED`` (event, trigger ``schedule_changed`` / ``schedule_ended``); the one
      whose lesson only changed its time follows it;
    * a substitute assigned to a date that is no lesson any more is released (``assignment_cleared``, the same trigger);
    * ``ended``: a cancellation of a date that is no lesson any more (a day off, a hand cancellation set in advance) is removed - what it was is returned for the audit; a cancelled
      ORIGINAL of a move stays (its copy lives on).
    Returns ``{"closedRequests", "releasedSubstitutes", "removedCancellations"}``."""
    trigger = "schedule_ended" if ended else "schedule_changed"
    subs = Substitutions(svc)
    db = svc.db
    iso = from_date.isoformat()
    book = TeacherBook(db)
    closed = released = 0
    for r in db.query(Req).filter(Req.group_id == group.id, Req.lesson_id == lesson_id, Req.lesson_date >= iso, Req.status == PENDING).all():
        reason, occ = subs.closure_reason(r, group, book)
        if reason == "LESSON_REMOVED":
            subs.close(r, reason, actor_id=actor_id, trigger=trigger, group=group)
            closed += 1
        elif reason is None and occ and (occ.get("startTime") != r.start_time or occ.get("endTime") != r.end_time):
            subs._sync_times(r, occ)
    for asg in db.query(LessonTeacherAssignment).filter(LessonTeacherAssignment.group_id == group.id, LessonTeacherAssignment.lesson_id == lesson_id,
                                                       LessonTeacherAssignment.lesson_date >= iso, LessonTeacherAssignment.cancelled_at.is_(None)).all():
        day = date.fromisoformat(asg.lesson_date)
        if next((o for o in svc._resolved_occurrences_for_group(group, date_from=day, date_to=day) if o["id"] == lesson_id), None) is None:
            subs.teachers.cancel_assignment(asg, actor_id, group=group, reason="Урок исчез из расписания: замена снята", trigger=trigger)
            released += 1
    removed: List[Dict[str, Any]] = []
    if ended:
        moved = {r.lesson_date for r in db.query(Req.lesson_date).filter(Req.group_id == group.id, Req.lesson_id == lesson_id, Req.lesson_date >= iso, Req.status == "rescheduled")}
        for row in db.query(GroupAttendance).filter(GroupAttendance.group_id == group.id, GroupAttendance.lesson_id == lesson_id, GroupAttendance.lesson_date >= iso,
                                                    GroupAttendance.status == "cancelled").all():
            if row.lesson_date in moved:
                continue
            removed.append({"date": row.lesson_date, "cancellationKind": row.cancellation_kind, "cancellationReason": row.cancellation_reason, "dayOffId": _s(row.day_off_id)})
            db.delete(row)
        db.flush()
    return {"closedRequests": closed, "releasedSubstitutes": released, "removedCancellations": removed}


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


def on_member_removed(svc, org_id: Any, user_id: UUID, actor_id: Any, released: Sequence[Tuple[LessonTeacherAssignment, Group]], *, role_changed: bool = False) -> Tuple[int, int]:
    """After a member was removed - or, with ``role_changed``, stopped being a teacher (W1 round 2: only a teacher leads lessons) - the ONE function of both (the caller commits):
    his pending requests are closed ``TEACHER_REMOVED`` (``ROLE_CHANGED``, trigger ``role_changed``); the requests whose assignment (HIM as the substitute) was just released are
    reopened as new system requests when the requester still leads the lesson (D-10); the requests that suggested him get one ``suggestion_unavailable`` event.
    Returns ``(closed, reopened)``."""
    subs = Substitutions(svc)
    db = svc.db
    now = bc.utc_now()
    closed = 0
    for r in db.query(Req).filter(Req.org_id == org_id, Req.requester_id == user_id, Req.status == PENDING).all():
        r = db.query(Req).filter(Req.id == r.id).with_for_update().populate_existing().first()          # (R3 5) a decision that came meanwhile wins: the row is read again under its lock
        if r is None or r.status != PENDING:
            continue
        subs.close(r, "ROLE_CHANGED" if role_changed else "TEACHER_REMOVED", actor_id=actor_id, trigger="role_changed" if role_changed else "member_removed")
        closed += 1
    for r in db.query(Req).filter(Req.org_id == org_id, Req.suggested_teacher_id == user_id, Req.status == PENDING).all():
        if any((e.snapshot or {}).get("teacherId") == str(user_id) for e in db.query(LessonSubstitutionEvent).filter(LessonSubstitutionEvent.request_id == r.id, LessonSubstitutionEvent.action == "suggestion_unavailable")):
            continue                        # told once (the cleanup script may run again)
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
                  comment=f"Заменяющий {short_name(removed_user)} больше не {'ведёт уроки' if role_changed else 'работает в центре'}. Нужно новое решение.", suggested_teacher_id=None,
                  status=PENDING, previous_request_id=old.id, created_at=now, updated_at=now)
        db.add(new)
        db.flush()
        subs.event(new, "reopened", actor_id, {"previousRequestId": str(old.id), "removedTeacherId": str(user_id), **({"cause": "role_changed"} if role_changed else {})})
        reopened += 1
    return closed, reopened


def reopen_requests_closed_by_day_off(svc, day_off, atts: Sequence[GroupAttendance], actor_id: Any) -> int:
    """W1 S10+ (Д28): «Отменить выходной» gives lessons back; the requests of teachers that THIS day off closed (``closed`` / ``LESSON_CANCELLED`` with the day off in the event) are opened
    again as new system requests (event ``reopened``) when the lesson is still ahead, the requester still leads it and nothing waits there.  The caller commits.  Returns how many.
    #157 A4: EVERY request this day off closed gets the event ``day_off_voided`` (``reopened``: whether a new one was opened) - also in the four cases that stay closed (the lesson passed,
    the group is archived, another request waits, the requester no longer leads it): the old record must not look finished while the lesson is back."""
    subs = Substitutions(svc)
    db = svc.db
    book = TeacherBook(db)
    now = bc.utc_now()
    reopened = 0
    for att in atts:
        old = (db.query(Req).filter(Req.group_id == att.group_id, Req.lesson_id == att.lesson_id, Req.lesson_date == att.lesson_date, Req.status == "closed",
                                    Req.closed_reason == "LESSON_CANCELLED").order_by(Req.created_at.desc(), Req.id).first())
        if old is None:
            continue
        closing = (db.query(LessonSubstitutionEvent).filter(LessonSubstitutionEvent.request_id == old.id, LessonSubstitutionEvent.action == "closed")
                   .order_by(LessonSubstitutionEvent.created_at.desc()).first())
        if closing is None or (closing.snapshot or {}).get("dayOffId") != str(day_off.id):
            continue
        new = None
        group = db.query(Group).filter(Group.id == att.group_id).first()
        if (old.starts_at > now and group is not None and group.status == "active"
                and not db.query(Req.id).filter(Req.group_id == att.group_id, Req.lesson_id == att.lesson_id, Req.lesson_date == att.lesson_date, Req.status == PENDING).first()
                and book.default_teacher(group, att.lesson_date, hhmm_to_minutes(old.end_time)) == old.requester_id):
            new = Req(org_id=old.org_id, group_id=old.group_id, lesson_id=old.lesson_id, lesson_date=old.lesson_date, start_time=old.start_time, end_time=old.end_time, starts_at=old.starts_at,
                      requester_id=old.requester_id, origin="system", batch_id=uuid4(), reason_code=old.reason_code,
                      comment=f"Выходной «{day_off.reason}» отменён: урок вернулся. Нужно новое решение."[:500],
                      suggested_teacher_id=None, status=PENDING, previous_request_id=old.id, created_at=now, updated_at=now)
            db.add(new)
            db.flush()
            subs.event(new, "reopened", actor_id, {"previousRequestId": str(old.id), "dayOffId": str(day_off.id)})
            reopened += 1
        subs.event(old, "day_off_voided", actor_id, {"dayOffId": str(day_off.id), "reopened": new is not None, "subject": "request", "needsDecision": new is not None})
    return reopened


def mark_substitutions_not_restored(svc, day_off, atts: Sequence[GroupAttendance], actor_id: Any) -> int:
    """#157 A4: the lessons «Отменить выходной» gives back WITHOUT the substitute the day off took off them - the SAME condition as the warning ``SUBSTITUTE_NOT_RESTORED`` of the answer
    (``DaysOffService.lost_substitution``) - mark the records of that substitution with the event ``day_off_voided`` (``subject: substitution``, ``reopened: false``).  ``needsDecision``:
    a request of a teacher stood behind the substitution (it was decided by it) - the teacher still cannot lead, somebody has to be assigned again; a substitution the administrator set himself needs nothing.
    The caller commits.  Returns how many."""
    subs = Substitutions(svc)
    days_off = DaysOffService(svc)
    marked = 0
    for att in atts:
        asg = days_off.lost_substitution(att)
        if asg is None:
            continue
        for r in svc.db.query(Req).filter(Req.assignment_id == asg.id, Req.org_id == day_off.org_id).all():
            by_hand = (svc.db.query(LessonSubstitutionEvent.id).filter(LessonSubstitutionEvent.request_id == r.id, LessonSubstitutionEvent.action == "requested",
                                                                      LessonSubstitutionEvent.snapshot["trigger"].astext == ASSIGN_TRIGGER).first() is not None)
            subs.event(r, "day_off_voided", actor_id, {"dayOffId": str(day_off.id), "reopened": False, "subject": "substitution", "needsDecision": not by_hand})
            marked += 1
    return marked


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


def one_off_authors(svc, lesson_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """``{one-off lesson id: {id, name, role}}`` - who added the one-off lesson (visibility only: no money follows from it)."""
    ids = sorted({i for i in lesson_ids if i and str(i).startswith("one-off-")})
    if not ids:
        return {}
    rows = svc.db.query(GroupScheduleOneOffLesson.id, GroupScheduleOneOffLesson.created_by, GroupScheduleOneOffLesson.org_id).filter(GroupScheduleOneOffLesson.id.in_(ids)).all()
    authors = {r.created_by for r in rows if r.created_by}
    names = {u.id: u for u in svc.db.query(User).filter(User.id.in_(authors)).all()} if authors else {}
    roles = {(m.org_id, m.user_id): m.org_role for m in svc.db.query(OrganizationMember).filter(OrganizationMember.user_id.in_(authors)).all()} if authors else {}
    return {r.id: {"id": str(r.created_by), "name": short_name(names.get(r.created_by)), "role": roles.get((r.org_id, r.created_by))} for r in rows if r.created_by}


def record_one_off_added(svc, group: Group, lesson_id: str, day: date, payload: Dict[str, Any], actor: UUID) -> None:
    """A TEACHER added a one-off lesson (it is paid to him and spends the package of the students without anybody's approval - the money part is a later pack): the journal «Замены» says so
    (``closed`` / ``ONE_OFF_ADDED``, origin ``system``, trigger ``teacher_one_off``, the teacher as the requester).  The caller commits."""
    subs = Substitutions(svc)
    ref = subs._ref_of(group, lesson_id, day, payload)
    record = subs._system_request(group, ref, actor, None, "teacher_one_off", actor, None)
    now = subs.now()
    record.status, record.closed_reason, record.decided_at, record.decided_via, record.decided_by, record.decision_note, record.updated_at = (
        "closed", "ONE_OFF_ADDED", now, "system", None, None, now)
    subs.event(record, "closed", actor, {"closedReason": "ONE_OFF_ADDED", "trigger": "teacher_one_off"})


def annotate_occurrences(svc, user: Dict[str, Any], items: Sequence[Any]) -> Sequence[Any]:
    """``substitutionRequest`` (manager and the requester) and ``substitutionOutcome`` (everybody who sees the schedule) on the occurrences of ``GET /org/schedule/occurrences``."""
    if not items:
        return items
    from .attendance_access import is_org_manager

    manager, actor = is_org_manager(user), _uuid(user.get("user_id"))
    gids = {g for g in (_uuid(i.groupId) for i in items) if g}
    origin = moved_from_map(svc.db, gids, [i.id for i in items])
    authors = one_off_authors(svc, [i.id for i in items])
    purposes = OneOffPurpose(svc).marks([(i.groupId, i.id, i.occurrenceDate) for i in items], manager=manager, actor=actor)          # #154: purpose / madeUp* / money (the mask is in ``marks``)
    for item in items:
        item.movedFrom = origin.get((str(item.groupId), item.id))
        item.createdBy = authors.get(item.id)
        apply_marks(item, purposes.get((str(item.groupId), item.id, item.occurrenceDate)))
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
        done = lesson_outcomes(group)                    # W1 S5: the newest of a cancellation / a move and a restore (a restored lesson has no outcome)
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
    authors = one_off_authors(svc, [l.get("id") for l in keys.values()])
    manager, actor = is_org_manager(user), _uuid(user.get("user_id"))
    purposes = OneOffPurpose(svc).marks([(group.id, l.get("id"), l.get("date")) for l in keys.values()], manager=manager, actor=actor)          # #154
    for lesson in keys.values():
        if (str(group.id), lesson.get("id")) in origin:
            lesson["movedFrom"] = origin[(str(group.id), lesson.get("id"))]
        if lesson.get("id") in authors:
            lesson["createdBy"] = authors[lesson["id"]]
        apply_marks(lesson, purposes.get((str(group.id), lesson.get("id"), lesson.get("date"))))
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
    by_lesson: Dict[str, List[Req]] = {}
    for r in reqs:
        by_lesson.setdefault(f"{r.lesson_date}_{r.lesson_id}", []).append(r)
    done = {key: out for key, out in ((key, lesson_outcomes(group_reqs)) for key, group_reqs in by_lesson.items()) if out is not None}
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
        purposes = OneOffPurpose(svc).marks([(l["group_id"], l["lesson_id"], l["date"]) for l in lessons], manager=False, money_visible=False)       # #154: a neutral label, never the money
        for lesson in lessons:
            if (lesson["group_id"], lesson["lesson_id"]) in origin:
                lesson["movedFrom"] = origin[(lesson["group_id"], lesson["lesson_id"])]
            apply_marks(lesson, purposes.get((lesson["group_id"], lesson["lesson_id"], lesson["date"])))
            row = rows.get((lesson["group_id"], lesson["lesson_id"], lesson["date"]))
            group = groups.get(_uuid(lesson["group_id"]))
            teacher = row.teacher_id if row is not None else None
            lesson["teacherName"] = short_name(names.get(str(teacher))) if teacher else None
            default = book.default_teacher(group, lesson["date"], None) if group is not None else None
            lesson["isSubstitution"] = bool(teacher is not None and default is not None and teacher != default)
    sid = str(student_id)
    notices: List[Dict[str, Any]] = []
    own_groups = [gid for (gid,) in db.query(StudentGroup.group_id).filter(StudentGroup.student_id == student_id)]      # only the groups he is / was in
    every = (db.query(Req).filter(Req.org_id == org_id, Req.group_id.in_(own_groups), Req.lesson_date >= d_from, Req.lesson_date <= d_to,
                                  or_(Req.status.in_(("cancelled", "rescheduled")), (Req.status == "closed") & (Req.closed_reason == RESTORED)))
             .order_by(Req.created_at, Req.id).all()) if own_groups else []
    by_lesson: Dict[Tuple[Any, str, str], List[Req]] = {}
    for r in every:
        by_lesson.setdefault((r.group_id, r.lesson_id, r.lesson_date), []).append(r)
    reqs = sorted((out for out in (lesson_outcomes(group_reqs) for group_reqs in by_lesson.values()) if out is not None), key=lambda r: (r.lesson_date, str(r.id)))   # W1 S5: the newest outcome only
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
    # PAY-F: the lessons a DAY OFF of the centre cancelled carry its reason («Урок отменён: выходной (Хайит)»); the reason of any other cancellation is never sent (D-12).
    # A cancelled lesson has no frozen roster: the student belongs to it when one of his stays in the group covers its day.
    if own_groups:
        stays: Dict[Any, List[Tuple[date, Optional[date]]]] = {}
        member_group = {m.id: m.group_id for m in db.query(StudentGroup).filter(StudentGroup.student_id == student_id, StudentGroup.group_id.in_(own_groups))}
        for mid, periods in periods_by_membership(db, member_group).items():
            stays.setdefault(member_group[mid], []).extend(
                (bc.utc_to_business_wall(p.joined_at).date(), bc.utc_to_business_wall(p.left_at).date() if p.left_at else None) for p in periods)
        for row, day_off in (db.query(GroupAttendance, OrgDayOff).join(OrgDayOff, OrgDayOff.id == GroupAttendance.day_off_id)
                             .filter(GroupAttendance.group_id.in_(own_groups), GroupAttendance.status == "cancelled",
                                     GroupAttendance.lesson_date >= d_from, GroupAttendance.lesson_date <= d_to)
                             .order_by(GroupAttendance.lesson_date, GroupAttendance.lesson_id).all()):
            day = date.fromisoformat(row.lesson_date)
            if not any(first <= day and (last is None or day <= last) for first, last in stays.get(row.group_id, [])):
                continue
            group = db.query(Group).filter(Group.id == row.group_id).first()
            notices.append({"lessonId": row.lesson_id, "date": row.lesson_date, "groupId": str(row.group_id), "groupName": group.name if group else "",
                            "kind": "cancelled", "movedTo": None, "holiday": day_off.reason})
    summary["notices"] = notices
    return summary
