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

from src.models.auth import Group, GroupAttendance, StudentGroup, StudentGroupPeriod

from . import ops_rules
from . import pay_due
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
    """The sentence under the field «Оплата с» of the dialog (ONE phrase, LESSONS 104): ``Оплата с 25.10.2026 — ближайшее занятие группы`` (today: «сегодня у группы занятие»)."""
    chosen = info["default_pay_from"]
    when = chosen.strftime("%d.%m.%Y")
    if info["first_lesson"] is not None and chosen == info["first_lesson"]:
        return f"Оплата с {when} — " + ("сегодня у группы занятие" if chosen == info["today"] else "ближайшее занятие группы")
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
    from . import ledger_service as ls  # lazy: the ledger pulls in the status maths
    from .membership_periods import periods_by_membership

    membership = db.get(StudentGroup, membership_id)
    periods = periods_by_membership(db, [membership_id]).get(membership_id, [])
    group = db.get(Group, membership.group_id) if membership is not None else None
    if group is None:
        return value
    loader = ls.LedgerLoader(db, str(group.org_id), ps.business_today())
    loader.load_marks([(membership, group)], {membership_id: periods})
    # Money-2 У5: a stay that charged NOTHING (opened and closed on one day, no mark) does not limit «Оплата с»: nothing was priced behind it (``ls.stay_of_period``)
    left = max((p.left_at for p in periods if p.left_at is not None and ls.stay_of_period(p, loader.marked_of(membership_id)) is not None), default=None)
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


