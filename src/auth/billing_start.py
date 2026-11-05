"""Money-1 B1: «Оплата с» - the day from which a student who is added to a group is charged (ДЫРА-03, the complaint of the partner).

A student of «свой месяц» was charged from the day he was ADDED to the group while the screen said «с первого занятия»: written down on 21.10 for a group that starts on 05.11, he owed 300 000
from 21.10 and had 7 lessons in the month instead of 13.  Now the day is a fact of the stay: ``student_group_periods.billing_from``.

* default - the first lesson of the group from today (the schedule, a lesson the centre cancelled is not a lesson); no lesson in sight - today;
* explicit (``pay_from``) - the money right only (LESSONS 99: a field that changes the charge is money); the day is not before the start of the accounting of the centre and not later than
  ``PAY_FROM_MAX_AHEAD_DAYS`` ahead (a backdated day charges the days of the past: the owner says so on purpose);
* a transfer has its own rule (the day after the transfer, ``first_billable_floor``): no ``pay_from`` there.

The ledger reads it as the first day of the stay (``ledger_service``): ``Stay.first = billing_from or the business day of joined_at``.  Nothing else about the membership moves (the roster, the
attendance and the teacher's lessons still follow ``joined_at``)."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from src.models.auth import Group, GroupAttendance, StudentGroupPeriod

from . import ops_rules
from . import payment_status as ps

PAY_FROM_MAX_AHEAD_DAYS = 62          # the first lesson of a group that opens in two months is a normal thing; later than that is a slip of the finger
LOOK_AHEAD_DAYS = 70                  # how far the schedule is searched for the first lesson (a bit more than the longest allowed «Оплата с»)


def first_lesson_from(db: Session, group: Group, today: date) -> Optional[date]:
    """The day of the first lesson of the group on or after ``today``: a lesson of the weekly schedule (or the copy of a move) that nobody cancelled; ``None`` - no lesson in ``LOOK_AHEAD_DAYS``."""
    from .service import AuthService  # lazy: service is heavy

    end = today + timedelta(days=LOOK_AHEAD_DAYS)
    occurrences = AuthService(db)._resolved_occurrences_for_group(group, date_from=today, date_to=end)
    cancelled = {
        (row.lesson_date, row.lesson_id or "")
        for row in db.query(GroupAttendance.lesson_date, GroupAttendance.lesson_id).filter(
            GroupAttendance.group_id == group.id, GroupAttendance.status == "cancelled",
            GroupAttendance.lesson_date >= today.isoformat(), GroupAttendance.lesson_date <= end.isoformat())
    }
    days = sorted(
        item["occurrenceDate"] for item in occurrences
        if (not item.get("isOneOff") or item.get("purpose") == "moved") and (item["occurrenceDate"], str(item["id"])) not in cancelled
    )
    return date.fromisoformat(days[0]) if days else None


def default_pay_from(db: Session, group: Group, today: Optional[date] = None) -> Dict[str, Any]:
    """``{"default_pay_from", "first_lesson", "today"}``: what the dialog of adding a student shows in the field «Оплата с»."""
    today = today or ps.business_today()
    first = first_lesson_from(db, group, today)
    return {"today": today, "first_lesson": first, "default_pay_from": first or today}


def check_pay_from(value: date, today: date, accounting_start: date) -> date:
    """The explicit day: from the start of the accounting of the centre to ``PAY_FROM_MAX_AHEAD_DAYS`` ahead (422 ``PAY_FROM_RANGE``)."""
    if value < accounting_start or value > today + timedelta(days=PAY_FROM_MAX_AHEAD_DAYS):
        raise ops_rules.unprocessable(
            "PAY_FROM_RANGE",
            f"«Оплата с» — не раньше начала учёта центра ({accounting_start.strftime('%d.%m.%Y')}) и не позже чем через {PAY_FROM_MAX_AHEAD_DAYS} дня от сегодня",
        )
    return value


def apply(db: Session, group: Group, period: StudentGroupPeriod, *, today: date, accounting_start: date, explicit: Optional[date] = None) -> Dict[str, Any]:
    """Write ``billing_from`` of a NEW period of a stay and say what was decided (``billing`` of the answer of adding a student).

    No value is stored when the student is charged from the day he joins anyway (``pay_from == today``): the column stays empty and the stay is what it was before the pack."""
    info = default_pay_from(db, group, today)
    chosen = check_pay_from(explicit, today, accounting_start) if explicit is not None else info["default_pay_from"]
    period.billing_from = None if chosen == today else chosen
    return {"pay_from": chosen, "explicit": explicit is not None, "default_pay_from": info["default_pay_from"], "first_lesson": info["first_lesson"],
            "text": text_of(chosen, today, explicit is not None, info["first_lesson"])}


def text_of(chosen: date, today: date, explicit: bool, first_lesson: Optional[date]) -> str:
    """The sentence of the answer (the dictionary of the money words): from which day the student is charged and why."""
    when = chosen.strftime("%d.%m.%Y")
    if explicit:
        return f"Оплата считается с {when}: так выбрал сотрудник"
    if first_lesson is None:
        return f"Оплата считается с {when}: у группы нет занятий в ближайшие дни"
    return f"Оплата считается с {when} — с первого занятия группы"
