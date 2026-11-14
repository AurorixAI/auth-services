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

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.auth import Group, GroupAttendance, StudentGroupPeriod

from . import ops_rules
from . import payment_status as ps
from . import payments_texts as tx

PAY_FROM_MAX_AHEAD_DAYS = 62          # the first lesson of a group that opens in two months is a normal thing; later than that is a slip of the finger
LOOK_AHEAD_DAYS = PAY_FROM_MAX_AHEAD_DAYS      # R3 code №8: the schedule is searched exactly as far as a chosen day is allowed - the server never suggests a day that it then refuses


def accounting_start_of(db: Session, org_id: Any, cache: Optional[Dict[Any, Any]] = None) -> date:
    """The start of the accounting of the centre (one query per request when a ``cache`` of the request is given: R3 code №13)."""
    key = ("accounting_start", str(org_id))
    if cache is not None and key in cache:
        return cache[key]
    from .payments_service import PaymentService  # lazy: pulls in the status maths

    value = PaymentService(db).get_settings(org_id).accounting_start_date
    if cache is not None:
        cache[key] = value
    return value


def first_lesson_from(db: Session, group: Group, today: date, cache: Optional[Dict[Any, Any]] = None) -> Optional[date]:
    """The day of the first lesson of the group on or after ``today``: a lesson of the weekly schedule (or the copy of a move) that nobody cancelled; ``None`` - no lesson in ``LOOK_AHEAD_DAYS``.
    ``cache``: the request's own dictionary (a bulk adding asks the schedule ONCE for the group, not once per student)."""
    key = ("first_lesson", str(group.id), today)
    if cache is not None and key in cache:
        return cache[key]
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
    found = date.fromisoformat(days[0]) if days else None
    if cache is not None:
        cache[key] = found
    return found


def default_pay_from(db: Session, group: Group, today: Optional[date] = None, accounting_start: Optional[date] = None, cache: Optional[Dict[Any, Any]] = None) -> Dict[str, Any]:
    """``{"default_pay_from", "first_lesson", "today"}``: what the dialog of adding a student shows in the field «Оплата с».  The suggested day is always one that ``check_pay_from`` accepts (R3 code №8):
    never before the start of the accounting of the centre (a centre may set it up to 31 days ahead), never later than ``PAY_FROM_MAX_AHEAD_DAYS``."""
    today = today or ps.business_today()
    first = first_lesson_from(db, group, today, cache)
    chosen = first or today
    if accounting_start is not None and chosen < accounting_start:
        chosen = accounting_start
    return {"today": today, "first_lesson": first, "default_pay_from": chosen}


def default_text(info: Dict[str, Any], accounting_start: Optional[date] = None) -> str:
    """The sentence under the field «Оплата с» of the dialog (ONE phrase, LESSONS 104): ``Оплата с 25.10.2026 — первое занятие группы с сегодня``."""
    chosen = info["default_pay_from"]
    when = chosen.strftime("%d.%m.%Y")
    if info["first_lesson"] is not None and chosen == info["first_lesson"]:
        return f"Оплата с {when} — первое занятие группы с сегодня"
    if accounting_start is not None and chosen == accounting_start and chosen > info["today"]:
        return f"Оплата с {when} — начало учёта центра"
    return f"Оплата с {when} — у группы нет занятий в ближайшие дни"


def check_pay_from(value: date, today: date, accounting_start: date) -> date:
    """The explicit day: from the start of the accounting of the centre to ``PAY_FROM_MAX_AHEAD_DAYS`` ahead (422 ``PAY_FROM_RANGE``)."""
    if value < accounting_start or value > today + timedelta(days=PAY_FROM_MAX_AHEAD_DAYS):
        raise ops_rules.unprocessable(
            "PAY_FROM_RANGE",
            f"«Оплата с» — не раньше начала учёта центра ({accounting_start.strftime('%d.%m.%Y')}) и не позже чем через {PAY_FROM_MAX_AHEAD_DAYS} дня от сегодня",
        )
    return value


def check_pay_from_after_stay(db: Session, membership_id: Any, value: date) -> date:
    """Money-1 Р3-1: a student who comes BACK to the group is charged from the day after his last stay ended at the earliest - an earlier «Оплата с» (and the individual price that goes with it) would
    re-price the closed stay behind his back (422 ``PAY_FROM_RANGE``).  One place for both: the check runs before anything is written, in the dry run and in the real adding."""
    left = db.query(func.max(StudentGroupPeriod.left_at)).filter(StudentGroupPeriod.membership_id == membership_id).scalar()
    if left is not None and value <= ps.business_date(left):
        first = ps.business_date(left) + timedelta(days=1)
        raise ops_rules.unprocessable("PAY_FROM_RANGE", f"«Оплата с» — не раньше {first.strftime('%d.%m.%Y')}: до этого дня ученик уже учился в группе")
    return value


