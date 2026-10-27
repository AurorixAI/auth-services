"""W1 (owner's decision 2): what a lesson would pay the teacher who takes it over - an ESTIMATE shown in the window where a substitute is chosen.

No second formula (LESSONS 38): the sum is ``payroll_engine.lessons_amount`` - for every student of the roster ``price of his group / his own price on THAT day x percent of the
candidate for the type of the group (``payroll_rates.RateBook``, ``lesson_types``) / D_lesson`` summed, rounded ONCE.  What a payroll sheet knows after the lesson is held (the covering
payment, a package that really covered it) is only guessed here: ``D_lesson`` is the size of the package of a student whose latest charge is a package, else the divisor of the month
(``PayrollService.divisor_at``); the roster is the one of the lesson time (the same ``_attendance_roster_at`` a started lesson freezes).  A lesson without students, or a candidate without a
rate for the type of the group, has no estimate: ``None`` with the reason ``NO_STUDENTS`` / ``RATE_MISSING``."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, Optional
from uuid import UUID

from src.models.auth import PayrollRate, StudentGroup

from . import business_clock as bc
from . import billing_ledger as bl
from . import lesson_types, payment_status as ps, payroll_engine as engine, pricing
from .payments_service import PaymentService
from .payroll_rates import RateBook
from .payroll_service import PayrollService

NO_STUDENTS = "NO_STUDENTS"
RATE_MISSING = "RATE_MISSING"


def substitute_pay_estimates(svc, org_id: Any, group, lesson_date: str, start_minutes: int, teacher_ids: Iterable[UUID]) -> Dict[UUID, Dict[str, Any]]:
    """``{teacher_id: {"amount": int | None, "reason": None | "RATE_MISSING" | "NO_STUDENTS"}}`` for the lesson of ``group`` on ``lesson_date``."""
    db = svc.db
    ids = list(teacher_ids)
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
        price = books.price(m.id, group.id, day).amount
        entry = entries.get(m.id)
        charges = [sg for sg in (entry.ledger.segments if entry else []) if sg.kind in ("period", "package")]
        if charges:
            size = charges[-1].nominal_days if charges[-1].kind == "package" else None
        else:
            regime = bl.regime_at(loader.settings.regimes, day)
            size = regime.package_size if regime.mode == ps.LESSONS else None
        lessons.append((price, size if size else divisor))
    out: Dict[UUID, Dict[str, Any]] = {}
    for teacher in ids:
        row = book.at(teacher, lesson_type, month)
        out[teacher] = ({"amount": None, "reason": RATE_MISSING} if row is None
                        else {"amount": engine.lessons_amount(engine.parse_percent(row.percent), lessons), "reason": None})
    return out
