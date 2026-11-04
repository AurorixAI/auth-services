"""Money-1 (A3, I-5): what an action does to money, in the answer of the server - one shape for every action that moves it.

``{"students": [...], "payroll": [...], "text": "..."}``:

* ``students`` - the lessons of the packs of the students that the action gives back (+1) or spends (-1), by the rule 4.3 (``payment_status.lesson_counts``);
* ``payroll`` - the SAVED sheets of the lesson's teacher the action touches.  Everybody gets ``{"month", "state"}`` (state of the period: ``open`` | ``closed`` | ``reopened``, see
  ``closed_period.py``); only the OWNER gets the sheet, the teacher, the saved total and - after the action - the fresh total and ``changedBy`` (LESSONS 45: the money of the teachers
  belongs to the owner, also in the answers of other people's actions);
* ``text`` - the sentence for the confirmation / the toast, in the words of the screen (no technical words).

A dry run (``dryRun`` / ``dry_run``) answers the same shape without writing: the owner then has no amounts (the sheet changes only when the action is done), the rest is as after the action.
Nothing here commits.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from uuid import UUID

from src.models.auth import PayrollPayout, PayrollSheet, Student, User

from . import payment_status as ps
from .closed_period import month_first, month_title, period_state
from .ops_rules import short_name


def counted(mark: Optional[str], excuse_status: Optional[str]) -> bool:
    return ps.lesson_counts(mark=mark, excuse_status=excuse_status)


def student_deltas(before: Dict[str, Optional[str]], after: Dict[str, Optional[str]], status_before: Dict[str, Optional[str]], status_after: Dict[str, Optional[str]],
                   *, held_before: bool = True, held_after: bool = True) -> List[Dict[str, Any]]:
    """``[{"studentId", "delta"}]`` of the students whose pack gets a lesson back (+1) or spends one (-1) by the change; ``before`` / ``after`` are the marks, ``status_*`` the excuse statuses by
    student, ``held_*`` whether the lesson is completed before / after (only a completed lesson spends a pack, rule 4.3)."""
    out = []
    for sid in sorted(set(before) | set(after)):
        was = held_before and counted(before.get(sid), status_before.get(sid))
        will = held_after and counted(after.get(sid), status_after.get(sid))
        if was != will:
            out.append({"studentId": sid, "delta": (1 if was else 0) - (1 if will else 0)})       # +1: a lesson goes back into the pack, -1: one is spent
    return out


def package_only(db, org_id: Any, group_id: Any, deltas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Only the students who pay by LESSONS (a package) have a pack to spend: a mark never changes the account of a student who pays by the calendar (area-attendance.md 4.4)."""
    if not deltas or group_id is None:
        return deltas
    from src.models.auth import StudentGroup

    from .payments_service import PaymentService

    members = {str(sid): mid for mid, sid in db.query(StudentGroup.id, StudentGroup.student_id).filter(
        StudentGroup.group_id == group_id, StudentGroup.student_id.in_([UUID(d["studentId"]) for d in deltas])).all()}
    if not members:
        return []
    modes = {e.membership.id: e.ledger.mode for e in PaymentService(db)._loader(str(org_id), recalc=False).run(list(members.values()))}
    return [d for d in deltas if modes.get(members.get(d["studentId"])) == "lessons"]


