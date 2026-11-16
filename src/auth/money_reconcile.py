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
  Money-2: the gross sum of the payments moves ONLY by the payments added minus the payments cancelled since the baseline of the rows (by id) - not by «some event happened that night»;
* I-5 the immutable money rows (payments, refunds, write-offs, openings, transfers, payouts, rates) are compared row by row with the baseline of the last night (``money_reconcile_rows.py``);
* I-3 a SAVED payroll sheet: its total is the sum of its lines (every saved sheet); and for a CLOSED month (the sheet saved after the month ended) - no line pays without a counted mark, no mark
  pays a lesson that was not held, and the fresh total of the month, recalculated by the payroll itself, equals the saved one (a drift is a teacher who is paid less or more than the data says);
* I-4 a CLOSED month of a teacher has no unresolved lesson (going on or never started, from the accounting start of the centre) that the owner did NOT confirm: nobody can resolve it without reopening
  the month, and a lesson the owner confirmed on saving the sheet («Сохранить всё равно» to the list of the warning, ``payroll_sheets.confirmed_open_lessons``) is the decision of the centre, not a failure -
  the letters of the reconciliation are about failures of the system only.

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
    StudentRefund, StudentWriteoff, User,
)
from src.models.platform import PlatformStaff

from . import business_clock as bc
from . import money_reconcile_rows as mrows
from . import payroll_engine as engine
from .closed_period import ClosedPeriods, month_first, open_lesson_key

logger = logging.getLogger(__name__)

INVARIANT_TEXT = {
    "I-1": "долг, начислено и оплачено не сходятся",
    "I-2": "«Оплачено до» изменилось без записи в журнале",
    "I-3": "сохранённая зарплата за месяц не сходится с уроками",
    "I-4": "в закрытом месяце остался урок без отметки",
    "I-5": "запись денег изменена, удалена или отменена мимо журнала",
}
ROW_CODE_TEXT = {
    "changed": "запись изменена на месте", "deleted": "запись удалена", "reopened": "недопустимый переход записи (отмена снята или изменена)",
    "no_journal": "запись отменена без строки в журнале", "foreign": "запись не своего участия", "untraced": "новая оплата без снимка квитанции",
}
GAP_PAYROLL_ERROR = "payroll_error"              # a month / a sheet the payroll could not recalculate (an exception)
GAP_PAYROLL_NO_FRESH = "payroll_no_fresh"        # a sheet the payroll cannot price (a rate is missing): nothing to compare
GAP_NO_SNAPSHOT = "membership_no_snapshot"       # a membership with no snapshot yet: I-2 has nothing to compare on its first night
GAP_CENTRE_INACTIVE = "centre_not_active"        # a centre that is not active is not checked at all
PAYROLL_GAPS = (GAP_PAYROLL_ERROR, GAP_PAYROLL_NO_FRESH)            # a run with one of these is not «clean»
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
def _gap(gaps: Optional[Dict[str, int]], reason: str, count: int = 1) -> None:
    """Count what the night could NOT check, by reason (a quiet «nothing found» must not hide it, LESSONS 133)."""
    if gaps is not None and count > 0:
        gaps[reason] = gaps.get(reason, 0) + count


