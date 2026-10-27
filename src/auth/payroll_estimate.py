"""W1 (owner's decision 2): what a lesson would pay the teacher who takes it over - an ESTIMATE («≈») shown to the OWNER in the window where a substitute is chosen.

Pay data is the owner's like the payroll sheet: an administrator gets ``None`` with the reason ``HIDDEN`` (``rateMissing`` - a yes / no - stays for both).

No second formula (LESSONS 38): the sum is ``payroll_engine.lessons_amount`` over the students of the roster whose lesson is COVERED (the payroll counts only what a payment covers; an unpaid
lesson is "не оплачено" and out of the sum): per student ``the price that covers his lesson`` (the price of the period / package that carries it, ``Ledger.lesson_cover``, as
``payroll_service`` takes it; a day before his first charged day: the price of the day) x percent of the candidate for the type of the group ON THE DAY (``payroll_rates.RateBook``,
``lesson_types``) / ``D_lesson`` (the size of the package that covers him, else the divisor of the month, ``PayrollService.divisor_at``).  What is only guessed: a future lesson of a student
in the lessons mode counts as covered when his package still has lessons left.  The roster is the one of the lesson time (``_attendance_roster_at``).
A lesson without students has no estimate: ``None`` with ``NO_STUDENTS``; a candidate without a rate for the type: ``None`` with ``RATE_MISSING``; students all unpaid: 0."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, Optional
from uuid import UUID

from src.models.auth import PayrollRate, StudentGroup

from . import business_clock as bc
from . import lesson_types, payroll_engine as engine, pricing
from .payments_service import PaymentService
from .payroll_rates import RateBook
from .payroll_service import PayrollService

NO_STUDENTS = "NO_STUDENTS"
RATE_MISSING = "RATE_MISSING"
HIDDEN = "HIDDEN"


def substitute_pay_estimates(svc, org_id: Any, group, lesson_id: str, lesson_date: str, start_minutes: int, teacher_ids: Iterable[UUID],
                             *, is_owner: bool) -> Dict[UUID, Dict[str, Any]]:
    """``{teacher_id: {"amount": int | None, "reason": None | "RATE_MISSING" | "NO_STUDENTS" | "HIDDEN"}}`` for the lesson of ``group`` on ``lesson_date``."""
    db = svc.db
    ids = list(teacher_ids)
    if not is_owner:
        return {t: {"amount": None, "reason": HIDDEN} for t in ids}
    day = date.fromisoformat(lesson_date)
    month = day.replace(day=1)
    roster = {r["student_id"] for r in svc._attendance_roster_at(group.id, bc.lesson_instant_utc(day, start_minutes))}
    memberships = [m for m in db.query(StudentGroup).filter(StudentGroup.group_id == group.id, StudentGroup.is_active.is_(True)).all() if str(m.student_id) in roster]
    if not memberships:
        return {t: {"amount": None, "reason": NO_STUDENTS} for t in ids}
    mids = [m.id for m in memberships]
    books = pricing.load_books(db, [group.id], mids)
    divisor = PayrollService(db).divisor_at(str(org_id), month)
    loader = PaymentService(db)._loader(str(org_id), recalc=False)
    entries = {e.membership.id: e for e in loader.run(mids)}
    lesson_type = lesson_types.load_book(db, [group]).type_at(group.id, day)
    book = RateBook(db.query(PayrollRate).filter(PayrollRate.org_id == org_id, PayrollRate.teacher_id.in_(ids)).all()) if ids else RateBook([])
    lessons = []
    for m in memberships:
        entry = entries.get(m.id)
        if entry is None:
            continue
        ledger = entry.ledger
        if any(lo <= day and (hi is None or day <= hi) for lo, hi in ledger.lessons_windows) and ledger.lessons is not None:     # lessons mode: a package carries it
            packages = [sg for sg in ledger.segments if sg.kind == "package"]
            if ledger.lessons.left <= 0 or not packages:
                continue                                                          # no package left: the lesson would be unpaid
            price, size = packages[-1].price.amount, packages[-1].nominal_days
        else:
            info = ledger.lesson_cover(day, lesson_id)
            if not info.paid:
                continue                                                          # UNPAID: out of the sum, as in the payroll sheet
            price = info.price.amount if info.price is not None else books.price(m.id, group.id, day).amount      # a day before the first charged day: the price of the day
            size = None
        lessons.append((price, size if size else divisor))
    out: Dict[UUID, Dict[str, Any]] = {}
    for teacher in ids:
        row = book.at(teacher, lesson_type, month)
        out[teacher] = ({"amount": None, "reason": RATE_MISSING} if row is None
                        else {"amount": engine.lessons_amount(engine.parse_percent(row.percent), lessons), "reason": None})
    return out
