"""Money-1 (A3, I-5): what an action does to money, in the answer of the server - one shape for every action that moves it.

``{"students": [...], "payroll": [...], "text": "..."}``:

* ``students`` - the lessons of the packs of the students that the action gives back (+1) or takes (-1), by the rule 4.3 (``payment_status.lesson_counts``);
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
    """``[{"studentId", "delta"}]`` of the students whose lesson is spent (-1) or given back (+1) by the change; ``before`` / ``after`` are the marks, ``status_*`` the excuse statuses by
    student, ``held_*`` whether the lesson is completed before / after (only a completed lesson spends a pack, rule 4.3)."""
    out = []
    for sid in sorted(set(before) | set(after)):
        was = held_before and counted(before.get(sid), status_before.get(sid))
        will = held_after and counted(after.get(sid), status_after.get(sid))
        if was != will:
            out.append({"studentId": sid, "delta": (1 if will else 0) - (1 if was else 0)})
    return out


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
        parts.append(f"у {took} {_pupils(took)} спишется занятие из пакета" if not done else f"у {took} {_pupils(took)} списано занятие из пакета")
    if gave:
        parts.append(f"{gave} {_pupils(gave)} вернётся занятие в пакет" if not done else f"{gave} {_pupils(gave)} возвращено занятие в пакет")
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


def _pupils(n: int) -> str:
    return "ученика" if n % 10 == 1 and n % 100 != 11 else "учеников"


def build(db, org_id: Any, *, students: List[Dict[str, Any]], pairs: Iterable[Tuple[Any, Any]], owner: bool, done: bool) -> Dict[str, Any]:
    """The block ``moneyEffects`` of an answer."""
    names = with_names(db, students)
    payroll = payroll_effects(db, org_id, pairs, owner=owner, amounts=done)
    return {"students": names, "payroll": payroll, "text": text_of(names, payroll, done=done), "dryRun": not done}
