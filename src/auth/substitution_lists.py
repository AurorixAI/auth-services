"""LESSON-RULES С7: the lists of «Замены» as ONE list each, in the order the owner reads them.

* «Ждут решения» (``status = pending``): the waiting requests of the teachers AND the cancellations that wait for the owner (``group_attendance.cancellation_kind = 'unconfirmed'``).
* «История» (``status = decided``): the decided requests AND the entries that are built when the list is READ from the journal of attendance and of the groups - a lesson «оформлен задним
  числом» (completed on a later day than its date), «отметки исправлены» (a held lesson corrected), «добавил урок в расписание» (a weekly lesson created by a teacher).  Nothing is written for
  them (the facts are in the audit already: no second copy, no new row in ``lesson_substitution_requests``).  A waiting cancellation is NOT in the history (it has no outcome yet); after the
  decision it is read from its record as before.
The branches are joined by ``UNION ALL`` over common columns (``COLUMNS``): the order, the count and the page belong to the database, so the number of the queries of a list stays what it was (the
count and the page).  ``backdated_only`` keeps the entries that look BACK (``backdated``, ``corrected``) - the owner's flag «Только задним числом».  Every entry is a function of the facts at the
moment of the reading.

Not here: the sums (the window of the decision asks ``dry_run``), the payroll marks (the pack PAYROLL-MONTH)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

from sqlalchemy import Boolean, Date, Integer, String, and_, case, cast, func, literal, null, or_, select, text, union_all
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Session, aliased

from . import business_clock as bc
from .models import Group, GroupAttendance, GroupAttendanceAudit, GroupAudit, OrganizationMember, User

BACKDATED, CORRECTED, LESSON_ADDED, UNCONFIRMED, REQUEST = "backdated", "corrected", "lesson_added", "unconfirmed", "request"
LOOKING_BACK = (BACKDATED, CORRECTED)
UUID_T = PGUUID(as_uuid=True)
TYPES = {"kind": String, "at": None, "overdue": Boolean, "req_id": UUID_T, "ref_id": UUID_T, "group_id": UUID_T, "group_name": String, "lesson_id": String, "lesson_date": String,
         "actor_id": UUID_T, "actor_last": String, "actor_first": String, "actor_role": String, "teacher_id": UUID_T, "teacher_last": String, "teacher_first": String,
         "late_days": Integer, "reason": String}
COLUMNS = tuple(TYPES)


def _hours() -> int:
    """The offset of the business clock in whole hours (Tashkent, no DST); the one source is ``business_clock``."""
    return int((bc.utc_to_business_wall(datetime(2000, 1, 1)) - datetime(2000, 1, 1)).total_seconds() // 3600)


def business_day(column):
    """The business day of a UTC instant column, in SQL."""
    return func.date(column + text(f"interval '{_hours()} hours'"))


def cols(kind, at, **given):
    """The columns of a branch in the order of ``COLUMNS``; what a branch does not know is a typed NULL (``UNION ALL`` needs the same shape everywhere)."""
    out = [(kind if not isinstance(kind, str) else literal(kind)).label("kind"), at.label("at")]
    for name in COLUMNS[2:]:
        value = given.get(name)
        out.append((value if value is not None else cast(null(), TYPES[name])).label(name))
    return out


def request_branch(q, at, overdue):
    """The records of the teachers: ``q`` is the ``Query`` of ``Req`` with every filter of the list already applied; ``at`` the sort column, ``overdue`` the flag of the pending order."""
    from .models import LessonSubstitutionRequest as Req

    return q.with_entities(*cols(REQUEST, at, overdue=overdue, req_id=Req.id)).order_by(None).statement


def _names(user):
    return {"last": user.last_name, "first": user.first_name}


def cancelled_at(g):
    """SQL: the moment the cancellation of the row ``g`` was WRITTEN - the newest line of the journal «cancelled» of the row (a day off set and voided, a kind changed back touch ``updated_at`` and are not a
    new cancellation); the row's own ``updated_at`` only for a row without such a line."""
    newest = (select(func.max(GroupAttendanceAudit.created_at)).where(GroupAttendanceAudit.attendance_id == g.id, GroupAttendanceAudit.action.in_(("cancelled", "reconciliation_cancelled")))
              .correlate(g).scalar_subquery())
    return func.coalesce(newest, g.updated_at)


