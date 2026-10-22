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


def paused_student_ids(db, group_id, student_ids, day: date) -> set:
    """The students of ``student_ids`` that have a live pause in ``group_id`` covering ``day`` (one query)."""
    ids = list(student_ids)
    if not ids:
        return set()
    rows = (
        db.query(StudentGroup.student_id)
        .join(MembershipPause, MembershipPause.membership_id == StudentGroup.id)
        .filter(StudentGroup.group_id == group_id, StudentGroup.student_id.in_(ids), MembershipPause.voided_at.is_(None),
                MembershipPause.first_day <= day, MembershipPause.last_day >= day)
        .all()
    )
    return {str(r[0]) for r in rows}


def paused_until_map(db, group_id, student_ids, day: date) -> dict:
    """``{student_id (str): last day of the pause}`` for the students of ``student_ids`` that have a live pause in ``group_id`` covering ``day`` (one query);
    two overlapping pauses cannot exist (exclusion constraint), the latest ``last_day`` wins anyway."""
    ids = list(student_ids)
    if not ids:
        return {}
    rows = (
        db.query(StudentGroup.student_id, MembershipPause.last_day)
        .join(MembershipPause, MembershipPause.membership_id == StudentGroup.id)
        .filter(StudentGroup.group_id == group_id, StudentGroup.student_id.in_(ids), MembershipPause.voided_at.is_(None),
                MembershipPause.first_day <= day, MembershipPause.last_day >= day)
        .all()
    )
    out: dict = {}
    for student_id, last_day in rows:
        key = str(student_id)
        if key not in out or last_day > out[key]:
            out[key] = last_day
    return out
