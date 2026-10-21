"""LOGIC-A (decision 2, S-3): SQL conditions on ``StudentGroup`` for «the student is on a pause that day».

A student whose live pause covers the business day of a lesson is NOT in the composition of that lesson (the snapshot excludes him), so he
is counted neither in a lessons package nor in payroll.  Used by the two roster builders of ``AuthService``."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import and_, exists

from src.models.auth import MembershipPause, StudentGroup

from . import payment_status as ps


def not_paused_on(day: date):
    """SQL condition: NO live pause of the membership covers ``day`` (a business day)."""
    return ~exists().where(
        and_(
            MembershipPause.membership_id == StudentGroup.id,
            MembershipPause.voided_at.is_(None),
            MembershipPause.first_day <= day,
            MembershipPause.last_day >= day,
        )
    )


def not_paused_now():
    return not_paused_on(ps.business_today())


def not_paused_at(instant: datetime):
    """The same for the business day of an instant (naive UTC / aware, as ``ps.business_date`` reads it)."""
    return not_paused_on(ps.business_date(instant))