def unconfirmed_branch(org_id: Any, *, group_id: Optional[UUID], teacher_id: Optional[UUID], date_from: Optional[str], date_to: Optional[str], like: Optional[str]):
    """The cancellations that wait for the owner: ``kind = unconfirmed``; the sort column is the moment the cancellation was written (the oldest wait first)."""
    g, grp, actor, teacher, member = GroupAttendance, aliased(Group), aliased(User), aliased(User), aliased(OrganizationMember)
    moment = cancelled_at(g)
    stmt = (select(*cols(UNCONFIRMED, moment, overdue=literal(True), ref_id=g.id, group_id=g.group_id, group_name=grp.name, lesson_id=g.lesson_id, lesson_date=g.lesson_date,
                         actor_id=g.marked_by, actor_last=actor.last_name, actor_first=actor.first_name, actor_role=member.org_role, teacher_id=g.teacher_id,
                         teacher_last=teacher.last_name, teacher_first=teacher.first_name, reason=g.cancellation_reason))
            .select_from(g).join(grp, grp.id == g.group_id).outerjoin(actor, actor.id == g.marked_by).outerjoin(teacher, teacher.id == g.teacher_id)
            .outerjoin(member, and_(member.user_id == g.marked_by, member.org_id == g.org_id))
            .where(g.org_id == org_id, g.status == "cancelled", g.cancellation_kind == "unconfirmed"))
    if group_id is not None:
        stmt = stmt.where(g.group_id == group_id)
    if teacher_id is not None:
        stmt = stmt.where(g.teacher_id == teacher_id)
    if date_from:
        stmt = stmt.where(g.lesson_date >= date_from)
    if date_to:
        stmt = stmt.where(g.lesson_date <= date_to)
    if like:
        stmt = stmt.where(or_(grp.name.ilike(like, escape="\\"), teacher.last_name.ilike(like, escape="\\"), teacher.first_name.ilike(like, escape="\\")))
    return stmt


def attendance_branch(org_id: Any, *, since: Optional[datetime], group_id: Optional[UUID], teacher_id: Optional[UUID], date_from: Optional[str], date_to: Optional[str], like: Optional[str]):
    """The entries of the journal of attendance: ``backdated`` (a lesson completed on a LATER day than its date; ``late_days`` = how many days later) and ``corrected`` (a held lesson corrected).
    The actor is whoever wrote the line (a teacher or an administrator over somebody's lesson); the teacher is the one the lesson is frozen with.
    Accepted (C12.3): the threshold «12 hours» of a lesson STARTED in time counts from ``started_at`` (the end of a lesson is not kept in the journal)."""
    a, g, grp, actor, teacher, member = GroupAttendanceAudit, aliased(GroupAttendance), aliased(Group), aliased(User), aliased(User), aliased(OrganizationMember)
    late = business_day(a.created_at) - cast(a.lesson_date, Date)
    # a lesson arranged afterwards (``started_at`` empty) is «late» by the day the teacher BEGAN to arrange it (the line ``reconciliation_started``) - not by the moment of the completion (C12.2: begun in
    # his own day at 23:58 and completed at 00:02 is no entry, as C4 does not refuse it); no such line (an old row): the completion, as before
    arranged = (select(func.min(GroupAttendanceAudit.created_at)).where(GroupAttendanceAudit.attendance_id == g.id, GroupAttendanceAudit.action == "reconciliation_started")
                .correlate(g).scalar_subquery())
    late_done = case((g.started_at.is_(None), business_day(func.coalesce(arranged, a.created_at))), else_=business_day(a.created_at)) - cast(a.lesson_date, Date)
    # a lesson STARTED in time (``started_at`` filled) is closed «later» only when it is closed more than 12 hours after it was started (the end of the lesson is not kept in the journal; the start is its lower
    # bound, so an evening lesson closed after midnight is not «задним числом»); a lesson arranged afterwards (``started_at`` empty) - by the difference of the dates
    wall = a.created_at + text(f"interval '{_hours()} hours'")
    in_time = and_(g.started_at.isnot(None), wall <= g.started_at + text("interval '12 hours'"))
    done_late = and_(a.action == "updated", a.before_status == "in_progress", a.after_status == "completed", late_done >= 1, ~in_time)    # the completion writes ``updated`` (service.py)
    corrected = and_(a.action == "corrected", a.before_status == "completed", a.after_status == "completed")
    stmt = (select(*cols(case((done_late, literal(BACKDATED)), else_=literal(CORRECTED)), a.created_at, overdue=literal(True), ref_id=a.id, group_id=a.group_id, group_name=grp.name,
                         lesson_id=a.lesson_id, lesson_date=a.lesson_date, actor_id=a.actor_id, actor_last=actor.last_name, actor_first=actor.first_name, actor_role=member.org_role,
                         teacher_id=g.teacher_id, teacher_last=teacher.last_name, teacher_first=teacher.first_name, late_days=cast(case((done_late, late_done), else_=late), Integer), reason=a.correction_reason))
            .select_from(a).join(g, g.id == a.attendance_id).join(grp, grp.id == a.group_id).outerjoin(actor, actor.id == a.actor_id).outerjoin(teacher, teacher.id == g.teacher_id)
            .outerjoin(member, and_(member.user_id == a.actor_id, member.org_id == a.org_id))
            .where(a.org_id == org_id, a.action.in_(("updated", "corrected")), or_(done_late, corrected)))          # the action in the WHERE: the partial index ``ix_gaa_org_done_corrected`` is used only when the query implies its condition
    if since is not None:
        stmt = stmt.where(a.created_at >= since)
    if group_id is not None:
        stmt = stmt.where(a.group_id == group_id)
    if teacher_id is not None:
        stmt = stmt.where(or_(g.teacher_id == teacher_id, a.actor_id == teacher_id))
    if date_from:
        stmt = stmt.where(a.lesson_date >= date_from)
    if date_to:
        stmt = stmt.where(a.lesson_date <= date_to)
    if like:
        stmt = stmt.where(or_(grp.name.ilike(like, escape="\\"), teacher.last_name.ilike(like, escape="\\"), teacher.first_name.ilike(like, escape="\\")))
    return stmt


