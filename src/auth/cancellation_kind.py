"""PAY-F: an owner / admin changes the KIND of a lesson that is already cancelled (``centre`` <-> ``holiday``), with a reason, audited.

The recalculation for the lessons the centre cancelled is a function of the facts, so nothing else is written: the next read of the ledger shows or drops the
line.  Refused: a lesson that is not cancelled, a lesson cancelled by a day off of the centre (change the day off), and an old cancellation / a move (no kind: they
are neutral for the money and never recalculated).

LESSON-RULES С2: the same road CONFIRMS a cancellation that waits (``unconfirmed`` -> ``centre`` | ``holiday``): the reason is optional for that transition, the moment of the confirmation is the
moment the ledger learns of the cancellation (``ledger_service._cancel_moments``: the record that turned the kind INTO ``centre``), the event is written on the record of the cancellation itself (no
second record in the journal «Замены»), ``dry_run`` answers what it would do to the students.  ``expectedKind`` is what the screen saw: another kind there = 409 ``ALREADY_DECIDED`` (who, when, which kind);
the same final kind again = 200 ``changed: false``."""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.models.auth import Group, GroupAttendance, GroupAttendanceAudit

from . import billing_ledger as bl
from . import business_clock as bc
from . import ledger_service as ls
from . import payment_status as ps
from .centre_policy import CentrePolicy
from .closed_period import ClosedPeriods, lesson_teacher_ids, open_lessons_since
from .one_off_purpose import MAKEUP_EXISTS_TEXT, OneOffPurpose
from .ops_rules import OpsError, conflict, is_too_old_for_teacher, lesson_age_days, not_found, unprocessable
from .lesson_teachers import _uuid
from .schemas import CancellationKind, StoredCancellationKind

logger = logging.getLogger(__name__)

UNCONFIRMED = "unconfirmed"
ROLE_WORDS = {"owner": "владелец", "admin": "администратор", "teacher": "преподаватель"}
KIND_WORDS = {"centre": "по вине центра", "holiday": "праздник", UNCONFIRMED: "ждёт подтверждения"}
ZERO_TEXTS = {          # the words of the server for «перерасчёта нет» (the dry run of a confirmation whose total is 0)
    "packages": "В пакетах уроков отмена не требует перерасчёта: урок просто не списывается.",
    "nobody_charged": "За этот урок никто из учеников не платит: перерасчёта нет.",
    "one_off": "Разовый урок: перерасчёта нет.",
    "holiday": "Праздник — перерасчёта нет.",          # FIX4 А4: the confirmation «праздник» recalculates nothing BY ITS KIND (not «nobody pays»)
}


def withdraw_refusal(*, author: bool, role: Optional[str], in_window: bool) -> Optional[str]:
    """THE rule of «Отозвать отмену» (the road ``withdraw_cancellation`` and the flag ``canWithdraw`` of every list read it, nobody keeps a copy): the right of the AUTHOR of the cancellation, while it waits.
    A teacher also only inside the window of the centre; an administrator without the right of money - as long as it is not confirmed (the window is the rule of the teacher's own actions, not of his).
    The refusal code, ``None`` when allowed.  The month (closed) and the group (archived) are checked beside it with their own answers."""
    if not author:
        return "NOT_YOUR_CANCELLATION"
    if role == "teacher" and not in_window:
        return "LESSON_TOO_OLD"
    return None


def zero_reason(block: Dict[str, Any], lesson_id: str, day: date) -> str:
    """Why a confirmation recalculates NOTHING for anybody: ``one_off`` (a one-off lesson), ``packages`` (the regime of that day is the package of lessons: the lesson is simply not written off), else
    ``nobody_charged`` (nobody of the group pays for that lesson: a pause, the period has not begun)."""
    if str(lesson_id).startswith("one-off-"):
        return "one_off"
    if bl.regime_at(ls.parse_regimes(block), day).mode == ps.LESSONS:
        return "packages"
    return "nobody_charged"


class ChangeCancellationKind(BaseModel):
    lessonDate: date
    lessonId: str = Field(..., min_length=1, max_length=100)
    cancellationKind: CancellationKind
    reason: Optional[str] = Field(None, min_length=3, max_length=500)       # required unless the cancellation WAITS (``unconfirmed``): its confirmation needs no reason
    expectedKind: Optional[StoredCancellationKind] = None                  # the kind the screen saw (``unconfirmed`` for a confirmation): another one = 409 ALREADY_DECIDED


