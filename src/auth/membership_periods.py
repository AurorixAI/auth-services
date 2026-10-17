"""Membership periods: every stay of a student in a group (N13, ROADMAP #59).

A membership (``student_groups``) is ONE row per student and group - the payments hang on it.  A student can
leave a group and return to it, so the row alone knows one stay only: ``joined_at`` / ``left_at`` describe the
CURRENT (or last) one, and the earlier ones are kept in ``student_group_periods``.

Invariants - they hold after every operation that goes through ``open_period`` / ``close_period``:

  * a membership has at least one period;
  * at most one period is open (``left_at IS NULL``) - the database enforces it with a partial unique index;
  * an open period exists if and only if the membership is active;
  * the periods of a membership do not overlap, and a period never ends before it starts;
  * the current / last period has the ``joined_at`` / ``left_at`` of the membership row itself, so the code that
    only knows ``student_groups.joined_at`` / ``left_at`` (payments, transfer labels, the card) keeps working.

Every moment is NAIVE UTC, like ``student_groups.joined_at`` / ``left_at``.  A lesson's time is Tashkent wall
time: convert it with ``lesson_start_as_naive_utc`` before asking "was he a member at that instant".

Write points (one helper each, never a bare ``is_active = ...``):
  * ``AuthService.add_student_to_group``  - a new membership, a return (reactivation), a transfer (closes the sources);
  * ``AuthService.remove_student_from_group``;
  * ``StudentArchiveService.archive``  (``restore`` goes through ``add_student_to_group``);
  * ``StudentGroupRepo.remove_student``, the bulk import (a new membership).
A membership added to the session without a period (a fixture, a script) gets one before the flush - see
``give_new_memberships_a_period`` in ``src/models/auth.py``.  A bulk SQL ``UPDATE student_groups`` bypasses all of
this: whoever writes one must write the period too.

Readers: "was he a member at instant X" asks the PERIODS (``member_at`` / ``group_period_spans``); "is he a
member now" keeps asking ``is_active``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import UUID

from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import Session

from src.models.auth import StudentGroup, StudentGroupPeriod


# ---------------------------------------------------------------------------------------------- moments


def naive_utc(moment: Optional[datetime]) -> Optional[datetime]:
    """A timezone-aware moment becomes naive UTC; a naive one is taken as UTC already."""
    if moment is None:
        return None
    if moment.tzinfo is not None:
        return moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ------------------------------------------------------------------------------------------ write points


def open_period(membership: StudentGroup, at: Optional[datetime] = None) -> StudentGroupPeriod:
    """The student joins the group (a new membership) or returns to it (a closed one is reactivated).

    Sets ``is_active`` / ``joined_at`` / ``left_at`` of the membership AND opens the period, so the two cannot
    drift.  A period that is already open is returned as it is (a no-op).  A new period never starts before the
    previous one ended: a clock that stepped back is clamped to the previous ``left_at``.
    """
    moment = naive_utc(at) or utcnow_naive()
    periods = list(membership.periods)
    for period in periods:
        if period.left_at is None:
            membership.is_active = True
            membership.left_at = None
            membership.joined_at = period.joined_at
            return period
    previous_end = max((p.left_at for p in periods if p.left_at is not None), default=None)
    if previous_end is not None and moment < previous_end:
        moment = previous_end
    period = StudentGroupPeriod(joined_at=moment)
    membership.periods.append(period)
    membership.is_active = True
    membership.joined_at = moment
    membership.left_at = None
    return period


def close_period(membership: StudentGroup, at: Optional[datetime] = None) -> Optional[StudentGroupPeriod]:
    """The student leaves the group (removed, transferred out, archived): the open period ends at ``at``.

    A membership that is not active and has no open period is left as it is (returns ``None``).  A membership
    that has no period at all (a row inserted outside the application) gets the closed one it should have had.
    """
    moment = naive_utc(at) or utcnow_naive()
    periods = list(membership.periods)
    open_periods = [p for p in periods if p.left_at is None]
    if not open_periods:
        if periods or not membership.is_active:
            return None
        started = naive_utc(membership.joined_at) or moment
        period = StudentGroupPeriod(joined_at=started, left_at=max(moment, started))
        membership.periods.append(period)
        membership.is_active = False
        membership.left_at = period.left_at
        return period
    closed = None
    for period in open_periods:
        period.left_at = max(moment, period.joined_at)  # never before it started
        closed = period
    membership.is_active = False
    membership.left_at = closed.left_at
    return closed


# ----------------------------------------------------------------------------------------------- readers


def member_at(instant: datetime):
    """SQL condition for a query on ``StudentGroup``: ANY period of the membership covers ``instant``.

    ``joined_at <= instant`` and (``left_at`` is open or ``left_at > instant``) - the rule of
    ``membership_covers_instant``, asked of every stay and not of the last one alone.
    """
    return exists().where(
        and_(
            StudentGroupPeriod.membership_id == StudentGroup.id,
            StudentGroupPeriod.joined_at <= instant,
            or_(StudentGroupPeriod.left_at.is_(None), StudentGroupPeriod.left_at > instant),
        )
    )


def group_period_spans(
    db: Session, group_ids: Sequence[UUID], *, not_before: datetime
) -> Dict[UUID, List[Tuple[datetime, Optional[datetime]]]]:
    """``{group_id: [(joined_at, left_at), ...]}`` of every period of the groups that is open or ended after
    ``not_before`` - ONE query for all the groups (the missed-lesson discovery pass)."""
    spans: Dict[UUID, List[Tuple[datetime, Optional[datetime]]]] = {}
    if not group_ids:
        return spans
    rows = (
        db.query(StudentGroup.group_id, StudentGroupPeriod.joined_at, StudentGroupPeriod.left_at)
        .join(StudentGroupPeriod, StudentGroupPeriod.membership_id == StudentGroup.id)
        .filter(
            StudentGroup.group_id.in_(list(group_ids)),
            or_(StudentGroupPeriod.left_at.is_(None), StudentGroupPeriod.left_at > not_before),
        )
        .all()
    )
    for group_id, joined_at, left_at in rows:
        spans.setdefault(group_id, []).append((joined_at, left_at))
    return spans


def periods_by_membership(
    db: Session, membership_ids: Iterable[UUID]
) -> Dict[UUID, List[StudentGroupPeriod]]:
    """The periods of the memberships, oldest first - ONE query for all of them."""
    ids = list(dict.fromkeys(membership_ids))
    result: Dict[UUID, List[StudentGroupPeriod]] = {membership_id: [] for membership_id in ids}
    if not ids:
        return result
    rows = (
        db.query(StudentGroupPeriod)
        .filter(StudentGroupPeriod.membership_id.in_(ids))
        .order_by(StudentGroupPeriod.membership_id, StudentGroupPeriod.joined_at, StudentGroupPeriod.id)
        .all()
    )
    for period in rows:
        result[period.membership_id].append(period)
    return result


def periods_payload(periods: Iterable[StudentGroupPeriod]) -> List[Dict[str, Optional[str]]]:
    """The ``periods`` field of an API entry: oldest first, naive-UTC ISO strings, ``left_at`` null while open."""
    return [
        {
            "joined_at": period.joined_at.isoformat() if period.joined_at else None,
            "left_at": period.left_at.isoformat() if period.left_at else None,
        }
        for period in periods
    ]


# ------------------------------------------------------------------------------------------- invariants


def membership_period_violations(
    db: Session, membership_ids: Optional[Iterable[UUID]] = None
) -> List[str]:
    """Every broken invariant, one readable line each (empty = all good).  Meant for tests and for an operator
    who edited ``student_groups`` by hand."""
    query = db.query(StudentGroup)
    if membership_ids is not None:
        ids = list(membership_ids)
        if not ids:
            return []
        query = query.filter(StudentGroup.id.in_(ids))
    memberships = query.all()
    periods = periods_by_membership(db, [m.id for m in memberships])
    problems: List[str] = []
    for membership in memberships:
        label = f"membership {membership.id}"
        stays = periods.get(membership.id, [])
        if not stays:
            problems.append(f"{label}: no period")
            continue
        opened = [p for p in stays if p.left_at is None]
        if len(opened) > 1:
            problems.append(f"{label}: {len(opened)} open periods")
        if bool(opened) != (membership.is_active is True):
            problems.append(f"{label}: is_active={membership.is_active} but {len(opened)} open period(s)")
        previous: Optional[StudentGroupPeriod] = None
        for stay in stays:
            if stay.left_at is not None and stay.left_at < stay.joined_at:
                problems.append(f"{label}: a period ends before it starts ({stay.joined_at} > {stay.left_at})")
            if previous is not None and (previous.left_at is None or previous.left_at > stay.joined_at):
                problems.append(f"{label}: periods overlap ({previous.joined_at}..{previous.left_at} / {stay.joined_at})")
            previous = stay
        last = stays[-1]
        if membership.joined_at is not None and last.joined_at != membership.joined_at:
            problems.append(f"{label}: last period starts {last.joined_at}, the membership says {membership.joined_at}")
        if not membership.is_active and last.left_at != membership.left_at:
            problems.append(f"{label}: last period ends {last.left_at}, the membership says {membership.left_at}")
    return problems


def check_periods(db: Session, membership_ids: Optional[Iterable[UUID]] = None) -> None:
    """Raise ``AssertionError`` listing the broken invariants (for tests)."""
    problems = membership_period_violations(db, membership_ids)
    if problems:
        raise AssertionError("membership periods are inconsistent:\n  " + "\n  ".join(problems))
