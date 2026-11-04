"""Money-1 (A7): the nightly reconciliation of the invariants I-1 .. I-4 over all centres.

Pure functions (``check_*``) say whether ONE thing diverges; the loader (``reconcile_org``) reads a centre and calls them; ``run`` writes the result (``money_reconcile_runs`` /
``_findings`` / ``_snapshots``) and tells the team; ``scripts/money_reconcile.py`` is the command.  A centre never sees any of it (the platform console reads the tables).

* I-1 the account of a membership adds up: ``charged - paid - transferred_in - written_off - recalculated + refunded + transferred_out == debt - advance`` (``Ledger.reconciles``) - the one
  identity every screen, the Excel and the receipt are built on;
* I-2 "paid until" / the covered lessons / the gross payments of a membership did not move since the last night WITHOUT a record that explains it (a payment, a write-off, a refund, an opening balance, a pause,
  a price, a transfer, the stay of the student in the group created or cancelled since); the first night only takes the snapshot;
* I-3 a SAVED payroll sheet: its total is the sum of its lines, and no line pays a lesson that was not held (a counted mark whose lesson is cancelled or not completed);
* I-4 a CLOSED month of a teacher has no unresolved lesson (a lesson he holds that is still going on or still waits for a decision): nobody can resolve it without reopening the month.
  Known limit: a never-started lesson older than the list «Требует разбора» (the previous month and this one) is not in what the check can see cheaply - only the rows that exist.

What is printed / stored / mailed: counts, ids, months, sums - never a name of a person.  Nothing here writes to a centre's data; ``run`` writes only its own three tables.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import UUID

from sqlalchemy import func, or_

from src.models.auth import (
    GroupAttendance, GroupPrice, MembershipOpeningBalance, MembershipPause, MembershipPriceOverride, MembershipTransfer, MoneyReconcileFinding, MoneyReconcileRun,
    MoneyReconcileSnapshot, Organization, PayrollSheet, StudentGroup, StudentPayment, StudentRefund, StudentWriteoff,
)

from . import business_clock as bc
from . import payroll_engine as engine
from .closed_period import ClosedPeriods, month_first, month_last

logger = logging.getLogger(__name__)

INVARIANT_TEXT = {
    "I-1": "долг, начислено и оплачено не сходятся",
    "I-2": "«оплачено до» изменилось без записи в журнале",
    "I-3": "в сохранённой ведомости зарплата не сходится с уроками",
    "I-4": "в закрытом месяце остался неразобранный урок",
}
LOADER_CHUNK = 150


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


# ====================================================================================================== pure checks
def check_identity(org_id: UUID, membership_id: Any, ledger) -> Optional[Found]:
    """I-1: the identity of ``Ledger.reconciles``; ``expected`` = what the left side says, ``actual`` = debt - advance."""
    if ledger.reconciles:
        return None
    left = ledger.charged - ledger.paid - ledger.transferred_in - ledger.written_off - ledger.recalculated + ledger.refunded + ledger.transferred_out
    return Found(org_id, "I-1", f"membership:{membership_id}", str(membership_id), None, left, ledger.debt - ledger.advance, {"charged": ledger.charged, "paid": ledger.paid})


def check_paid_until(org_id: UUID, membership_id: Any, before: Optional[Tuple[Optional[date], Optional[int], Optional[int]]], now: Tuple[Optional[date], Optional[int], Optional[int]],
                     explained: bool) -> Optional[Found]:
    """I-2: ``before`` / ``now`` = ``(covered_through, lessons_covered, paid)`` (the gross payments move only with a record, too); a change without an explaining record is a finding.
    No snapshot yet (first night) -> nothing to compare."""
    if before is None or before == now or explained:
        return None
    return Found(org_id, "I-2", f"membership:{membership_id}", str(membership_id), None, None, None,
                 {"covered_through": [before[0].isoformat() if before[0] else None, now[0].isoformat() if now[0] else None], "lessons_covered": [before[1], now[1]], "paid": [before[2], now[2]]})


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


def check_unresolved(org_id: UUID, teacher_id: Any, month: str, unresolved: Sequence[str]) -> Optional[Found]:
    """I-4: a closed month of a teacher with lessons that nobody can resolve any more."""
    if not unresolved:
        return None
    return Found(org_id, "I-4", f"closed-open-lessons:{teacher_id}:{month}", str(teacher_id), month, 0, len(unresolved), {"lessons": sorted(unresolved)[:10]})


# ====================================================================================================== the loader
def _explaining_events(db, org_id: Any, since_by_membership: Dict[UUID, datetime]) -> set:
    """Memberships with a record created / cancelled after their snapshot: anything that can move "paid until" lawfully."""
    if not since_by_membership:
        return set()
    oldest = min(since_by_membership.values())
    ids = list(since_by_membership)
    seen: Dict[UUID, datetime] = {}

    def note(membership_id, moments):
        for moment in moments:
            if moment is not None and moment > since_by_membership.get(membership_id, oldest):
                seen[membership_id] = moment

    for model in (StudentPayment, StudentWriteoff, StudentRefund, MembershipOpeningBalance, MembershipPause):
        for membership_id, created, voided in (db.query(model.membership_id, model.created_at, model.voided_at)
                                               .filter(model.org_id == org_id, model.membership_id.in_(ids), or_(model.created_at > oldest, model.voided_at > oldest)).all()):
            note(membership_id, (created, voided))
    for membership_id, created in db.query(MembershipPriceOverride.membership_id, MembershipPriceOverride.created_at).filter(
            MembershipPriceOverride.membership_id.in_(ids), MembershipPriceOverride.created_at > oldest).all():
        note(membership_id, (created,))
    for src, dst, created, voided in db.query(MembershipTransfer.from_membership_id, MembershipTransfer.to_membership_id, MembershipTransfer.created_at, MembershipTransfer.voided_at).filter(
            MembershipTransfer.org_id == org_id, or_(MembershipTransfer.from_membership_id.in_(ids), MembershipTransfer.to_membership_id.in_(ids)),
            or_(MembershipTransfer.created_at > oldest, MembershipTransfer.voided_at > oldest)).all():
        note(src, (created, voided))
        note(dst, (created, voided))
    groups = {m: g for m, g in db.query(StudentGroup.id, StudentGroup.group_id).filter(StudentGroup.id.in_(ids)).all()}
    for group_id, created in db.query(GroupPrice.group_id, GroupPrice.created_at).filter(GroupPrice.group_id.in_(set(groups.values())), GroupPrice.created_at > oldest).all():
        for membership_id, g in groups.items():
            if g == group_id:
                note(membership_id, (created,))
    for membership_id, joined, left in db.query(StudentGroup.id, StudentGroup.joined_at, StudentGroup.left_at).filter(StudentGroup.id.in_(ids)).all():
        note(membership_id, (joined, left))
    return {m for m, moment in seen.items() if moment > since_by_membership[m]}


def reconcile_org(db, org_id: Any, today: Optional[date] = None, *, snapshots: Optional[List[Tuple[Any, Optional[date], Optional[int], Optional[int]]]] = None) -> List[Found]:
    """All four checks over one centre.  ``snapshots`` (when given) is filled with the new ``(membership_id, covered_through, lessons_covered)`` for ``run`` to store."""
    from .payments_service import PaymentService

    today = today or bc.business_today()
    org_uuid = org_id if isinstance(org_id, UUID) else UUID(str(org_id))
    found: List[Found] = []

    # ---- I-1 and I-2: the ledgers
    from src.models.auth import Group

    memberships = [m for (m,) in db.query(StudentGroup.id).join(Group, Group.id == StudentGroup.group_id).filter(Group.org_id == org_uuid).all()]
    old = {s.membership_id: s for s in db.query(MoneyReconcileSnapshot).filter(MoneyReconcileSnapshot.org_id == org_uuid).all()}
    since = {m: s.taken_at for m, s in old.items()}
    explained = _explaining_events(db, org_uuid, since)
    loader = PaymentService(db)._loader(str(org_uuid), recalc=False)
    for start in range(0, len(memberships), LOADER_CHUNK):
        chunk = memberships[start:start + LOADER_CHUNK]
        for entry in loader.run(chunk):
            ledger = entry.ledger
            if (item := check_identity(org_uuid, entry.membership.id, ledger)) is not None:
                found.append(item)
            now = (ledger.covered_through, ledger.lessons.covered if ledger.lessons is not None else None, int(ledger.paid))
            before = (old[entry.membership.id].covered_through, old[entry.membership.id].lessons_covered, old[entry.membership.id].paid) if entry.membership.id in old else None
            if (item := check_paid_until(org_uuid, entry.membership.id, before, now, entry.membership.id in explained)) is not None:
                found.append(item)
            if snapshots is not None:
                snapshots.append((entry.membership.id, now[0], now[1], now[2]))

    # ---- I-3: the saved sheets
    sheets = db.query(PayrollSheet).filter(PayrollSheet.org_id == org_uuid, PayrollSheet.period_month <= today).all()
    wanted: set = set()
    per_sheet: Dict[UUID, List[Tuple[str, str, str]]] = {}
    for sheet in sheets:
        month = sheet.period_month.strftime("%Y-%m")
        if (item := check_sheet_total(org_uuid, sheet.id, month, sheet.lines or [], sheet.total_amount)) is not None:
            found.append(item)
        per_sheet[sheet.id] = paid_marks(sheet.lines or [])
        wanted |= set(per_sheet[sheet.id])
    status_of: Dict[Tuple[str, str, str], str] = {}
    if wanted:
        dates = sorted({d for _, d, _ in wanted})
        groups = {UUID(g) for g, _, _ in wanted if g not in ("None", "")}
        for group_id, lesson_date, lesson_id, status in db.query(GroupAttendance.group_id, GroupAttendance.lesson_date, GroupAttendance.lesson_id, GroupAttendance.status).filter(
                GroupAttendance.org_id == org_uuid, GroupAttendance.group_id.in_(groups), GroupAttendance.lesson_date >= dates[0], GroupAttendance.lesson_date <= dates[-1]).all():
            status_of[(str(group_id), lesson_date, str(lesson_id))] = status
    for sheet in sheets:
        if (item := check_paid_lessons(org_uuid, sheet.id, sheet.period_month.strftime("%Y-%m"), per_sheet[sheet.id], status_of)) is not None:
            found.append(item)

    # ---- I-4: the closed months
    periods = ClosedPeriods.of(db, org_uuid)
    for first in sorted(periods.months_with_closed()):
        closed = periods._load()[first]
        rows = db.query(GroupAttendance.teacher_id, GroupAttendance.group_id, GroupAttendance.lesson_date).filter(
            GroupAttendance.org_id == org_uuid, GroupAttendance.status == "in_progress", GroupAttendance.lesson_date >= first.isoformat(),
            GroupAttendance.lesson_date <= month_last(first).isoformat(), GroupAttendance.teacher_id.in_(list(closed))).all()
        by_teacher: Dict[UUID, List[str]] = {}
        for teacher_id, group_id, lesson_date in rows:
            by_teacher.setdefault(teacher_id, []).append(f"{lesson_date}|{group_id}")
        for teacher_id, items in by_teacher.items():
            if (item := check_unresolved(org_uuid, teacher_id, first.strftime("%Y-%m"), items)) is not None:
                found.append(item)
    return found


# ====================================================================================================== the run
def _team_address() -> str:
    """The mailbox of the team: ``MONEY_RECONCILE_EMAIL`` of the environment, else the support mailbox of the mail settings.  Never printed, never stored."""
    from src.mail import config as mail_config

    return (os.getenv("MONEY_RECONCILE_EMAIL") or "").strip() or mail_config.load().support_email


def run(db, *, apply: bool = False, mail: bool = False, today: Optional[date] = None) -> Dict[str, Any]:
    """Check every active centre.  ``apply=False`` (the default) is a dry run: nothing is written, nothing is sent.  Returns the counters and the findings (ids only)."""
    started = bc.utc_now()
    orgs = [o for (o,) in db.query(Organization.id).filter(Organization.status == "active").order_by(Organization.id).all()]
    findings: List[Found] = []
    snapshots: Dict[UUID, List[Tuple[Any, Optional[date], Optional[int], Optional[int]]]] = {}
    failed: List[str] = []
    for org_id in orgs:
        try:
            snaps: List[Tuple[Any, Optional[date], Optional[int], Optional[int]]] = []
            findings.extend(reconcile_org(db, org_id, today, snapshots=snaps))
            snapshots[org_id] = snaps
        except Exception:
            logger.exception("money reconcile failed for org %s", org_id)
            db.rollback()
            failed.append(str(org_id))
    previous = _previous_keys(db) if apply else set()
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
    taken = bc.utc_now()
    for org_id, rows in snapshots.items():
        for membership_id, through, lessons, paid in rows:
            existing = db.get(MoneyReconcileSnapshot, membership_id)
            if existing is None:
                db.add(MoneyReconcileSnapshot(membership_id=membership_id, org_id=org_id, covered_through=through, lessons_covered=lessons, paid=paid, taken_at=taken))
            else:
                existing.covered_through, existing.lessons_covered, existing.paid, existing.taken_at = through, lessons, paid, taken
    if mail:
        run_row.mail_status = _mail(findings, new_count, run_row.id)
        out["mail"] = run_row.mail_status
    db.commit()
    out["runId"] = str(run_row.id)
    return out


def _previous_keys(db) -> set:
    last = db.query(MoneyReconcileRun.id).filter(MoneyReconcileRun.status.in_(("clean", "findings"))).order_by(MoneyReconcileRun.started_at.desc()).first()
    if last is None:
        return set()
    return {(o, i, k) for o, i, k in db.query(MoneyReconcileFinding.org_id, MoneyReconcileFinding.invariant, MoneyReconcileFinding.subject_key).filter(MoneyReconcileFinding.run_id == last[0]).all()}


def _mail(findings: List[Found], new_count: int, run_id: Any) -> str:
    """One letter to the team when there is something NEW (a finding that stood last night too is in the console, not in the mailbox again).  ``skipped``: nothing new / no findings."""
    if not findings or not new_count:
        return "skipped"
    from src.mail import letters, sender

    rows = [(f"{f.invariant} · центр {str(f.org_id)[:8]}", f"{INVARIANT_TEXT[f.invariant]}; {f.subject_key}" + (f"; {f.month}" if f.month else "") +
             (f"; ожидалось {f.expected}, есть {f.actual}" if f.expected is not None or f.actual is not None else "")) for f in findings[:20]]
    try:
        ok = sender.send(_team_address(), letters.render_money_reconcile(total=len(findings), new=new_count, rows=rows, run_id=str(run_id)))
    except Exception:
        logger.exception("money reconcile letter failed")
        ok = False
    return "sent" if ok else "failed"