def immediate_charge(db: Session, org_id: Any, membership_id: Any, today: date, before: Optional[Dict[str, Any]] = None, view: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Money-1 R3 (lead, 11) + PAY-DUE 10.4: what the account of the new stay owes ON THE DAY of adding (a past «Оплата с», or a first lesson that is today), told by the name the function of the term gives it
    (``К оплате до ДД.ММ`` / ``Долг сразу``): the unpaid sum of the periods that have begun and the lessons of the group inside their days.  ``None``: nothing is owed at once (a start in the future, a package, no price,
    the advance covers it).  Read from the account the add has just made (flushed, not committed: a dry run rolls it back).
    Stage 6 (М11Б): ``before`` = the same read taken BEFORE a returning student is written into his old membership (``immediate_charge`` of the closed stay, ``None`` = nothing yet): the answer is then what THIS
    adding adds, the account after minus the account before (``immediate_from_ledger``).  Both reads name the sum as a student IN the centre (``in_centre=True``): the term of the old part does not move because he was away."""
    from .payments_service import PaymentService  # lazy: pulls in the status maths

    db.flush()
    loader = PaymentService(db)._loader(str(org_id), today)
    (entry,) = loader.run([membership_id])
    if view is not None:      # the same account, read once: ``period_view`` for the text of a return (no second run of the ledger)
        view.update(_period_view(entry))
    due = pay_due.classify(entry.ledger, today, loader.settings.pay_due_days, loader.settings.pay_due_lessons, True)
    return immediate_from_ledger(entry.ledger, entry.inp.lesson_days, due, before)


def _period_view(entry: Any) -> Dict[str, Any]:
    """PAY-TAILS Б11.51 С2': what the text of a return needs of an account - the begun periods (start, end, the word of the window) and the first day of the newest stay; ``package``: a package is in the account (the text does not say a period for it)."""
    segments = entry.ledger.segments
    return {"package": any(s.kind == "package" for s in segments),
            "periods": [(s.period_start, s.period_end, immediate_label(s)) for s in segments if s.kind == "period" and s.begun and s.amount > 0],
            "first": max((st.first for st in entry.inp.stays), default=None)}


def period_view(db: Session, org_id: Any, membership_id: Any, today: date) -> Dict[str, Any]:
    """``_period_view`` of the account of the membership as it is now (a read of its own: for a caller that does not read the immediate charge)."""
    from .payments_service import PaymentService  # lazy: pulls in the status maths

    db.flush()
    loader = PaymentService(db)._loader(str(org_id), today)
    (entry,) = loader.run([membership_id])
    return _period_view(entry)


def returned_text(before: Dict[str, Any], after: Dict[str, Any]) -> Optional[str]:
    """PAY-TAILS Б11.51 С2': the sentence of a return whose first day lies in a period that began EARLIER and was in the account BEFORE the return (the same ``period_start`` as ``_minus_before``): the period is not counted again, it only
    grows by the days of the new stay.  ``None``: no such period (a new month, a first adding, a package) - the text stays what it was.  Only the words: the day «Оплата с» and the sums are not touched."""
    first = after.get("first")
    if not before or not after or first is None or before.get("package") or after.get("package"):
        return None
    was = {start for start, _, _ in before["periods"]}
    for start, end, label in after["periods"]:
        if start < first <= end and start in was:
            return f"Возврат в том же периоде: {label} уже в счёте и заново не считается."
    return None


def immediate_from_ledger(ledger: Any, lesson_days: Any, due: pay_due.DueResult, before: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The pure part of ``immediate_charge`` (no database): the begun periods of an account with the part of their charge that is still unpaid, named by ``due`` (the answer of ``pay_due.classify`` for this account)
    -> ``{amount, debt_amount, due_amount, due_until, term_was, lessons, text, lines, parts}`` or ``None``.  ``amount`` = ``debt_amount + due_amount``.
    ``before`` (stage 6): the same answer of the account before the return -> only the difference is said, by name; a difference of nothing or less = ``None``."""
    begun = [s for s in ledger.segments if s.kind == "period" and s.begun and s.amount > 0 and due.parts_of(s.id)]
    amount = sum(p.unpaid for s in begun for p in due.parts_of(s.id))
    if amount <= 0:
        return None
    parts = immediate_parts(begun, lesson_days, due)
    lessons = sum(p["lessons"] for p in parts)
    debt = sum(p["amount"] for p in parts if p["name"] == pay_due.DEBT)
    owed = amount - debt
    if before is not None:
        amount, lessons, parts = _minus_before(amount, lessons, parts, before)
        debt, owed = max(0, debt - before["debt_amount"]), max(0, owed - before["due_amount"])
        if amount <= 0 or debt + owed <= 0:
            return None
        if debt + owed != amount:
            amount, parts = debt + owed, []      # the names went by their own sums: no breakdown that would not add up
    # 17.10: a return into the same participation says the term of the NEW part, not of the old one: only the periods that are left in the difference give the day and the term
    kept = {p["period_start"] for p in parts} if before is not None and parts else None
    said_segments = [s for s in begun if kept is None or s.period_start.isoformat() in kept]
    until = min((p.due_until for s in said_segments for p in due.parts_of(s.id) if p.name == pay_due.DUE and p.due_until), default=None)
    terms = [p.term for s in said_segments for p in due.parts_of(s.id) if p.name == pay_due.DEBT]
    term_was = min(terms) if terms and all(terms) else None
    shown = {n: [p for p in parts if p["name"] == n] for n in (pay_due.DEBT, pay_due.DUE)}
    said = {n: sum(p["lessons"] for p in ps_) if ps_ and all(p["lessons"] > 0 for p in ps_) else 0 for n, ps_ in shown.items()}
    lines = tx.immediate_lines(debt, owed, until, debt_lessons=said[pay_due.DEBT], due_lessons=said[pay_due.DUE], term_was=term_was, today=ps.business_today())
    return {"amount": amount, "debt_amount": debt, "due_amount": owed, "due_until": until, "term_was": term_was, "lessons": lessons,
            "text": tx.immediate_text(lines), "lines": lines, "parts": parts}


def _minus_before(amount: int, lessons: int, parts: list, before: Dict[str, Any]) -> tuple:
    """The account after the return minus the account before it: the total, the lessons of the difference and the parts.  A period charged before is taken off by the START of the period (the name is not
    a key: November 2025 and November 2026 are both «ноябрь»); a period with nothing more drops out; the lessons of a part never go below zero.  Every period of the closed stay is in the account after the
    return too (a charge of a closed stay does not disappear), so the parts add up to the total and their lessons to the lessons by themselves - there is no «rest» to put anywhere."""
    old: Dict[str, Any] = {}
    for p in before["parts"]:
        old.setdefault(p["period_start"], []).append(p)       # one list per start: a period may have two parts (two versions of the rules in one month), the parts go by the first day, the closed stay first
    left = []
    for p in parts:
        was = old[p["period_start"]].pop(0) if old.get(p["period_start"]) else None      # a part before is taken off ONCE
        part = {**p, "amount": p["amount"] - (was["amount"] if was else 0), "lessons": max(0, p["lessons"] - (was["lessons"] if was else 0))}
        if was:
            part["from"], part["whole"] = None, False      # a difference is not the start of the stay: the date «from» of the whole part does not describe it, and a reduced part is not whole
        left.append(part)
    left = [p for p in left if p["amount"] != 0]
    total, total_lessons = amount - int(before["amount"]), max(0, lessons - int(before.get("lessons") or 0))
    if sum(p["amount"] for p in left) != total or sum(p["lessons"] for p in left) != total_lessons or any(p["amount"] <= 0 for p in left):
        return total, total_lessons, []      # the lock: the parts do not add up to the sum - no breakdown (the sum and the text stay)
    return total, total_lessons, left


def immediate_label(seg: Any) -> str:
    """The name of a period: the month («октябрь») when it is a calendar month, else its days («06.10\u201305.11»)."""
    from .closed_period import MONTH_NAMES  # lazy: the same list of month names as the payroll

    last = seg.period_start.replace(day=28) + timedelta(days=4)
    if seg.period_start.day == 1 and seg.period_end == last.replace(day=1) - timedelta(days=1):
        return MONTH_NAMES[seg.period_start.month - 1]
    return f"{tx.ddmm(seg.period_start)}\u2013{tx.ddmm(seg.period_end)}"


def immediate_parts(begun: Any, lesson_days: Any, due: pay_due.DueResult) -> list:
    """Money-1 V0 + PAY-DUE 10.4: the sum of ``immediate_charge`` by the periods it is made of - one element per begun period with an unpaid sum, read from the SAME segments of the same ledger and named by ``due`` (no second formula):
    ``{label, period_start, from, amount, lessons, lessons_in_period, whole, name, due_until}`` (``period_start``: ISO first day of the period, the key that matches a part before and after a return).  ``amount`` is the UNPAID
    part of the period's charge, ``name`` is ``due`` / ``debt``, ``due_until`` the last day of «к оплате» (``None`` for a debt).  ``from`` is the first charged day when the period is taken not in full (else ``None``);
    ``lessons_in_period`` is the lessons of the group in the whole period (the ledger knows it for a period charged by lessons, «2 из 9»), else ``None``.  A period paid in part says no lessons (0): the sum is no longer
    the price of those lessons.  The amounts add up to ``amount``, the lessons to ``lessons``."""
    parts = []
    for seg in begun:
        (named,) = due.parts_of(seg.id)
        inside = set(seg.days)
        whole = seg.basis == "full" or (seg.basis == "lessons" and seg.lessons_in_part is not None and seg.lessons_in_part == seg.lessons_in_period)       # nothing of the period is lost
        parts.append({
            "label": immediate_label(seg), "period_start": seg.period_start.isoformat(), "from": None if whole or not seg.days else min(inside).isoformat(), "amount": int(named.unpaid),
            "lessons": sum(1 for d in lesson_days(min(inside), max(inside)) if d in inside) if inside and named.unpaid >= seg.amount else 0,
            "lessons_in_period": seg.lessons_in_period, "whole": whole, "name": named.name, "due_until": named.due_until.isoformat() if named.due_until else None})
    return parts


def immediate_total(items: list) -> Dict[str, Any]:
    """The lines under a bulk adding: what is owed at once for all the students (``items``: the ``immediate`` of the students that have one): the sums by name and the NEAREST last day of «к оплате» (each student has his own in his line)."""
    amount = sum(i["amount"] for i in items)
    lessons = sum(i["lessons"] for i in items)
    debt, owed = sum(i["debt_amount"] for i in items), sum(i["due_amount"] for i in items)
    until = min((i["due_until"] for i in items if i["due_until"]), default=None)
    day = date.fromisoformat(until) if isinstance(until, str) else until
    lines = tx.immediate_lines(debt, owed, day, debt_lessons=lessons if owed == 0 else 0, due_lessons=lessons if debt == 0 else 0, total=True, today=ps.business_today())
    return {"amount": amount, "debt_amount": debt, "due_amount": owed, "due_until": until, "lessons": lessons, "students": len(items), "text": tx.immediate_text(lines), "lines": lines}


def text_of(chosen: date, today: date, explicit: bool, first_lesson: Optional[date]) -> str:
    """The sentence of the answer (the dictionary of the money words): from which day the student is charged and why."""
    when = chosen.strftime("%d.%m.%Y")
    if explicit:
        return f"Оплата считается с {when}: так выбрал сотрудник"
    if first_lesson is None:
        return f"Оплата считается с {when}: у группы нет занятий в ближайшие дни"
    return f"Оплата считается с {when} — с первого занятия группы"