def apply(db: Session, group: Group, period: StudentGroupPeriod, *, today: date, accounting_start: date, explicit: Optional[date] = None, cache: Optional[Dict[Any, Any]] = None) -> Dict[str, Any]:
    """Write ``billing_from`` of a NEW period of a stay and say what was decided (``billing`` of the answer of adding a student).

    No value is stored when the student is charged from the day he joins anyway (``pay_from == today``): the column stays empty and the stay is what it was before the pack."""
    info = default_pay_from(db, group, today, accounting_start, cache)
    chosen = check_pay_from(explicit, today, accounting_start) if explicit is not None else info["default_pay_from"]
    period.billing_from = None if chosen == today else chosen
    return {"pay_from": chosen, "explicit": explicit is not None, "default_pay_from": info["default_pay_from"], "first_lesson": info["first_lesson"],
            "text": text_of(chosen, today, explicit is not None, info["first_lesson"])}


def immediate_charge(db: Session, org_id: Any, membership_id: Any, today: date) -> Optional[Dict[str, Any]]:
    """Money-1 R3 (lead, 11): «сразу начислится N сум (K уроков)» - what the account of the new stay charges ON THE DAY of adding (a past «Оплата с», or a first lesson that is today): the sum of the periods that have
    begun and the lessons of the group inside their days.  ``None``: nothing is charged at once (a start in the future, a package, no price).  Read from the account the add has just made (flushed, not
    committed: a dry run rolls it back)."""
    from .payments_service import PaymentService  # lazy: pulls in the status maths

    db.flush()
    (entry,) = PaymentService(db)._loader(str(org_id), today).run([membership_id])
    return immediate_from_ledger(entry.ledger, entry.inp.lesson_days)


def immediate_from_ledger(ledger: Any, lesson_days: Any) -> Optional[Dict[str, Any]]:
    """The pure part of ``immediate_charge`` (no database): the begun periods with a charge of an account -> ``{amount, lessons, text, parts}`` or ``None``."""
    begun = [s for s in ledger.segments if s.kind == "period" and s.begun and s.amount > 0]
    amount = sum(s.amount for s in begun)
    if amount <= 0:
        return None
    parts = immediate_parts(begun, lesson_days)
    lessons = sum(p["lessons"] for p in parts)
    return {"amount": amount, "lessons": lessons, "text": tx.immediate_text(amount, lessons), "parts": parts}


def immediate_label(seg: Any) -> str:
    """The name of a period: the month («октябрь») when it is a calendar month, else its days («06.10\u201305.11»)."""
    from .closed_period import MONTH_NAMES  # lazy: the same list of month names as the payroll

    last = seg.period_start.replace(day=28) + timedelta(days=4)
    if seg.period_start.day == 1 and seg.period_end == last.replace(day=1) - timedelta(days=1):
        return MONTH_NAMES[seg.period_start.month - 1]
    return f"{tx.ddmm(seg.period_start)}\u2013{tx.ddmm(seg.period_end)}"


def immediate_parts(begun: Any, lesson_days: Any) -> list:
    """Money-1 V0: the sum of ``immediate_charge`` by the periods it is made of - one element per begun period with a charge, read from the SAME segments of the ledger (no second formula):
    ``{label, from, amount, lessons, lessons_in_period, whole}``.  ``from`` is the first charged day when the period is taken not in full (else ``None``); ``lessons_in_period`` is the lessons
    of the group in the whole period (the ledger knows it for a period charged by lessons, «2 из 9»), else ``None``.  The amounts add up to ``amount``, the lessons to ``lessons``."""
    parts = []
    for seg in begun:
        inside = set(seg.days)
        whole = seg.basis == "full" or (seg.basis == "lessons" and seg.lessons_in_part is not None and seg.lessons_in_part == seg.lessons_in_period)       # nothing of the period is lost
        parts.append({
            "label": immediate_label(seg), "from": None if whole or not seg.days else min(inside).isoformat(), "amount": int(seg.amount),
            "lessons": sum(1 for d in lesson_days(min(inside), max(inside)) if d in inside) if inside else 0,
            "lessons_in_period": seg.lessons_in_period, "whole": whole})
    return parts


def immediate_total(items: list) -> Dict[str, Any]:
    """The line under a bulk adding: what is charged at once for all the students (``items``: the ``immediate`` of the students that have one)."""
    amount = sum(i["amount"] for i in items)
    lessons = sum(i["lessons"] for i in items)
    return {"amount": amount, "lessons": lessons, "students": len(items), "text": tx.immediate_text(amount, lessons, total=True)}


def text_of(chosen: date, today: date, explicit: bool, first_lesson: Optional[date]) -> str:
    """The sentence of the answer (the dictionary of the money words): from which day the student is charged and why."""
    when = chosen.strftime("%d.%m.%Y")
    if explicit:
        return f"Оплата считается с {when}: так выбрал сотрудник"
    if first_lesson is None:
        return f"Оплата считается с {when}: у группы нет занятий в ближайшие дни"
    return f"Оплата считается с {when} — с первого занятия группы"