def _row_findings(org_id: UUID, verdict: "mrows.RowVerdict") -> List[Found]:
    """I-5 findings of the comparison of the rows as ``Found`` (key = kind + id + code: the same row says the same key every night)."""
    return [Found(org_id, "I-5", f.key, str(f.row_id), None, f.was, f.now, {"kind": f.kind, "code": f.code, "what": ROW_CODE_TEXT[f.code], "state": f.state}) for f in verdict.findings]


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

    # Money-1 R3 code №9: the «оплачено до» / the money of a membership that came by a TRANSFER is derived from the facts of the groups it came from (the paid days of their openings, their payments, pauses): a
    # lawful correction there moves the coverage here without a record on this membership - it explains it (the sources of the sources too)
    via_sources: Dict[UUID, datetime] = {}
    sources_of: Dict[UUID, set] = {}
    frontier = set(ids)
    for _hop in range(8):
        found = db.query(MembershipTransfer.to_membership_id, MembershipTransfer.from_membership_id).filter(
            MembershipTransfer.org_id == org_id, MembershipTransfer.voided_at.is_(None), MembershipTransfer.to_membership_id.in_(list(frontier))).all()
        if not found:
            break
        frontier = set()
        for target, source in found:
            for owner_id in [m for m, srcs in sources_of.items() if target in srcs] + [target]:
                sources_of.setdefault(owner_id, set()).add(source)
            frontier.add(source)
    every_source = set().union(*sources_of.values()) if sources_of else set()
    if every_source:
        moments: Dict[UUID, datetime] = {}
        for model in (StudentPayment, StudentWriteoff, StudentRefund, MembershipOpeningBalance, MembershipPause):
            for membership_id, created, voided in (db.query(model.membership_id, model.created_at, model.voided_at)
                                                   .filter(model.org_id == org_id, model.membership_id.in_(list(every_source)), or_(model.created_at > oldest, model.voided_at > oldest)).all()):
                _latest(moments, membership_id, created)
                _latest(moments, membership_id, voided)
        for target, sources in sources_of.items():
            if target in since_by_membership:
                for source in sources:
                    if source in moments:
                        _latest(via_sources, target, moments[source])

    # a transfer names BOTH its ends: the receiving membership born after the last night has no snapshot - nothing to explain there (its event still explains the source through the loop above)
    money_explained = {m for m, moment in money.items() if m in since_by_membership and moment > since_by_membership[m]}
    coverage_explained = set(money_explained) | {m for m, moment in via_sources.items() if moment > since_by_membership[m]}
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


def reconcile_org(db, org_id: Any, today: Optional[date] = None, *, snapshots: Optional[List[Tuple[Any, Optional[date], Optional[int], Optional[int]]]] = None,
                  plan: Optional[List[Tuple[UUID, "mrows.RowVerdict"]]] = None, gaps: Optional[Dict[str, int]] = None) -> List[Found]:
    """All five checks over one centre.  ``snapshots`` (when given) is filled with the new ``(membership_id, covered_through, lessons_covered, paid)`` for ``run`` to store; ``plan`` with the
    comparison of the money rows (what ``run`` writes to the baseline); ``gaps`` with what the night could not check, by reason."""
    from .payments_service import PaymentService

    today = today or bc.business_today()
    org_uuid = org_id if isinstance(org_id, UUID) else UUID(str(org_id))
    found: List[Found] = []

    # ---- I-5 FIRST: the rows are read before anything else (and before the journal) in the one REPEATABLE READ transaction of the centre
    verdict = mrows.evaluate(db, org_uuid)
    found.extend(_row_findings(org_uuid, verdict))
    if plan is not None:
        plan.append((org_uuid, verdict))

    # ---- I-1 and I-2: the ledgers (the way the screens show them: with the deduction for the lessons the centre cancelled)
    memberships = [m for (m,) in db.query(StudentGroup.id).join(Group, Group.id == StudentGroup.group_id).filter(Group.org_id == org_uuid).all()]
    old = {s.membership_id: s for s in db.query(MoneyReconcileSnapshot).filter(MoneyReconcileSnapshot.org_id == org_uuid).all()}
    _, coverage_explained = _explaining_events(db, org_uuid, {m: s.taken_at for m, s in old.items()})
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
            if before is None:
                _gap(gaps, GAP_NO_SNAPSHOT)
            # the gross payments move only by the payments added minus the payments cancelled since the baseline of the rows; the first night of the centre has no baseline to say it
            paid_explained = verdict.first or before is None or now[2] - (before[2] or 0) == verdict.paid_delta.get(mid, 0)
            if (item := check_paid_until(org_uuid, mid, before, now, mid in coverage_explained, paid_explained)) is not None:
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
    found.extend(_drift(db, org_uuid, closed_sheets, today, gaps))

    # ---- I-4: the closed months with an unresolved lesson (going on or never started: the same list the screens show, closed months included)
    from .service import AuthService

    # What the owner CONFIRMED when he saved the sheet («Сохранить всё равно» to the warning with the list) is the decision of the centre, not a failure of the system: it is taken off.  The list is the one of the
    # LAST save of the sheet (a reopening keeps it; a save without the question clears it); a lesson that became unresolved after the closing, or was never in the list, stays a finding.
    confirmed: Dict[Tuple[str, str], set] = {}
    for teacher_id, first, keys in db.query(PayrollSheet.teacher_id, PayrollSheet.period_month, PayrollSheet.confirmed_open_lessons).filter(PayrollSheet.org_id == org_uuid).all():
        confirmed[(str(teacher_id), first.strftime("%Y-%m"))] = set(keys or [])
    by_month: Dict[Tuple[str, str], List[str]] = {}
    for action in AuthService(db).get_open_attendance_actions(str(org_uuid), actor_id=None, is_org_admin=True, include_closed=True):
        if action.periodClosed and action.teacherId:
            month = month_first(action.lessonDate).strftime("%Y-%m")
            if open_lesson_key(action.lessonDate, action.groupId, action.lessonId) in confirmed.get((action.teacherId, month), set()):
                continue
            by_month.setdefault((action.teacherId, month), []).append(f"{action.lessonDate}|{action.groupId}")
    for (teacher_id, month), items in sorted(by_month.items()):
        if (item := check_unresolved(org_uuid, teacher_id, month, items)) is not None:
            found.append(item)
    return found