def _who_decided(db: Session, group: Group, row: Optional[GroupAttendance], lesson_id: str, day: date) -> Dict[str, Any]:
    """Who made the cancellation what it is now, from the journal: the last change of the kind of the row, else (the row is gone) the last return of the lesson.  ``{}`` when nobody did."""
    from src.models.auth import GroupAudit, OrganizationMember, User

    actor_id, moment, role, outcome = None, None, None, None
    if row is not None:
        last = (db.query(GroupAttendanceAudit).filter(GroupAttendanceAudit.attendance_id == row.id, GroupAttendanceAudit.action == "corrected")
                .order_by(GroupAttendanceAudit.created_at.desc(), GroupAttendanceAudit.id.desc()).first())
        if last is not None and isinstance(last.details, dict) and isinstance(last.details.get("cancellation_kind"), dict):
            actor_id, moment, outcome = last.actor_id, last.created_at, "kind_changed"
    else:
        for audit in db.query(GroupAudit).filter(GroupAudit.group_id == group.id, GroupAudit.action == "lesson_restored").order_by(GroupAudit.created_at.desc()).limit(30):
            snap = audit.snapshot or {}
            if snap.get("lessonId") == lesson_id and snap.get("lessonDate") == day.isoformat():
                actor_id, moment, role = audit.actor_id, audit.created_at, audit.actor_role
                break
    name = None
    if actor_id is not None:
        user = db.query(User).filter(User.id == actor_id).first()
        if user is not None:                                              # a user who is gone (the line outlives him): no name, the answer is the same 409
            name = " ".join(p for p in ((user.first_name or "").strip(), (user.last_name or "").strip()) if p) or None
        member = db.query(OrganizationMember.org_role).filter(OrganizationMember.org_id == group.org_id, OrganizationMember.user_id == actor_id).scalar()
        role = member or role
    return {"decided_by_name": name, "decided_by_role": role, "decided_at": bc.as_utc(moment).isoformat() if moment else None,
            "decided_at_text": bc.utc_to_business_wall(moment).strftime("%H:%M") if moment else None, "outcome": outcome}


def already_decided(db: Session, group: Group, row: Optional[GroupAttendance], lesson_id: str, day: date):
    """409 ``ALREADY_DECIDED``: the cancellation is not what the screen saw.  The row is gone = the lesson was given back (the teacher withdrew his cancellation, or an owner returned the lesson):
    ``kind`` is null and ``outcome`` says which."""
    who = _who_decided(db, group, row, lesson_id, day)
    kind = row.cancellation_kind if row is not None and row.status == "cancelled" else None
    if kind is None:
        by_teacher = who["decided_by_role"] == "teacher"
        who["outcome"] = "withdrawn_by_teacher" if by_teacher else "lesson_returned"
        head = "Преподаватель отозвал отмену" if by_teacher else "Урок уже вернули на отметку"
        text = f"{head}" + (f": {who['decided_by_name']}" if who["decided_by_name"] and not by_teacher else "") + (f" в {who['decided_at_text']}" if who["decided_at_text"] else "") + "."
    else:
        who_text = f"{who['decided_by_name']} ({ROLE_WORDS.get(who['decided_by_role'], 'сотрудник')})" if who["decided_by_name"] else "другой сотрудник"
        text = f"Отмену подтвердил {who_text}" + (f" в {who['decided_at_text']}" if who["decided_at_text"] else "") + f": {KIND_WORDS.get(kind, kind)}."
    return conflict("ALREADY_DECIDED", text, kind=kind, decided_by_name=who["decided_by_name"], decided_by_role=who["decided_by_role"], decided_at=who["decided_at"], outcome=who["outcome"])


