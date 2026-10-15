"""
ALGO - when an excused absence must be confirmed (N13 P1.5).  Pure functions.

Owner decision 15.10: a pending excuse is confirmed *before the student's next
payment*, so the administrator sees how long it can still wait.  The deadline
follows the payment that covers the lesson:

* a **calendar** payment (``valid_from <= lesson date <= valid_to``): the date
  ``valid_to - excuse_deadline_days``; a lesson on or after that cutoff day gets
  ``valid_to`` itself.  Every lesson gets at least ``MIN_GRACE_DAYS`` days
  (``lesson date + 2``, never past ``valid_to``) to be confirmed;
* a **lessons** package that consumed - or, for a lesson not yet closed, would
  consume - the lesson (the FIFO hand-out of ``payment_status``), symmetric with
  the calendar rule: with N = ``excuse_deadline_lessons`` and L lessons left in
  the package, ``L > N`` is normal, ``L == N`` is the last lesson to confirm
  (``due_now``), ``L < N`` is overdue.  N is capped at ``package size - 1`` so a
  small package never makes its first lesson overdue at once; when the effective N
  is 0, ``L == 1`` is ``due_now`` and ``L == 0`` (package used up) is overdue;
* **no covering payment** - or no package that took the lesson (all full, the
  lesson lies beyond the paid lessons): the last day of the lesson's month.

Only dates and lesson counts are produced - never an amount.  Confirming after
the deadline stays possible; ``overdue`` is a hint for the administrator.
"""
from __future__ import annotations

import calendar as _calendar
from datetime import date, timedelta
from typing import Any, Dict, Iterable, Optional, Sequence

from . import payment_status as ps


# Whatever the cutoff, a lesson's excuse can wait at least this many days (but never past valid_to).
MIN_GRACE_DAYS = 2


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


def effective_threshold(threshold: int, package_size: Optional[int]) -> int:
    """N of the rule: the setting, never above ``package size - 1`` and never below 0."""
    n = threshold
    if package_size is not None:
        n = min(n, package_size - 1)
    return max(n, 0)


def lessons_deadline(
    lessons_left: int, threshold: int, package_size: Optional[int] = None
) -> Dict[str, Any]:
    """Lessons-package state: ``left > N`` normal, ``== N`` due now, ``< N`` overdue.

    N is ``effective_threshold(threshold, package_size)``.  With N == 0 the rule would never
    fire before the package is gone, so the last lesson (``left == 1``) is the one due now and
    an exhausted package (``left == 0``) is overdue.  ``urgent`` is "amber or worse".
    """
    left = max(0, lessons_left)
    n = effective_threshold(threshold, package_size)
    if n == 0:
        due_now, overdue = left == 1, left == 0
    else:
        due_now, overdue = left == n, left < n
    return {
        "kind": "lessons",
        "lessons_left": left,
        "urgent": due_now or overdue,
        "due_now": due_now,
        "overdue": overdue,
    }


def calendar_deadline(lesson_date: date, valid_to: date, deadline_days: int) -> date:
    """Last day to confirm a lesson covered by a calendar payment ending on ``valid_to``."""
    deadline = valid_to - timedelta(days=deadline_days)
    if lesson_date >= deadline:
        deadline = valid_to  # the lesson is inside the last stretch: nothing earlier makes sense
    deadline = max(deadline, lesson_date + timedelta(days=MIN_GRACE_DAYS))
    return min(deadline, valid_to)


def excuse_deadline(
    *,
    lesson_date: date,
    today: date,
    payments: Sequence[ps.PaymentRecord],
    counted_dates: Iterable[date] = (),
    lesson_counted: bool = False,
    deadline_days: int,
    deadline_lessons: int,
) -> Dict[str, Any]:
    """Deadline of an excuse for ``lesson_date``.  ``payments`` = the live payments of the membership.

    ``counted_dates`` are the dates of the student's counted lessons; ``lesson_counted`` says
    whether THIS lesson is one of them (a completed lesson).  It cannot be read off the dates:
    two lessons on one day are two entries, and an unfinished one must be added on top of the
    other one, not mistaken for it.
    """
    calendar_payment = covering_calendar_payment(payments, lesson_date)
    if calendar_payment is not None:
        return date_deadline(
            calendar_deadline(lesson_date, calendar_payment.valid_to, deadline_days), today
        )

    if needs_lessons_pool(payments, lesson_date):
        pool = list(counted_dates)
        if not lesson_counted or lesson_date not in pool:
            pool.append(lesson_date)  # an unfinished lesson would be charged to the package too
        # Same-date entries may straddle two packages; the earliest package is the conservative pick.
        for payment, taken in ps.allocate_lessons_detail(payments, pool):
            if lesson_date in taken:
                return lessons_deadline(
                    payment.lessons_count - len(taken), deadline_lessons, payment.lessons_count
                )
        # No package took the lesson (all were already full): it is not covered, like "no payment".

    return date_deadline(month_end(lesson_date), today)