def _drift(db, org_uuid: UUID, closed_sheets: List[PayrollSheet], today: date, gaps: Optional[Dict[str, int]] = None) -> List[Found]:
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
            _gap(gaps, GAP_PAYROLL_ERROR, len(month_sheets))
            continue
        for sheet in month_sheets:
            try:
                fresh = service._fresh_total(str(org_uuid), sheet, seed)
            except Exception:                  # a sheet the payroll cannot even read is not a reason to lose the check of the others
                logger.exception("money reconcile: the sheet %s could not be recalculated", sheet.id)
                _gap(gaps, GAP_PAYROLL_ERROR)
                continue
            if fresh is None:                  # no rate / a manual price: nothing to compare - and the night says so (it is not «checked»)
                _gap(gaps, GAP_PAYROLL_NO_FRESH)
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
    """The reading of a centre: ONE read-only ``REPEATABLE READ`` transaction with a time limit - the whole centre is seen as of one moment (a payment, its receipt and its line of the journal are committed
    together and are never seen apart; a payment made while the centre is read is not in the ledger and not in the rows, so the sums of the two paths stay equal).  One heavy centre must not hold
    a transaction for hours; nothing of the check can write to a centre."""
    db.rollback()
    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    db.execute(text(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}"))


def run(db, *, apply: bool = False, mail: bool = False, today: Optional[date] = None, budget_seconds: Optional[int] = None, only: Optional[Iterable[Any]] = None) -> Dict[str, Any]:
    """Check every active centre.  ``apply=False`` (the default) is a dry run: nothing is written, nothing is sent.  Returns the counters and the findings (ids only).  ``busy``: another run is going.  ``only``: check just these centres (a manual check of one centre; the tests).  A run on ``only`` is never a basis for "the last
    successful check" of the others: their rows are untouched."""
    with _RunLock(db) as lock:
        if not lock.held:
            return {"busy": True, "centres": 0, "failed": [], "findings": 0, "new": 0, "apply": apply, "mail": "off", "byInvariant": {k: 0 for k in INVARIANT_TEXT}, "items": []}
        return _run_locked(db, apply=apply, mail=mail, today=today, budget_seconds=BUDGET_SECONDS if budget_seconds is None else budget_seconds, only=only)


def accept(db, key: str, reason: str, *, actor: Optional[str]) -> Dict[str, Any]:
    """The team accepts ONE finding of I-5 by its key (``kind:row id:code``) with a reason: the baseline of the row takes the row as it is and the journal of the centre says who / why / the old and the
    new fingerprint.  ``actor``: an active user of the platform (required).  ``busy``: another run is going.  Raises ``ValueError`` when the key is not a finding right now."""
    with _RunLock(db) as lock:
        if not lock.held:
            return {"busy": True}
        db.rollback()
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))          # first statement of the transaction: the same view of the centre as the night has
        try:
            out = mrows.accept(db, key, reason, actor_id=platform_actor(db, actor), at=bc.utc_now())
            db.commit()
        except Exception:
            db.rollback()
            raise
        return out