def change_cancellation_kind(db: Session, org_id: str, group_id: str, req: ChangeCancellationKind, *, actor_id: str, dry_run: bool = False) -> Dict[str, Any]:
    group = db.query(Group).filter(Group.id == group_id, Group.org_id == org_id).with_for_update().populate_existing().first()      # the order of every writer: the GROUP, then the journal row (LESSONS 23; the creation of a make-up takes the same two)
    if group is None:
        raise not_found("GROUP_NOT_FOUND", "Группа не найдена")
    row = (db.query(GroupAttendance)
           .filter(GroupAttendance.org_id == org_id, GroupAttendance.group_id == group_id, GroupAttendance.lesson_date == req.lessonDate.isoformat(),
                   GroupAttendance.lesson_id == req.lessonId)
           .with_for_update().first())
    if row is None or row.status != "cancelled":
        if req.expectedKind is not None:                                  # the screen saw a cancellation: it is gone (given back) - say who and how
            raise already_decided(db, group, row, req.lessonId, req.lessonDate)
        raise not_found("LESSON_NOT_CANCELLED", "Урок не отменён: менять нечего")
    if req.expectedKind is not None and row.cancellation_kind != req.expectedKind:
        if row.cancellation_kind == req.cancellationKind and not dry_run:          # the same final kind (a double press, a second owner of the same mind): nothing to do - a PREVIEW of it is always the refusal below (C13.1: a window that shows a decision made by another must say so)
            return {"ok": True, "changed": False, "cancellationKind": row.cancellation_kind, "version": row.version}
        raise already_decided(db, group, row, req.lessonId, req.lessonDate)
    if row.made_up_by:                                                    # #154: a make-up is appointed - the kind decides the money of its deduction: cancel the make-up first
        raise conflict("MAKEUP_EXISTS", MAKEUP_EXISTS_TEXT)
    from .days_off import DaysOffService
    from .service import AuthService  # lazy: the service layer imports this module's neighbours

    held = DaysOffService(AuthService(db)).live_for_date(org_id, req.lessonDate)
    if held is not None:                                                  # W1 R3 (1): a date a live day off holds is a holiday whoever cancelled the lesson
        raise conflict("CANCELLED_BY_DAY_OFF", "Дата — выходной день центра: причина отмены — праздник")
    if row.day_off_id is not None:                                        # cancelled by a day off that is gone meanwhile
        raise conflict("CANCELLED_BY_DAY_OFF", "Урок отменён выходным днём центра: измените или отмените сам выходной")
    if row.cancellation_kind is None:
        raise conflict("CANCELLATION_HAS_NO_KIND", "Это отмена до обновления или перенос урока: вид отмены у неё не меняется")
    ClosedPeriods.of(db, org_id).assert_open(lesson_teacher_ids(db, group, req.lessonId, req.lessonDate.isoformat(), row), req.lessonDate)   # Money-1 A1: a closed month does not move (the one rule, ``closed_period.py``)
    before = row.cancellation_kind
    if before == req.cancellationKind:
        return {"ok": True, "changed": False, "cancellationKind": before, "version": row.version}
    waiting = before == UNCONFIRMED
    reason = (req.reason or "").strip() or None
    if reason is None and not waiting:
        raise unprocessable("REASON_REQUIRED", "Причина: от 3 до 500 символов")
    if dry_run:
        return _preview(db, org_id, group, row, req, reason)
    _apply(db, group, row, req, actor_id, before, reason)
    db.commit()
    return {"ok": True, "changed": True, "cancellationKind": req.cancellationKind, "version": row.version}


def _apply(db: Session, group: Group, row: GroupAttendance, req: ChangeCancellationKind, actor_id: Any, before: str, reason: Optional[str]) -> None:
    """Write the change (no commit): the kind, the version, the record of the journal of the lesson, the event / the record of the journal «Замены»."""
    from .lesson_cancel import lesson_outcomes
    from .lesson_substitutions import Substitutions
    from .service import AuthService

    row.cancellation_kind = req.cancellationKind
    row.version = int(row.version or 1) + 1
    db.add(GroupAttendanceAudit(
        attendance_id=row.id, org_id=group.org_id, group_id=group.id, actor_id=actor_id, action="corrected", before_status="cancelled", after_status="cancelled",
        before_records={}, after_records={}, correction_reason=reason, cancellation_reason=row.cancellation_reason, lesson_date=row.lesson_date, lesson_id=row.lesson_id,
        details={"cancellation_kind": {"from": before, "to": req.cancellationKind}}))
    subs = Substitutions(AuthService(db))
    actor = _uuid(actor_id)
    if before == UNCONFIRMED:
        OneOffPurpose(AuthService(db)).release(group, row.lesson_id, actor)           # C12.1: the confirmation of a cancelled make-up frees the lesson it made up for - what the owner's cancellation does at once
                                              # the confirmation is an EVENT of the record of the cancellation, not a second record
        record = lesson_outcomes(list(reversed(subs._lesson_requests(group, req.lessonId, req.lessonDate.isoformat()))), row.status)
        if record is not None and record.status == "cancelled":
            subs.event(record, "confirmed", actor, {"cancellationKind": {"from": before, "to": req.cancellationKind}, **({"reason": reason} if reason else {})})
            return
    subs.record_kind_change(group, req.lessonId, req.lessonDate, actor, before, req.cancellationKind, reason or "Отмена подтверждена")