def with_names(db, deltas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not deltas:
        return deltas
    ids = [UUID(d["studentId"]) for d in deltas]
    names = {str(s.id): f"{s.last_name or ''} {s.first_name or ''}".strip() for s in db.query(Student).filter(Student.id.in_(ids)).all()}
    return [{**d, "name": names.get(d["studentId"])} for d in deltas]


def payroll_effects(db, org_id: Any, pairs: Iterable[Tuple[Any, Any]], *, owner: bool, amounts: bool = True) -> List[Dict[str, Any]]:
    """The saved sheets of ``(teacher_id, day or month)`` pairs that an action touches.  Teachers without a saved sheet of that month are not listed (nothing to change)."""
    seen: Set[Tuple[UUID, date]] = set()
    out: List[Dict[str, Any]] = []
    for teacher, day in pairs:
        if teacher is None:
            continue
        key = (teacher if isinstance(teacher, UUID) else UUID(str(teacher)), month_first(day))
        if key in seen:
            continue
        seen.add(key)
        sheet = db.query(PayrollSheet).filter(PayrollSheet.org_id == org_id, PayrollSheet.teacher_id == key[0], PayrollSheet.period_month == key[1]).first()
        if sheet is None:
            continue
        state = period_state(db, org_id, key[0], key[1])["state"]
        line: Dict[str, Any] = {"month": key[1].strftime("%Y-%m"), "state": state}
        if owner:
            user = db.query(User).filter(User.id == key[0]).first()
            paid = int(sum(p.amount for p in db.query(PayrollPayout).filter(PayrollPayout.sheet_id == sheet.id, PayrollPayout.voided_at.is_(None)).all()))
            line.update({"sheetId": str(sheet.id), "teacherId": str(key[0]), "teacherName": short_name(user), "savedTotal": int(sheet.total_amount), "paid": paid})
            if amounts:
                from .payroll_service import PayrollService

                fresh = PayrollService(db)._fresh_total(str(org_id), sheet)
                line.update({"freshTotal": fresh, "changedBy": (fresh - int(sheet.total_amount)) if fresh is not None else None})
        out.append(line)
    return out


def text_of(students: List[Dict[str, Any]], payroll: List[Dict[str, Any]], *, done: bool) -> str:
    """The sentence under the confirmation (``done=False``) or in the toast after (``done=True``); empty when the action moves no money."""
    parts: List[str] = []
    gave = sum(1 for s in students if s["delta"] > 0)
    took = sum(1 for s in students if s["delta"] < 0)
    if took:
        parts.append(f"в пакетах учеников спишется занятие: {took}" if not done else f"в пакетах учеников списано занятие: {took}")
    if gave:
        parts.append(f"в пакеты учеников вернётся занятие: {gave}" if not done else f"в пакеты учеников возвращено занятие: {gave}")
    for line in payroll:
        title = month_title(date.fromisoformat(line["month"] + "-01"))
        who = f" {line['teacherName']}," if line.get("teacherName") else ""
        change = line.get("changedBy")
        if done and change:
            parts.append(f"ведомость{who} {title}: {'+' if change > 0 else '−'}{abs(change):,}".replace(",", " ") + " сум к сохранённой — пересохраните её")
        else:
            parts.append(f"ведомость{who} {title} {'изменилась' if done else 'изменится'}" + (" (месяц закрыт: сначала переоткройте его)" if line["state"] == "closed" and not done else ""))
    text = "; ".join(parts)
    return (text[0].upper() + text[1:]) if text else ""


def build(db, org_id: Any, *, students: List[Dict[str, Any]], pairs: Iterable[Tuple[Any, Any]], owner: bool, done: bool, group_id: Any = None) -> Dict[str, Any]:
    """The block ``moneyEffects`` of an answer (``group_id``: keep the students of a package only)."""
    names = with_names(db, package_only(db, org_id, group_id, students))
    payroll = payroll_effects(db, org_id, pairs, owner=owner, amounts=done)
    return {"students": names, "payroll": payroll, "text": text_of(names, payroll, done=done), "dryRun": not done}


def _months(first: date, last: date) -> List[date]:
    out, cursor = [], first.replace(day=1)
    while cursor <= last:
        out.append(cursor)
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return out


def membership_effects(db, org_id: Any, membership_id: Any, since: date, *, owner: bool, done: bool = True) -> Dict[str, Any]:
    """A3: what a payment / a write-off / a refund (or its cancellation) of a membership does to the pay of the teachers of its group: the saved sheets of the months from the month BEFORE ``since``
    (a payment covers the adjacent periods) to now.  The owner gets only the sheets that really differ from what was saved (``changedBy`` != 0); everybody else the candidate months."""
    from src.models.auth import Group, StudentGroup

    from . import business_clock as bc
    from .closed_period import group_teacher_ids

    membership = db.query(StudentGroup).filter(StudentGroup.id == membership_id).first()
    group = db.get(Group, membership.group_id) if membership is not None else None
    if group is None:
        return {"students": [], "payroll": [], "text": "", "dryRun": not done}
    first = month_first(since)
    first = date(first.year - (first.month == 1), (first.month - 2) % 12 + 1, 1)
    months = _months(first, bc.business_today())
    teachers = group_teacher_ids(db, group, first, bc.business_today())
    payroll = payroll_effects(db, org_id, [(t, m) for t in teachers for m in months], owner=owner, amounts=done and owner)
    if owner and done:
        payroll = [p for p in payroll if p.get("changedBy")]
    return {"students": [], "payroll": payroll, "text": text_of([], payroll, done=done), "dryRun": not done}