def _uuids(items: Iterable[Any]) -> List[UUID]:
    return [o if isinstance(o, UUID) else UUID(str(o)) for o in items]


def platform_actor(db, actor: Optional[str]) -> UUID:
    """The author of a manual move of the baseline: the id (or the e-mail) of an ACTIVE user of the platform (the console staff).  Anyone else is refused."""
    text_ = (actor or "").strip()
    if not text_:
        raise ValueError("Автор обязателен: --actor (id или e-mail сотрудника платформы)")
    try:
        user_filter = User.id == UUID(text_)
    except ValueError:
        user_filter = func.lower(User.email) == text_.lower()
    found = db.query(User.id).join(PlatformStaff, PlatformStaff.user_id == User.id).filter(user_filter, PlatformStaff.is_active.is_(True), User.is_active.is_(True)).scalar()
    if found is None:
        raise ValueError("Автор — не активный сотрудник платформы")
    return found


def rebaseline(db, reason: str, *, actor: Optional[str], with_open_findings: bool = False, only: Optional[Iterable[Any]] = None) -> Dict[str, Any]:
    """Take the baseline of the money rows again for every active centre (or ``only``): after a migration of data or a restore of the database.  ``actor``: an active user of the platform (required).
    While the old baseline holds a row that differs or a finding is open it refuses, unless ``with_open_findings``: then their keys go to the note of the run and to the journal of the centre.  One run row
    (status ``rebaselined``, no centres, no findings) keeps the number of rows and the reason; each centre gets a line in its journal.  ``busy``: another run is going."""
    reason = (reason or "").strip()
    if not 3 <= len(reason) <= 300:
        raise ValueError("Причина: от 3 до 300 символов")
    with _RunLock(db) as lock:
        if not lock.held:
            return {"busy": True}
        db.rollback()
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))          # first statement of the transaction: one view of the centres
        started = bc.utc_now()
        try:
            actor_id = platform_actor(db, actor)          # Money-2 T21: inside the ``try`` - a refused author closes the transaction too (as the description of the function says)
            query = db.query(Organization.id).filter(Organization.status == "active")
            if only is not None:
                query = query.filter(Organization.id.in_(_uuids(only)))
            orgs = [o for (o,) in query.order_by(Organization.id).all()]
            run_row = MoneyReconcileRun(started_at=started, finished_at=started, status="rebaselined", centres_checked=0, findings_count=0, new_findings_count=0)
            db.add(run_row)
            db.flush()
            plan = mrows.rebaseline(db, orgs, run_row.id, reason, started, actor_id=actor_id, with_open_findings=with_open_findings)
            rows = sum(len(c.now) for c in plan.values())
            named = [k for c in plan.values() for k in c.findings]
            divergent = sum(len(c.divergent) for c in plan.values())
            tail = f"; расходящихся строк {divergent}, открытых находок {len(named)}: " + ", ".join(named) if named or divergent else ""
            run_row.note = f"пересъёмка эталона: центров {len(orgs)}, записей {rows}; причина: {reason}{tail}"[:500]
            db.commit()
        except Exception:
            db.rollback()
            raise
        return {"centres": len(orgs), "rows": rows, "runId": str(run_row.id), "note": run_row.note, "divergent": divergent, "findings": named, "actor": str(actor_id)}


