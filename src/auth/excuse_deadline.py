"""
ALGO - when an excused absence must be confirmed (N13 P1.5).  Pure functions.

Owner decision 15.10: a pending excuse is confirmed *before the student's next
payment*, so the administrator sees how long it can still wait.  The deadline
follows the payment that covers the lesson:

* a **calendar** payment (``valid_from <= lesson date <= valid_to``): the date
  ``valid_to - excuse_deadline_days``; a lesson inside that last stretch gets
  ``valid_to`` itself (there is no earlier date it could still be confirmed by);
* a **lessons** package that consumed - or, for a lesson not yet closed, would
  consume - the lesson (the FIFO hand-out of ``payment_status``): the number of
  lessons left in that package; urgent at ``<= excuse_deadline_lessons``,
  overdue when the package is used up;
* **no covering payment**: the last day of the lesson's month.

Only dates and lesson counts are produced - never an amount.  Confirming after
the deadline stays possible; ``overdue`` is a hint for the administrator.
"""
from __future__ import annotations

import calendar as _calendar
from datetime import date, timedelta
from typing import Any, Dict, Iterable, Optional, Sequence

from . import payment_status as ps


def month_end(day: date) -> date:
    return day.replace(day=_calendar.monthrange(day.year, day.month)[1])


def date_deadline(deadline: date, today: date) -> Dict[str, Any]:
    return {
        "kind": "date",
        "date": deadline.isoformat(),
        "overdue": today > deadline,
        "days_left": (deadline - today).days,
    }


def covering_calendar_payment(
    payments: Sequence[ps.PaymentRecord], lesson_date: date
) -> Optional[ps.PaymentRecord]:
    """The calendar payment whose period contains the lesson (the longest-lasting one if several)."""
    covering = [
        p for p in payments
        if p.lessons_count is None and p.valid_to is not None and p.valid_from <= lesson_date <= p.valid_to
    ]
    return max(covering, key=lambda p: (p.valid_to, p.paid_on, p.created_at)) if covering else None


def needs_lessons_pool(payments: Sequence[ps.PaymentRecord], lesson_date: date) -> bool:
    """Whether the lessons FIFO is needed to decide this lesson's deadline."""
    return covering_calendar_payment(payments, lesson_date) is None and any(
        p.lessons_count is not None and p.valid_from <= lesson_date for p in payments
    )


def excuse_deadline(
    *,
    lesson_date: date,
    today: date,
    payments: Sequence[ps.PaymentRecord],
    counted_dates: Iterable[date] = (),
    deadline_days: int,
    deadline_lessons: int,
) -> Dict[str, Any]:
    """Deadline of an excuse for ``lesson_date``.  ``payments`` = the live payments of the membership."""
    calendar_payment = covering_calendar_payment(payments, lesson_date)
    if calendar_payment is not None:
        deadline = calendar_payment.valid_to - timedelta(days=deadline_days)
        if lesson_date > deadline:
            deadline = calendar_payment.valid_to
        return date_deadline(deadline, today)

    if needs_lessons_pool(payments, lesson_date):
        pool = list(counted_dates)
        if lesson_date not in pool:
            pool.append(lesson_date)  # an unfinished lesson would be charged to the package too
        for payment, taken in ps.allocate_lessons_detail(payments, pool):
            if lesson_date in taken:
                left = max(0, payment.lessons_count - len(taken))
                return {
                    "kind": "lessons",
                    "lessons_left": left,
                    "urgent": left <= deadline_lessons,
                    "overdue": left == 0,
                }
        # Every package that could have taken the lesson was already full.
        return {"kind": "lessons", "lessons_left": 0, "urgent": True, "overdue": True}

    return date_deadline(month_end(lesson_date), today)
