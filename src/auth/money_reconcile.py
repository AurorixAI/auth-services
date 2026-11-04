"""Money-1 (A7): the nightly reconciliation of the invariants I-1 .. I-4 over all centres.

Pure functions (``check_*``) say whether ONE thing diverges; the loader (``reconcile_org``) reads a centre and calls them; ``run`` writes the result (``money_reconcile_runs`` /
``_findings`` / ``_snapshots`` / ``_centres``) and tells the team; ``scripts/money_reconcile.py`` is the command.  A centre never sees any of it (the platform console reads the tables).

R3 round: the checks verify the CALCULATION, not only its shape, and a check that cries wolf is worse than none (the owner's rule: 0 false findings on honest chains).

* I-1 the account of a membership, with the deduction for the lessons the centre cancelled (``recalc=True`` - the way every screen, the Excel and the receipt show it), by independent paths:
  (a) the identity ``charged - paid - transferred_in - written_off - recalculated + refunded + transferred_out == debt - advance``; (b) the running balance of the statement (its events one by
  one) equals ``advance - debt``; (c) the gross payments and the refunds of the ledger equal the sums of the rows of the database; (d) money does not leave more than came
  (``refunded + transferred_out <= paid + transferred_in``); (e) the rows of write-offs do not exceed everything ever charged;
* I-2 what a membership has paid for did not move since the last night WITHOUT a record that explains it.  The gross sum of the payments moves only with a money record (a payment made /
  cancelled / replaced, a transfer, an opening balance, a stay); the COVERAGE - "paid until" of a calendar account, the number of lessons covered of a package account (a package account is not
  compared by date: that date moves with every held lesson) - also with the price of the group, its schedule, a day off, an excuse, a lesson cancelled / returned / held, a pause, a change of
  the settings of the centre.  The first night only takes the snapshot;
* I-3 a SAVED payroll sheet: its total is the sum of its lines (every saved sheet); and for a CLOSED month (the sheet saved after the month ended) - no line pays without a counted mark, no mark
  pays a lesson that was not held, and the fresh total of the month, recalculated by the payroll itself, equals the saved one (a drift is a teacher who is paid less or more than the data says);
* I-4 a CLOSED month of a teacher has no unresolved lesson (going on or never started, from the accounting start of the centre): nobody can resolve it without reopening the month.

Operation: one run at a time (an advisory lock: the second one answers ``busy``); a centre is read in a read-only transaction with a time limit and the run has a budget - a centre it could not
check is named (the letter says so) and its findings of the next night are compared with its LAST SUCCESSFUL check, not with the failed night.

What is printed / stored / mailed: counts, ids, months, sums - never a name of a person.  Nothing here writes to a centre's data; ``run`` writes only its own tables.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import UUID

from sqlalchemy import func, or_, text, tuple_
from sqlalchemy.dialects.postgresql import distinct_on, insert as pg_insert

from src.models.auth import (
    AttendanceExcuse, Group, GroupAttendance, GroupPrice, GroupScheduleOneOffLesson, GroupScheduleOverride, MembershipOpeningBalance, MembershipPause, MembershipPriceOverride,
    MembershipTransfer, MoneyReconcileCentre, MoneyReconcileFinding, MoneyReconcileRun, MoneyReconcileSnapshot, Organization, OrgDayOff, PayrollSheet, StudentGroup, StudentPayment,
    StudentRefund, StudentWriteoff,
)

from . import business_clock as bc
from . import payroll_engine as engine
from .closed_period import ClosedPeriods, month_first

logger = logging.getLogger(__name__)

INVARIANT_TEXT = {
    "I-1": "долг, начислено и оплачено не сходятся",
    "I-2": "«оплачено до» изменилось без записи в журнале",
    "I-3": "в сохранённой ведомости зарплата не сходится с уроками",
    "I-4": "в закрытом месяце остался неразобранный урок",
}
LOADER_CHUNK = 150
LOCK_KEY = 7_204_001                       # the advisory lock of the nightly run
DRIFT_MONTHS = 3                           # I-3: the closed months looked at for a drift (the last ones; an old month changes for lawful reasons nobody can name any more)
STATEMENT_TIMEOUT_MS = int(os.getenv("MONEY_RECONCILE_STATEMENT_TIMEOUT_MS", "120000"))
BUDGET_SECONDS = int(os.getenv("MONEY_RECONCILE_BUDGET_SECONDS", "3300"))


@dataclass
class Found:
    """One divergence.  ``subject_key`` is stable between nights (the next run says «новое» / «прежнее» by it)."""

    org_id: UUID
    invariant: str
    subject_key: str
    subject_id: Optional[str] = None
    month: Optional[str] = None
    expected: Optional[int] = None
    actual: Optional[int] = None
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RowSums:
    """The sums of the rows of the database of ONE membership (the second path of I-1: no ledger in it)."""

    paid: int = 0
    refunded: int = 0
    written_off: int = 0


# ====================================================================================================== pure checks: I-1
def check_identity(org_id: UUID, membership_id: Any, ledger) -> Optional[Found]:
    """I-1 (a): the identity of ``Ledger.reconciles``; ``expected`` = what the left side says, ``actual`` = debt - advance."""
    if ledger.reconciles:
        return None
    left = ledger.charged - ledger.paid - ledger.transferred_in - ledger.written_off - ledger.recalculated + ledger.refunded + ledger.transferred_out
    return Found(org_id, "I-1", f"membership:{membership_id}", str(membership_id), None, left, ledger.debt - ledger.advance, {"what": "identity", "charged": ledger.charged, "paid": ledger.paid})


def check_statement(org_id: UUID, membership_id: Any, ledger) -> Optional[Found]:
    """I-1 (b): the running balance of the statement (its events added one by one) is ``advance - debt`` - the second way to the same number."""
    last = ledger.statement[-1].balance_after if ledger.statement else 0
    if last == ledger.advance - ledger.debt:
        return None
    return Found(org_id, "I-1", f"membership:{membership_id}:statement", str(membership_id), None, ledger.advance - ledger.debt, last, {"what": "statement"})


def check_rows(org_id: UUID, membership_id: Any, ledger, rows: RowSums) -> List[Found]:
    """I-1 (c)(d)(e): the ledger against the rows of the database and against the common sense of money."""
    out: List[Found] = []
    sid = str(membership_id)
    if rows.paid != ledger.paid:
        out.append(Found(org_id, "I-1", f"membership:{sid}:paid", sid, None, rows.paid, ledger.paid, {"what": "paid_rows"}))
    if rows.refunded != ledger.refunded:
        out.append(Found(org_id, "I-1", f"membership:{sid}:refunded", sid, None, rows.refunded, ledger.refunded, {"what": "refund_rows"}))
    if ledger.refunded + ledger.transferred_out > ledger.paid + ledger.transferred_in:
        out.append(Found(org_id, "I-1", f"membership:{sid}:outflow", sid, None, ledger.paid + ledger.transferred_in, ledger.refunded + ledger.transferred_out, {"what": "outflow_exceeds_inflow"}))
    if max(rows.written_off, ledger.written_off) > max(ledger.charged, 0):
        out.append(Found(org_id, "I-1", f"membership:{sid}:writeoff", sid, None, ledger.charged, rows.written_off, {"what": "writeoff_exceeds_charged"}))
    return out


# ====================================================================================================== pure checks: I-2
Coverage = Tuple[Optional[date], Optional[int], Optional[int]]            # (covered_through of a calendar account, lessons covered of a package account, gross paid)


def coverage_of(ledger) -> Coverage:
    """What the snapshot keeps: the date only for a calendar account, the number of lessons only for a package account (the date of a package account moves with every held lesson)."""
    if ledger.mode == "lessons":
        return (None, ledger.lessons.covered if ledger.lessons is not None else None, int(ledger.paid))
    return (ledger.covered_through, None, int(ledger.paid))


def check_paid_until(org_id: UUID, membership_id: Any, before: Optional[Coverage], now: Coverage, explained: bool, paid_explained: Optional[bool] = None) -> Optional[Found]:
    """I-2: ``before`` / ``now`` = ``(covered_through, lessons_covered, paid)``.  ``explained`` - a record explains a move of the coverage; ``paid_explained`` (default: the same) - a MONEY record
    explains a move of the gross payments.  No snapshot yet (first night) -> nothing to compare."""
    if before is None or before == now:
        return None
    paid_explained = explained if paid_explained is None else paid_explained
    coverage_moved = before[:2] != now[:2]
    paid_moved = before[2] != now[2]
    if not ((coverage_moved and not explained) or (paid_moved and not paid_explained)):
        return None
    return Found(org_id, "I-2", f"membership:{membership_id}", str(membership_id), None, None, None,
                 {"covered_through": [before[0].isoformat() if before[0] else None, now[0].isoformat() if now[0] else None], "lessons_covered": [before[1], now[1]], "paid": [before[2], now[2]]})


# ====================================================================================================== pure checks: I-3
def check_sheet_total(org_id: UUID, sheet_id: Any, month: str, lines: Sequence[Dict[str, Any]], total: int) -> Optional[Found]:
    """I-3 (a): the total of a saved sheet is the sum of its lines."""
    summed = engine.sheet_total(lines)
    if summed == int(total):
        return None
    return Found(org_id, "I-3", f"sheet-total:{sheet_id}", str(sheet_id), month, summed, int(total), {"what": "total"})


PAID_STATES = (engine.COUNTED, engine.EXCUSED_PENDING)


def paid_marks(lines: Sequence[Dict[str, Any]]) -> List[Tuple[str, str, str]]:
    """``[(group_id, date, lesson_id)]`` of the marks a saved sheet PAYS for (a counted mark / an excuse waiting for a decision)."""
    out = []
    for line in lines:
        for mark in line.get("marks", ()):
            if mark.get("state") in PAID_STATES:
                out.append((str(line.get("group_id")), str(mark.get("date")), str(mark.get("lesson_id"))))
    return out


def check_paid_lessons(org_id: UUID, sheet_id: Any, month: str, marks: Iterable[Tuple[str, str, str]], status_of: Dict[Tuple[str, str, str], str]) -> Optional[Found]:
    """I-3 (b): a sheet pays only lessons that were HELD (a row ``completed``).  ``actual`` = how many paid marks stand on a lesson that is not held (cancelled, going on, missing)."""
    bad = sorted({m for m in marks if status_of.get(m) != "completed"})
    if not bad:
        return None
    return Found(org_id, "I-3", f"sheet-lessons:{sheet_id}", str(sheet_id), month, 0, len(bad), {"what": "not_held", "lessons": [f"{d}|{g}" for g, d, _ in bad[:10]]})


def check_line_marks(org_id: UUID, sheet_id: Any, month: str, lines: Sequence[Dict[str, Any]]) -> Optional[Found]:
    """I-3 (c): every line that pays something stands on at least one paid mark - a self-consistent sheet with a line of 100 000 and no lesson behind it is a finding."""
    bare = [str(line.get("line_key") or line.get("membership_id")) for line in lines if int(line.get("amount") or 0) != 0 and not any(m.get("state") in PAID_STATES for m in line.get("marks", ()))]
    if not bare:
        return None
    return Found(org_id, "I-3", f"sheet-marks:{sheet_id}", str(sheet_id), month, 0, len(bare), {"what": "line_without_marks", "lines": bare[:10]})


def check_drift(org_id: UUID, sheet_id: Any, month: str, fresh: Optional[int], saved: int) -> Optional[Found]:
    """I-3 (d), a CLOSED month: the payroll recalculated now says what the saved sheet says.  ``fresh`` None: it cannot be recalculated (a rate is missing) - nothing to compare."""
    if fresh is None or fresh == int(saved):
        return None
    return Found(org_id, "I-3", f"sheet-drift:{sheet_id}", str(sheet_id), month, int(fresh), int(saved), {"what": "drift", "changedBy": int(fresh) - int(saved)})


# ====================================================================================================== pure checks: I-4
def check_unresolved(org_id: UUID, teacher_id: Any, month: str, unresolved: Sequence[str]) -> Optional[Found]:
    """I-4: a closed month of a teacher with lessons that nobody can resolve any more."""
    if not unresolved:
        return None
    return Found(org_id, "I-4", f"closed-open-lessons:{teacher_id}:{month}", str(teacher_id), month, 0, len(unresolved), {"lessons": sorted(unresolved)[:10]})


# ====================================================================================================== the loader
def _latest(into: Dict[Any, datetime], key: Any, moment: Optional[datetime]) -> None:
    if moment is not None and (key not in into or moment > into[key]):
        into[key] = moment


def _explaining_events(db, org_id: Any, since_by_membership: Dict[UUID, datetime]) -> Tuple[set, set]:
    """``(money_explained, coverage_explained)``: the memberships with a record created / cancelled after their snapshot.  MONEY records move the gross payments; anything that can lawfully move what
    the payments COVER (the price of the group, its schedule, a day off, an excuse, a lesson cancelled / returned / held, the settings of the centre) moves the coverage."""
    if not since_by_membership:
        return set(), set()
    oldest = min(since_by_membership.values())
    ids = list(since_by_membership)
    money: Dict[UUID, datetime] = {}
    for model in (StudentPayment, StudentWriteoff, StudentRefund, MembershipOpeningBalance, MembershipPause):
        for membership_id, created, voided in (db.query(model.membership_id, model.created_at, model.voided_at)
                                               .filter(model.org_id == org_id, model.membership_id.in_(ids), or_(model.created_at > oldest, model.voided_at > oldest)).all()):
            _latest(money, membership_id, created)
            _latest(money, membership_id, voided)
    for membership_id, created in db.query(MembershipPriceOverride.membership_id, MembershipPriceOverride.created_at).filter(
            MembershipPriceOverride.membership_id.in_(ids), MembershipPriceOverride.created_at > oldest).all():
        _latest(money, membership_id, created)
    for src, dst, created, voided in db.query(MembershipTransfer.from_membership_id, MembershipTransfer.to_membership_id, MembershipTransfer.created_at, MembershipTransfer.voided_at).filter(
            MembershipTransfer.org_id == org_id, or_(MembershipTransfer.from_membership_id.in_(ids), MembershipTransfer.to_membership_id.in_(ids)),
            or_(MembershipTransfer.created_at > oldest, MembershipTransfer.voided_at > oldest)).all():
        for member in (src, dst):
            _latest(money, member, created)
            _latest(money, member, voided)
    groups = {m: g for m, g in db.query(StudentGroup.id, StudentGroup.group_id).filter(StudentGroup.id.in_(ids)).all()}
    for membership_id, joined, left in db.query(StudentGroup.id, StudentGroup.joined_at, StudentGroup.left_at).filter(StudentGroup.id.in_(ids)).all():
        _latest(money, membership_id, joined)
        _latest(money, membership_id, left)

    # ---- what moves the coverage but not the gross payments: per group, and for the whole centre
    group_ids = set(groups.values())
    by_group: Dict[UUID, datetime] = {}
    if group_ids:
        for group_id, moment in db.query(GroupPrice.group_id, func.max(GroupPrice.created_at)).filter(GroupPrice.group_id.in_(group_ids), GroupPrice.created_at > oldest).group_by(GroupPrice.group_id).all():
            _latest(by_group, group_id, moment)
        for group_id, moment in db.query(GroupScheduleOverride.group_id, func.max(func.greatest(GroupScheduleOverride.created_at, GroupScheduleOverride.updated_at))).filter(
                GroupScheduleOverride.group_id.in_(group_ids), func.greatest(GroupScheduleOverride.created_at, GroupScheduleOverride.updated_at) > oldest).group_by(GroupScheduleOverride.group_id).all():
            _latest(by_group, group_id, moment)
        for group_id, moment in db.query(GroupScheduleOneOffLesson.group_id, func.max(func.greatest(GroupScheduleOneOffLesson.created_at, GroupScheduleOneOffLesson.updated_at))).filter(
                GroupScheduleOneOffLesson.group_id.in_(group_ids), func.greatest(GroupScheduleOneOffLesson.created_at, GroupScheduleOneOffLesson.updated_at) > oldest).group_by(GroupScheduleOneOffLesson.group_id).all():
            _latest(by_group, group_id, moment)
        for group_id, moment in db.query(GroupAttendance.group_id, func.max(func.greatest(GroupAttendance.created_at, GroupAttendance.updated_at))).filter(
                GroupAttendance.group_id.in_(group_ids), func.greatest(GroupAttendance.created_at, GroupAttendance.updated_at) > oldest).group_by(GroupAttendance.group_id).all():
            _latest(by_group, group_id, moment)
        for group_id, moment in db.query(GroupAttendance.group_id, func.max(func.coalesce(AttendanceExcuse.reviewed_at, AttendanceExcuse.reason_at))).join(
                AttendanceExcuse, AttendanceExcuse.attendance_id == GroupAttendance.id).filter(
                GroupAttendance.group_id.in_(group_ids), func.coalesce(AttendanceExcuse.reviewed_at, AttendanceExcuse.reason_at) > oldest).group_by(GroupAttendance.group_id).all():
            _latest(by_group, group_id, moment)
        for group_id, moment in db.query(Group.id, Group.updated_at).filter(Group.id.in_(group_ids), Group.updated_at > oldest).all():
            _latest(by_group, group_id, moment)
    centre: Optional[datetime] = None
    for created, voided in db.query(OrgDayOff.created_at, OrgDayOff.voided_at).filter(OrgDayOff.org_id == org_id, or_(OrgDayOff.created_at > oldest, OrgDayOff.voided_at > oldest)).all():
        for moment in (created, voided):
            if moment is not None and (centre is None or moment > centre):
                centre = moment
    org_moment = db.query(Organization.updated_at).filter(Organization.id == org_id).scalar()
    if org_moment is not None and org_moment > oldest and (centre is None or org_moment > centre):
        centre = org_moment

    money_explained = {m for m, moment in money.items() if moment > since_by_membership[m]}
    coverage_explained = set(money_explained)
    for membership_id, since in since_by_membership.items():
        if (by_group.get(groups.get(membership_id)) or since) > since or (centre is not None and centre > since):
            coverage_explained.add(membership_id)
    return money_explained, coverage_explained


def _row_sums(db, org_id: UUID, ids: List[UUID]) -> Dict[UUID, RowSums]:
    """The sums of the rows of the database per membership: payments (live, positive), refunds (live), write-offs (live) - read straight from the tables."""
    out: Dict[UUID, RowSums] = {m: RowSums() for m in ids}
    for membership_id, total in db.query(StudentPayment.membership_id, func.sum(StudentPayment.amount)).filter(
            StudentPayment.org_id == org_id, StudentPayment.membership_id.in_(ids), StudentPayment.voided_at.is_(None), StudentPayment.kind == "payment", StudentPayment.amount > 0).group_by(StudentPayment.membership_id).all():
        out[membership_id].paid = int(total)
    for membership_id, total in db.query(StudentRefund.membership_id, func.sum(StudentRefund.amount)).filter(
            StudentRefund.org_id == org_id, StudentRefund.membership_id.in_(ids), StudentRefund.voided_at.is_(None)).group_by(StudentRefund.membership_id).all():
        out[membership_id].refunded = int(total)
    for membership_id, total in db.query(StudentWriteoff.membership_id, func.sum(StudentWriteoff.amount)).filter(
            StudentWriteoff.org_id == org_id, StudentWriteoff.membership_id.in_(ids), StudentWriteoff.voided_at.is_(None)).group_by(StudentWriteoff.membership_id).all():
        out[membership_id].written_off = int(total)
    return out


def reconcile_org(db, org_id: Any, today: Optional[date] = None, *, snapshots: Optional[List[Tuple[Any, Optional[date], Optional[int], Optional[int]]]] = None) -> List[Found]:
    """All four checks over one centre.  ``snapshots`` (when given) is filled with the new ``(membership_id, covered_through, lessons_covered, paid)`` for ``run`` to store."""
    from .payments_service import PaymentService

    today = today or bc.business_today()
    org_uuid = org_id if isinstance(org_id, UUID) else UUID(str(org_id))
    found: List[Found] = []

    # ---- I-1 and I-2: the ledgers (the way the screens show them: with the deduction for the lessons the centre cancelled)
    memberships = [m for (m,) in db.query(StudentGroup.id).join(Group, Group.id == StudentGroup.group_id).filter(Group.org_id == org_uuid).all()]
    old = {s.membership_id: s for s in db.query(MoneyReconcileSnapshot).filter(MoneyReconcileSnapshot.org_id == org_uuid).all()}
    money_explained, coverage_explained = _explaining_events(db, org_uuid, {m: s.taken_at for m, s in old.items()})
    loader = PaymentService(db)._loader(str(org_uuid), recalc=True)
    for start in range(0, len(memberships), LOADER_CHUNK):
        chunk = memberships[start:start + LOADER_CHUNK]
        sums = _row_sums(db, org_uuid, chunk)
        for entry in loader.run(chunk):
            ledger, mid = entry.ledger, entry.membership.id
            for item in (check_identity(org_uuid, mid, ledger), check_statement(org_uuid, mid, ledger), *check_rows(org_uuid, mid, ledger, sums.get(mid, RowSums()))):
                if item is not None:
                    found.append(item)
            now = coverage_of(ledger)
            before = (old[mid].covered_through, old[mid].lessons_covered, old[mid].paid) if mid in old else None
            if (item := check_paid_until(org_uuid, mid, before, now, mid in coverage_explained, mid in money_explained)) is not None:
                found.append(item)
            if snapshots is not None:
                snapshots.append((mid, now[0], now[1], now[2]))

    periods = ClosedPeriods.of(db, org_uuid)
    # ---- I-3: the saved sheets
    sheets = db.query(PayrollSheet).filter(PayrollSheet.org_id == org_uuid, PayrollSheet.period_month <= today).all()
    closed_sheets = [s for s in sheets if periods.closed_for([s.teacher_id], s.period_month) is not None]
    closed_ids = {s.id for s in closed_sheets}
    wanted: set = set()
    per_sheet: Dict[UUID, List[Tuple[str, str, str]]] = {}
    for sheet in sheets:
        month = sheet.period_month.strftime("%Y-%m")
        if (item := check_sheet_total(org_uuid, sheet.id, month, sheet.lines or [], sheet.total_amount)) is not None:
            found.append(item)
        if sheet.id in closed_ids:
            per_sheet[sheet.id] = paid_marks(sheet.lines or [])
            wanted |= set(per_sheet[sheet.id])
            if (item := check_line_marks(org_uuid, sheet.id, month, sheet.lines or [])) is not None:
                found.append(item)
    status_of: Dict[Tuple[str, str, str], str] = {}
    if wanted:
        dates = sorted({d for _, d, _ in wanted})
        groups = {UUID(g) for g, _, _ in wanted if g not in ("None", "")}
        for group_id, lesson_date, lesson_id, status in db.query(GroupAttendance.group_id, GroupAttendance.lesson_date, GroupAttendance.lesson_id, GroupAttendance.status).filter(
                GroupAttendance.org_id == org_uuid, GroupAttendance.group_id.in_(groups), GroupAttendance.lesson_date >= dates[0], GroupAttendance.lesson_date <= dates[-1]).all():
            status_of[(str(group_id), lesson_date, str(lesson_id))] = status
    for sheet in closed_sheets:                    # R3 #3: the check of the lessons is for CLOSED months only (an open month's sheet is a stage and goes stale lawfully)
        if (item := check_paid_lessons(org_uuid, sheet.id, sheet.period_month.strftime("%Y-%m"), per_sheet[sheet.id], status_of)) is not None:
            found.append(item)
    found.extend(_drift(db, org_uuid, closed_sheets, today))

    # ---- I-4: the closed months with an unresolved lesson (going on or never started: the same list the screens show, closed months included)
    from .service import AuthService

    by_month: Dict[Tuple[str, str], List[str]] = {}
    for action in AuthService(db).get_open_attendance_actions(str(org_uuid), actor_id=None, is_org_admin=True, include_closed=True):
        if action.periodClosed and action.teacherId:
            by_month.setdefault((action.teacherId, month_first(action.lessonDate).strftime("%Y-%m")), []).append(f"{action.lessonDate}|{action.groupId}")
    for (teacher_id, month), items in sorted(by_month.items()):
        if (item := check_unresolved(org_uuid, teacher_id, month, items)) is not None:
            found.append(item)
    return found


def _drift(db, org_uuid: UUID, closed_sheets: List[PayrollSheet], today: date) -> List[Found]:
    """I-3 (d) over the last ``DRIFT_MONTHS`` closed months: one calculation of each month for all its sheets (the payroll's own ``_collect``)."""
    from .payroll_service import PayrollService

    if not closed_sheets:
        return []
    recent = sorted({s.period_month for s in closed_sheets}, reverse=True)[:DRIFT_MONTHS]
    service = PayrollService(db)
    out: List[Found] = []
    for first in recent:
        month_sheets = [s for s in closed_sheets if s.period_month == first]
        last = date(first.year + (first.month == 12), first.month % 12 + 1, 1) - timedelta(days=1)
        try:
            seed = service._collect(str(org_uuid), first, last, teacher_ids={s.teacher_id for s in month_sheets})
        except Exception:
            logger.exception("money reconcile: the month %s of org %s could not be calculated", first, org_uuid)
            continue
        for sheet in month_sheets:
            try:
                fresh = service._fresh_total(str(org_uuid), sheet, seed)
            except Exception:                  # a sheet the payroll cannot even read is not a reason to lose the check of the others
                logger.exception("money reconcile: the sheet %s could not be recalculated", sheet.id)
                continue
            if (item := check_drift(org_uuid, sheet.id, first.strftime("%Y-%m"), fresh, int(sheet.total_amount))) is not None:
                out.append(item)
    return out


# ====================================================================================================== the run
def _team_address() -> str:
    """The mailbox of the team: ``MONEY_RECONCILE_EMAIL`` of the environment, else the support mailbox of the mail settings.  Never printed, never stored."""
    from src.mail import config as mail_config

    return (os.getenv("MONEY_RECONCILE_EMAIL") or "").strip() or mail_config.load().support_email


class _RunLock:
    """One run at a time: a session advisory lock on a connection of its own (the run commits and rolls back its session many times)."""

    def __init__(self, db) -> None:
        bind = db.get_bind()
        self.engine = getattr(bind, "engine", bind)
        self.conn = None
        self.held = False

    def __enter__(self) -> "_RunLock":
        self.conn = self.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        self.held = bool(self.conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar())
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.held:
                self.conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
        finally:
            self.conn.close()


def _read_only(db) -> None:
    """The reading of a centre: a read-only transaction with a time limit (one heavy centre must not hold a transaction for hours; nothing of the check can write to a centre)."""
    db.rollback()
    db.execute(text("SET TRANSACTION READ ONLY"))
    db.execute(text(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}"))


def run(db, *, apply: bool = False, mail: bool = False, today: Optional[date] = None, budget_seconds: Optional[int] = None, only: Optional[Iterable[Any]] = None) -> Dict[str, Any]:
    """Check every active centre.  ``apply=False`` (the default) is a dry run: nothing is written, nothing is sent.  Returns the counters and the findings (ids only).  ``busy``: another run is going.  ``only``: check just these centres (a manual check of one centre; the tests).  A run on ``only`` is never a basis for "the last
    successful check" of the others: their rows are untouched."""
    with _RunLock(db) as lock:
        if not lock.held:
            return {"busy": True, "centres": 0, "failed": [], "findings": 0, "new": 0, "apply": apply, "mail": "off", "byInvariant": {k: 0 for k in INVARIANT_TEXT}, "items": []}
        return _run_locked(db, apply=apply, mail=mail, today=today, budget_seconds=BUDGET_SECONDS if budget_seconds is None else budget_seconds, only=only)


def _run_locked(db, *, apply: bool, mail: bool, today: Optional[date], budget_seconds: int, only: Optional[Iterable[Any]] = None) -> Dict[str, Any]:
    started = bc.utc_now()
    began = time.monotonic()
    query = db.query(Organization.id).filter(Organization.status == "active")
    if only is not None:
        query = query.filter(Organization.id.in_([o if isinstance(o, UUID) else UUID(str(o)) for o in only]))
    orgs = [o for (o,) in query.order_by(Organization.id).all()]
    findings: List[Found] = []
    snapshots: Dict[UUID, List[Tuple[Any, Optional[date], Optional[int], Optional[int]]]] = {}
    per_centre: Dict[UUID, int] = {}
    failed: List[str] = []
    for org_id in orgs:
        if time.monotonic() - began > budget_seconds:
            failed.append(str(org_id))                      # no time left: named as not checked, like any other failure
            continue
        try:
            _read_only(db)
            snaps: List[Tuple[Any, Optional[date], Optional[int], Optional[int]]] = []
            mine = reconcile_org(db, org_id, today, snapshots=snaps)
            findings.extend(mine)
            snapshots[org_id] = snaps
            per_centre[org_id] = len(mine)
        except Exception:
            logger.exception("money reconcile failed for org %s", org_id)
            failed.append(str(org_id))
        finally:
            db.rollback()
    previous = _previous_keys(db, [o for o in orgs if str(o) not in failed]) if apply else set()
    new_count = sum(1 for f in findings if (f.org_id, f.invariant, f.subject_key) not in previous)
    out: Dict[str, Any] = {"centres": len(orgs), "failed": failed, "findings": len(findings), "new": new_count, "apply": apply, "mail": "off",
                           "byInvariant": {k: sum(1 for f in findings if f.invariant == k) for k in INVARIANT_TEXT},
                           "items": [{"org": str(f.org_id), "invariant": f.invariant, "key": f.subject_key, "month": f.month, "expected": f.expected, "actual": f.actual} for f in findings]}
    if not apply:
        return out
    run_row = MoneyReconcileRun(started_at=started, finished_at=bc.utc_now(), status="failed" if failed and not findings else ("findings" if findings else "clean"),
                                centres_checked=len(orgs) - len(failed), findings_count=len(findings), new_findings_count=new_count,
                                error=("не удалось проверить центров: %d" % len(failed)) if failed else None)
    db.add(run_row)
    db.flush()
    for f in findings:
        db.add(MoneyReconcileFinding(run_id=run_row.id, org_id=f.org_id, invariant=f.invariant, subject_key=f.subject_key, subject_id=f.subject_id, month=f.month, expected=f.expected,
                                     actual=f.actual, details=f.details, is_new=(f.org_id, f.invariant, f.subject_key) not in previous))
    for org_id in orgs:
        db.add(MoneyReconcileCentre(run_id=run_row.id, org_id=org_id, status="failed" if str(org_id) in failed else "ok", findings_count=per_centre.get(org_id, 0)))
    _store_snapshots(db, snapshots, bc.utc_now())
    if mail:
        run_row.mail_status = _mail(findings, new_count, failed, run_row.id)
        out["mail"] = run_row.mail_status
    db.commit()
    out["runId"] = str(run_row.id)
    return out


def _store_snapshots(db, snapshots: Dict[UUID, List[Tuple[Any, Optional[date], Optional[int], Optional[int]]]], taken: datetime) -> None:
    """One upsert per chunk (no ``get`` per membership); a centre that could not be checked has no snapshot of the night, so its next night is compared with its last good one."""
    for org_id, rows in snapshots.items():
        for start in range(0, len(rows), 1000):
            values = [{"membership_id": m, "org_id": org_id, "covered_through": through, "lessons_covered": lessons, "paid": paid, "taken_at": taken} for m, through, lessons, paid in rows[start:start + 1000]]
            insert = pg_insert(MoneyReconcileSnapshot).values(values)
            db.execute(insert.on_conflict_do_update(index_elements=[MoneyReconcileSnapshot.membership_id], set_={
                "covered_through": insert.excluded.covered_through, "lessons_covered": insert.excluded.lessons_covered, "paid": insert.excluded.paid, "taken_at": insert.excluded.taken_at,
                "org_id": insert.excluded.org_id}))


def _previous_keys(db, org_ids: List[UUID]) -> set:
    """The findings of the LAST SUCCESSFUL check of each centre (a night the centre could not be checked says nothing about it): ``{(org, invariant, key)}``."""
    if not org_ids:
        return set()
    last = (db.query(MoneyReconcileCentre.org_id, MoneyReconcileCentre.run_id)
            .join(MoneyReconcileRun, MoneyReconcileRun.id == MoneyReconcileCentre.run_id)
            .filter(MoneyReconcileCentre.status == "ok", MoneyReconcileCentre.org_id.in_(org_ids))
            .ext(distinct_on(MoneyReconcileCentre.org_id)).order_by(MoneyReconcileCentre.org_id, MoneyReconcileRun.started_at.desc()).all())
    if not last:
        return set()
    return {(o, i, k) for o, i, k in db.query(MoneyReconcileFinding.org_id, MoneyReconcileFinding.invariant, MoneyReconcileFinding.subject_key).filter(
        tuple_(MoneyReconcileFinding.org_id, MoneyReconcileFinding.run_id).in_([(org, rid) for org, rid in last])).all()}


def _mail(findings: List[Found], new_count: int, failed: List[str], run_id: Any) -> str:
    """One letter to the team when there is something NEW (a finding that stood last night too is in the console, not in the mailbox again) or a centre could not be checked.  ``skipped``: nothing
    to say.  Counts, ids and sums only."""
    if not (findings and new_count) and not failed:
        return "skipped"
    from src.mail import letters, sender

    try:
        if findings and new_count:
            rows = [(f"{f.invariant} · центр {str(f.org_id)[:8]}", f"{INVARIANT_TEXT[f.invariant]}; {f.subject_key}" + (f"; {f.month}" if f.month else "") +
                     (f"; ожидалось {f.expected}, есть {f.actual}" if f.expected is not None or f.actual is not None else "")) for f in findings[:20]]
            if failed:
                rows.append(("Не проверено центров", f"{len(failed)}: " + ", ".join(x[:8] for x in failed[:10])))
            letter = letters.render_money_reconcile(total=len(findings), new=new_count, rows=rows, run_id=str(run_id))
        else:
            letter = letters.render_money_reconcile_failed(failed=len(failed), centres=[x[:8] for x in failed[:20]], run_id=str(run_id))
        ok = sender.send(_team_address(), letter)
    except Exception:
        logger.exception("money reconcile letter failed")
        ok = False
    return "sent" if ok else "failed"