def _run_locked(db, *, apply: bool, mail: bool, today: Optional[date], budget_seconds: int, only: Optional[Iterable[Any]] = None) -> Dict[str, Any]:
    started = bc.utc_now()
    began = time.monotonic()
    query = db.query(Organization.id).filter(Organization.status == "active")
    if only is not None:
        query = query.filter(Organization.id.in_(_uuids(only)))
    orgs = [o for (o,) in query.order_by(Organization.id).all()]
    not_checked: Dict[str, int] = {}
    inactive = db.query(func.count(Organization.id)).filter(Organization.status != "active")
    if only is not None:
        inactive = inactive.filter(Organization.id.in_(_uuids(only)))
    _gap(not_checked, GAP_CENTRE_INACTIVE, int(inactive.scalar() or 0))
    db.rollback()
    findings: List[Found] = []
    snapshots: Dict[UUID, List[Tuple[Any, Optional[date], Optional[int], Optional[int]]]] = {}
    plans: Dict[UUID, "mrows.RowVerdict"] = {}
    read_at: Dict[UUID, datetime] = {}                       # the moment each centre began to be read: the snapshot is «as of» it, not of the end of the run (a record made while the run goes is NEWER than the snapshot)
    per_centre: Dict[UUID, int] = {}
    failed: List[str] = []
    why: Dict[str, str] = {}                                # centre id -> the first line of its error (the letter and the console name it)
    for org_id in orgs:
        if time.monotonic() - began > budget_seconds:
            failed.append(str(org_id))                      # no time left: named as not checked, like any other failure
            why[str(org_id)] = "TimeBudget: no time left in the run"
            continue
        try:
            read_at[org_id] = bc.utc_now()
            _read_only(db)
            snaps: List[Tuple[Any, Optional[date], Optional[int], Optional[int]]] = []
            plan: List[Tuple[UUID, "mrows.RowVerdict"]] = []
            centre_gaps: Dict[str, int] = {}
            mine = reconcile_org(db, org_id, today, snapshots=snaps, plan=plan, gaps=centre_gaps)
            findings.extend(mine)
            snapshots[org_id] = snaps
            plans[org_id] = plan[0][1]
            for reason, count in centre_gaps.items():          # only a centre that WAS checked adds to the counters (a failed one is named as failed)
                _gap(not_checked, reason, count)
            per_centre[org_id] = len(mine)
        except Exception as exc:
            logger.exception("money reconcile failed for org %s", org_id)
            failed.append(str(org_id))
            why[str(org_id)] = _first_line(exc)
        finally:
            db.rollback()
    previous = _previous_findings(db, [o for o in orgs if str(o) not in failed]) if apply else {}
    for f in findings:
        _mark_restated(f, previous)
    new_count = sum(1 for f in findings if _is_new(f, previous))
    rows_seen = sum(v.rows for v in plans.values())
    out: Dict[str, Any] = {"centres": len(orgs), "failed": failed, "failedWhy": why, "findings": len(findings), "new": new_count, "apply": apply, "mail": "off",
                           "rows": rows_seen, "baselined": sum(v.baselined for v in plans.values()), "firstCentres": sum(1 for v in plans.values() if v.first), "notChecked": dict(sorted(not_checked.items())),
                           "byInvariant": {k: sum(1 for f in findings if f.invariant == k) for k in INVARIANT_TEXT},
                           "items": [{"org": str(f.org_id), "invariant": f.invariant, "key": f.subject_key, "month": f.month, "expected": f.expected, "actual": f.actual} for f in findings]}
    if not apply:
        return out
    partial = any(not_checked.get(reason) for reason in PAYROLL_GAPS)      # a payroll that was not recalculated is not «checked»: the run is not clean
    run_row = MoneyReconcileRun(started_at=started, finished_at=bc.utc_now(), status="failed" if failed else ("findings" if findings else ("partial" if partial else "clean")),
                                centres_checked=len(orgs) - len(failed), findings_count=len(findings), new_findings_count=new_count,
                                error=_failed_text(failed, why) if failed else None, not_checked=dict(sorted(not_checked.items())) or None,
                                note=(f"эталон записей снят в первый раз: центров {out['firstCentres']}, записей {sum(v.baselined for v in plans.values() if v.first)}" if out["firstCentres"] else None))
    db.add(run_row)
    db.flush()
    for f in findings:
        db.add(MoneyReconcileFinding(run_id=run_row.id, org_id=f.org_id, invariant=f.invariant, subject_key=f.subject_key, subject_id=f.subject_id, month=f.month, expected=f.expected,
                                     actual=f.actual, details=f.details, is_new=_is_new(f, previous)))
    for org_id in orgs:
        db.add(MoneyReconcileCentre(run_id=run_row.id, org_id=org_id, status="failed" if str(org_id) in failed else "ok", findings_count=per_centre.get(org_id, 0),
                                    rows_checked=org_id in plans))
    for org_id, verdict in plans.items():
        mrows.store(db, org_id, run_row.id, verdict, read_at[org_id])
    _store_snapshots(db, snapshots, read_at)
    if mail:
        run_row.mail_status = _mail(findings, new_count, failed, run_row.id, why)
        out["mail"] = run_row.mail_status
    db.commit()
    out["runId"] = str(run_row.id)
    return out