def _preview(db: Session, org_id: str, group: Group, row: GroupAttendance, req: ChangeCancellationKind, reason: Optional[str]) -> Dict[str, Any]:
    """``dry_run``: what the change would do - the students' recalculation (the difference of ``money_effects.centre_deductions`` before and after the change, written and rolled back like the preview of
    the cancellation), whether the lesson can be given back, whether its teacher could still mark it."""
    from . import money_effects as me
    from .lesson_substitutions import Substitutions
    from .models import Organization
    from .service import AuthService

    day = req.lessonDate
    before = row.cancellation_kind
    today = bc.business_today()
    settings = db.query(Organization.settings).filter(Organization.id == org_id).scalar() or {}
    block = settings.get("payments") if isinstance(settings, dict) else None
    recalc_off = not bl.recalc_on(ls.parse_recalc(block if isinstance(block, dict) else {}), day)        # judged by the day of the LESSON, not by today's setting (SPEC 7.2)
    with me.writes_rolled_back(db):
        _, added = me.measure_cancellation(db, org_id, group.id, lambda: _apply(db, group, row, req, row.marked_by, before, reason))
    centre_added = added
    if req.cancellationKind != "centre":              # C13.5: the words of the outcome «по вине центра» are said whatever kind the window asked the numbers for
        db.expire(row)
        centre_req = req.model_copy(update={"cancellationKind": "centre"})
        with me.writes_rolled_back(db):
            _, centre_added = me.measure_cancellation(db, org_id, group.id, lambda: _apply(db, group, row, centre_req, row.marked_by, before, reason))
        db.expire(row)
    amounts = [int(a["amount"]) for a in added]
    why = ("holiday" if req.cancellationKind == "holiday" else zero_reason(block if isinstance(block, dict) else {}, req.lessonId, day)) if not amounts and not recalc_off else None          # recalc off: the screen says it by ``recalc_off``
    policy = CentrePolicy.of(db, org_id)
    window = policy.lesson_window_days
    restore_allowed = group.status == "active" and day >= open_lessons_since(db, org_id, today)
    if restore_allowed:
        try:
            Substitutions(AuthService(db))._lesson_ref(group, req.lessonId, day)
        except OpsError:                                                   # the schedule no longer has the lesson: it cannot be given back
            restore_allowed = False
        except Exception:                                                  # a broken database is not «нельзя вернуть»: it goes out, with its trace in the log
            logger.exception("dry run of the cancellation kind: the lesson could not be read org=%s group=%s", org_id, group.id)
            raise
    can_mark = not is_too_old_for_teacher(day, today, window)
    return {"ok": True, "dry_run": True, "changed": True, "from": before, "to": req.cancellationKind, "students": len(amounts),
            "per_student": amounts[0] if amounts and len(set(amounts)) == 1 else None, "total": sum(amounts), "deductions": added, "recalc_off": recalc_off, "zero_reason": why, "zero_text": ZERO_TEXTS.get(why),
            "restore_allowed": restore_allowed, "age_days": lesson_age_days(day, today), "window_days": window, "teacher_can_mark": can_mark,
            **outcome_texts(centre_added, recalc_off=recalc_off, zero_text=ZERO_TEXTS.get(zero_reason(block if isinstance(block, dict) else {}, req.lessonId, day)) if not centre_added and not recalc_off else None,
                            window=window, teacher_can_mark=can_mark)}


TEACHER_NOT_PAID = "Преподавателю урок не оплачивается."


def outcome_texts(added: Any, *, recalc_off: bool, zero_text: Optional[str], window: int, teacher_can_mark: bool) -> Dict[str, str]:
    """C13.5: THE words of each outcome of the confirmation (the screen keeps no copy of the rules of the money): ``centre_text`` (the recalculation as counted for «по вине центра»), ``holiday_text``, ``held_text``
    («Урок был»: the lesson returns to the mark of its teacher, or - older than the window - of the administrator / the owner)."""
    from . import money_effects as me
    from .payments_texts import plural

    amounts = [int(a["amount"]) for a in added]
    if recalc_off:
        centre = f"Перерасчёт за отмены в центре выключен — ученикам ничего не изменится. {TEACHER_NOT_PAID}"
    elif not amounts or sum(amounts) <= 0:
        centre = f"{zero_text or 'Ученикам: без перерасчёта.'} {TEACHER_NOT_PAID}"
    else:
        n = len(amounts)
        who = f"{n} {plural(n, 'ученику', 'ученикам', 'ученикам')}"
        each = f" по {me.num(amounts[0])}{me.NBSP}сум" if len(set(amounts)) == 1 else ""
        centre = f"Перерасчёт {who}{each}, всего {me.num(sum(amounts))}{me.NBSP}сум. {TEACHER_NOT_PAID}"
    held = ("Отмена снимается, преподаватель отметит посещаемость. Деньги считаются как за обычный урок." if teacher_can_mark
            else f"Отмена снимается. Урок старше {window} дн.: отметит администратор или владелец.")
    return {"centre_text": centre, "holiday_text": f"Ученикам: без перерасчёта. {TEACHER_NOT_PAID}", "held_text": held}