def lesson_added_branch(org_id: Any, *, since: Optional[datetime], group_id: Optional[UUID], teacher_id: Optional[UUID], like: Optional[str]):
    """«Добавил урок в расписание»: a weekly lesson created by a teacher (the line of the journal of the group written when the lesson is created; the owner learns of it here)."""
    a, grp, actor, member = GroupAudit, aliased(Group), aliased(User), aliased(OrganizationMember)
    stmt = (select(*cols(LESSON_ADDED, a.created_at, overdue=literal(True), ref_id=a.id, group_id=a.group_id, group_name=grp.name, actor_id=a.actor_id, actor_last=actor.last_name,
                         actor_first=actor.first_name, actor_role=member.org_role, teacher_id=a.actor_id, teacher_last=actor.last_name, teacher_first=actor.first_name,
                         lesson_id=a.snapshot["lessonId"].astext, lesson_date=a.snapshot["validFrom"].astext))
            .select_from(a).join(grp, grp.id == a.group_id).outerjoin(actor, actor.id == a.actor_id).outerjoin(member, and_(member.user_id == a.actor_id, member.org_id == a.org_id))
            .where(a.org_id == org_id, a.action == "lesson_added", a.actor_role == "teacher"))
    if since is not None:
        stmt = stmt.where(a.created_at >= since)
    if group_id is not None:
        stmt = stmt.where(a.group_id == group_id)
    if teacher_id is not None:
        stmt = stmt.where(a.actor_id == teacher_id)
    if like:
        stmt = stmt.where(or_(grp.name.ilike(like, escape="\\"), actor.last_name.ilike(like, escape="\\"), actor.first_name.ilike(like, escape="\\")))
    return stmt


def page(db: Session, branches: Sequence[Any], *, pending: bool, limit: int, offset: int):
    """``(total, rows)``: the count and the page of the union, ordered as the list is read - the waiting first, the oldest first (``pending``), else the newest first."""
    union = union_all(*branches).subquery("lst")
    total = db.execute(select(func.count()).select_from(union)).scalar() or 0
    key = func.coalesce(union.c.req_id, union.c.ref_id)
    order = (union.c.overdue.desc(), union.c.at.asc(), key) if pending else (union.c.at.desc(), key)
    rows = db.execute(select(union).order_by(*order).offset(offset).limit(limit)).all()
    return int(total), rows


def person(row, prefix: str, ident: str) -> Optional[Dict[str, Any]]:
    """``{id, name, role}`` of the actor / the teacher of a row (``Фамилия И.``), ``None`` when the row has nobody."""
    pid = getattr(row, ident)
    if pid is None:
        return None
    last, first = (getattr(row, f"{prefix}_last") or "").strip(), (getattr(row, f"{prefix}_first") or "").strip()
    name = f"{last} {first[0]}." if last and first else (last or first or None)
    return {"id": str(pid), "name": name, "role": getattr(row, "actor_role", None) if prefix == "actor" else None}


def days_word(n: int) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


def entry_text(row) -> str:
    """The words of an entry (the server speaks, the screen shows them as they are)."""
    if row.kind == BACKDATED:
        return f"Урок оформлен задним числом: на {days_word(row.late_days or 0)} позже даты урока"
    if row.kind == CORRECTED:
        return "Отметки исправлены" + (f": {row.reason}" if row.reason else "")
    if row.kind == LESSON_ADDED:
        return "Добавил урок в расписание"
    return ""


def audit_item(row, occ: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One entry built from the journal (the history list): no request id - ``source: audit``, the id is the id of the line of the journal.  ``occ``: the lesson of the schedule - its time (LESSON-RULES С10.3:
    the same ``startTime`` / ``endTime`` as the entries of the requests; ``None`` when the lesson is no more in the schedule)."""
    return {"source": "audit", "id": str(row.ref_id), "kind": row.kind, "at": row.at.isoformat(), "group": {"id": str(row.group_id), "name": row.group_name},
            "lesson": {"lessonId": row.lesson_id, "date": row.lesson_date, "startTime": occ["startTime"] if occ else None, "endTime": occ["endTime"] if occ else None},
            "teacher": person(row, "teacher", "teacher_id"), "by": person(row, "actor", "actor_id"),
            "lateDays": int(row.late_days) if row.late_days is not None else None, "reason": row.reason, "text": entry_text(row)}