def _first_line(exc: BaseException) -> str:
    """``ErrorType: first line of the message`` (cut short): enough for the team to see WHAT broke; the details stay in the log of the server."""
    text_ = (str(getattr(exc, "orig", None) or exc).strip().splitlines() or [""])[0]
    return f"{type(exc).__name__}: {text_}"[:140]


def _centre_why(org_id: str, why: Optional[Dict[str, str]]) -> str:
    return f"{org_id[:8]} ({(why or {}).get(org_id) or 'причина в логе сервера'})"


def _failed_text(failed: List[str], why: Dict[str, str]) -> str:
    """The error of the run (a column of 300): the count, then the id and the first line of the first failures."""
    head = "не удалось проверить центров: %d" % len(failed)
    for org_id in failed[:3]:
        part = f"; {org_id[:8]}: {why.get(org_id, '?')}"
        if len(head) + len(part) > 300:
            break
        head += part
    return head


def _store_snapshots(db, snapshots: Dict[UUID, List[Tuple[Any, Optional[date], Optional[int], Optional[int]]]], taken: Dict[UUID, datetime]) -> None:
    """One upsert per chunk (no ``get`` per membership); a centre that could not be checked has no snapshot of the night, so its next night is compared with its last good one."""
    for org_id, rows in snapshots.items():
        for start in range(0, len(rows), 1000):
            values = [{"membership_id": m, "org_id": org_id, "covered_through": through, "lessons_covered": lessons, "paid": paid, "taken_at": taken[org_id]} for m, through, lessons, paid in rows[start:start + 1000]]
            insert = pg_insert(MoneyReconcileSnapshot).values(values)
            db.execute(insert.on_conflict_do_update(index_elements=[MoneyReconcileSnapshot.membership_id], set_={
                "covered_through": insert.excluded.covered_through, "lessons_covered": insert.excluded.lessons_covered, "paid": insert.excluded.paid, "taken_at": insert.excluded.taken_at,
                "org_id": insert.excluded.org_id}))


def _previous_findings(db, org_ids: List[UUID]) -> Dict[Tuple[UUID, str, str], Tuple[Dict[str, Any], Optional[int]]]:
    """The findings of the LAST SUCCESSFUL check of each centre (a night the centre could not be checked says nothing about it): ``{(org, invariant, key): (details, actual)}``."""
    if not org_ids:
        return {}
    last = (db.query(MoneyReconcileCentre.org_id, MoneyReconcileCentre.run_id)
            .join(MoneyReconcileRun, MoneyReconcileRun.id == MoneyReconcileCentre.run_id)
            .filter(MoneyReconcileCentre.status == "ok", MoneyReconcileCentre.org_id.in_(org_ids))
            .ext(distinct_on(MoneyReconcileCentre.org_id)).order_by(MoneyReconcileCentre.org_id, MoneyReconcileRun.started_at.desc()).all())
    if not last:
        return {}
    return {(o, i, k): (d or {}, a) for o, i, k, d, a in db.query(MoneyReconcileFinding.org_id, MoneyReconcileFinding.invariant, MoneyReconcileFinding.subject_key, MoneyReconcileFinding.details,
                                                                    MoneyReconcileFinding.actual).filter(tuple_(MoneyReconcileFinding.org_id, MoneyReconcileFinding.run_id).in_([(org, rid) for org, rid in last])).all()}


def _restated(f: Found, previous: Dict[Tuple[UUID, str, str], Tuple[Dict[str, Any], Optional[int]]]) -> bool:
    """A row that already had this finding last night but is NOT the same row any more (its fingerprint differs from the one the last finding recorded): the second edit under the same key is a new fact."""
    before = previous.get((f.org_id, f.invariant, f.subject_key))
    return before is not None and f.invariant == "I-5" and "state" in before[0] and before[0]["state"] != f.details.get("state")


def _is_new(f: Found, previous: Dict[Tuple[UUID, str, str], Tuple[Dict[str, Any], Optional[int]]]) -> bool:
    return (f.org_id, f.invariant, f.subject_key) not in previous or _restated(f, previous)


def _mark_restated(f: Found, previous: Dict[Tuple[UUID, str, str], Tuple[Dict[str, Any], Optional[int]]]) -> None:
    """Say in the details of the finding what it was in the last one: «было в прошлой находке / стало»."""
    if _restated(f, previous):
        before_details, before_actual = previous[(f.org_id, f.invariant, f.subject_key)]
        f.details["restated"] = {"was": before_details["state"], "wasAmount": before_actual, "now": f.details.get("state"), "nowAmount": f.actual}


def _mail(findings: List[Found], new_count: int, failed: List[str], run_id: Any, why: Optional[Dict[str, str]] = None) -> str:
    """One letter to the team when there is something NEW (a finding that stood last night too is in the console, not in the mailbox again) or a centre could not be checked.  ``skipped``: nothing
    to say.  Counts, ids and sums only."""
    if not (findings and new_count) and not failed:
        return "skipped"
    from src.mail import letters, sender

    try:
        if findings and new_count:
            def line(f: Found) -> Tuple[str, str]:
                return (f"{f.invariant} · центр {str(f.org_id)[:8]}", f"{INVARIANT_TEXT[f.invariant]}; {f.subject_key}" + (f"; {f.month}" if f.month else "") +
                        (f"; ожидалось {f.expected}, есть {f.actual}" if f.expected is not None or f.actual is not None else ""))

            def row_line(f: Found) -> Tuple[str, str]:          # I-5: the kind of the record, its id, the centre and the sum «было → стало» (the sums of the payroll too - the letter is for the team of the platform)
                was = f"{f.expected}" if f.expected is not None else "—"
                now_ = f"{f.actual}" if f.actual is not None else "—"
                again = f.details.get("restated")
                tail = f"; правка повторная: было в прошлой находке {again['wasAmount'] if again['wasAmount'] is not None else '—'} → стало {again['nowAmount'] if again['nowAmount'] is not None else '—'}" if again else ""
                return (f"I-5 · центр {str(f.org_id)[:8]} · {f.details.get('kind')}", f"{f.details.get('what')}; запись {f.subject_id}; сумма было {was} → стало {now_}{tail}")

            rows = [line(f) for f in findings if f.invariant != "I-5"][:20]
            i5 = [row_line(f) for f in findings if f.invariant == "I-5"][:20]
            if i5:
                rows.append(("Построчная сверка записей денег (I-5)", f"находок: {sum(1 for f in findings if f.invariant == 'I-5')}; ниже — вид записи, центр, id, сумма до и после"))
                rows.extend(i5)
            if failed:
                rows.append(("Не проверено центров", f"{len(failed)}: " + "; ".join(_centre_why(x, why) for x in failed[:10])))
            letter = letters.render_money_reconcile(total=len(findings), new=new_count, rows=rows, run_id=str(run_id))
        else:
            letter = letters.render_money_reconcile_failed(failed=len(failed), centres=[_centre_why(x, why) for x in failed[:20]], run_id=str(run_id))
        ok = sender.send(_team_address(), letter)
    except Exception:
        logger.exception("money reconcile letter failed")
        ok = False
    return "sent" if ok else "failed"
