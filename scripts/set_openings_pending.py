#!/usr/bin/env python3
"""OPENING-BULK release script: start the accounting of the centres on 01.11.2026 and ask them for the opening balances.

    python scripts/set_openings_pending.py                 # DRY RUN (default): prints what would change, writes nothing
    python scripts/set_openings_pending.py --execute       # writes
    python scripts/set_openings_pending.py --start 2026-11-01
    python scripts/set_openings_pending.py --org <uuid> --org <uuid>    # a canary: only these centres

For every organisation WITHOUT a live money event (payment, write-off, refund, transfer) dated before the start: ``payments.accounting_start_date`` =
the start and ``payments.openings_state`` = ``pending`` (+ ``openings_pending_since``).  The centres that already have money before the start are NOT
touched (their money would lose its place in the account): the list is printed for the owner of ALGO.  A centre whose state is already ``pending`` /
``done`` / ``skipped`` is left alone.  Also left alone: a centre whose CURRENT accounting start (set or derived) is after the start, and a centre with
live openings whose debt is dated on / after the start (moving a start earlier could count a day twice).  The dry run prints, per centre, the current
and the new start and the impact (the same dry run as ``PUT /org/settings/payments?dry_run=true``: memberships affected, debt before / after) - Sep-Oct
charges without payments DISAPPEAR from the debt: the operator sees how much.  ``--execute`` writes exactly the centres listed as «will be set».  This is the ONLY place that sets ``pending`` (the owner of a centre can only close the checklist).
The script never connects anywhere else than ``DATABASE_URL``; run it against a real DB only on the owner's explicit go.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_START = date(2026, 11, 1)


def earliest_money_event(db, org_id) -> Any:
    """``(date, kind)`` of the earliest live money event of the organisation, or ``None``."""
    from sqlalchemy import func

    from src.auth import payment_status as ps
    from src.models.auth import MembershipTransfer, StudentPayment, StudentRefund, StudentWriteoff

    found = []
    q = db.query(func.min(StudentPayment.paid_on)).filter(
        StudentPayment.org_id == org_id, StudentPayment.voided_at.is_(None), StudentPayment.kind == "payment").scalar()
    if q is not None:
        found.append((q, "payment"))
    q = db.query(func.min(StudentWriteoff.effective_on)).filter(StudentWriteoff.org_id == org_id, StudentWriteoff.voided_at.is_(None)).scalar()
    if q is not None:
        found.append((q, "writeoff"))
    q = db.query(func.min(StudentRefund.effective_on)).filter(StudentRefund.org_id == org_id, StudentRefund.voided_at.is_(None)).scalar()
    if q is not None:
        found.append((q, "refund"))
    q = db.query(func.min(MembershipTransfer.at)).filter(MembershipTransfer.org_id == org_id, MembershipTransfer.voided_at.is_(None)).scalar()
    if q is not None:
        found.append((ps.business_date(q), "transfer"))
    return min(found) if found else None


def live_openings_dated_from(db, org_id, start: date) -> int:
    """How many LIVE openings of the centre carry a debt dated ``start`` or later (they would be counted twice with the charges from the start)."""
    from sqlalchemy import func

    from src.models.auth import MembershipOpeningBalance

    return int(db.query(func.count(MembershipOpeningBalance.id)).filter(
        MembershipOpeningBalance.org_id == org_id, MembershipOpeningBalance.voided_at.is_(None),
        MembershipOpeningBalance.debt_on.isnot(None), MembershipOpeningBalance.debt_on >= start).scalar() or 0)


def debt_now(db, org_id) -> Dict[str, int]:
    """The debt of the centre TODAY under its current accounting start: ``{"debt": total, "debtors": memberships with a debt}``."""
    from src.auth import ledger_service as ls
    from src.auth import payment_status as ps

    today = ps.business_today()
    loader = ls.LedgerLoader(db, str(org_id), today, ls.load_settings(db, str(org_id)))
    ledgers = [r.ledger for r in loader.run(loader.select(scope="all"))]
    return {"debt": sum(l.debt for l in ledgers), "debtors": sum(1 for l in ledgers if l.debt > 0)}


def impact_of_start(db, org_id, start: date) -> Dict[str, Any]:
    """What the move does, from the SAME dry run the settings endpoint uses (``PaymentService._accounting_start_impact``) + the debt before / after."""
    from src.auth.payments_service import PaymentService

    svc = PaymentService(db)
    impact = svc._accounting_start_impact(str(org_id), start)
    before = debt_now(db, org_id)
    return {
        "memberships_with_new_debt": impact.memberships_with_new_debt,
        "memberships_with_changed_advance": impact.memberships_with_changed_advance,
        "openings_to_review": impact.openings_to_review,
        "debt_before": before["debt"], "debt_after": before["debt"] + impact.debt_delta, "debt_delta": impact.debt_delta,
        "debtors_before": before["debtors"],
        "advance_delta": impact.advance_delta, "refundable_delta": impact.refundable_delta,
    }


def run(db, *, execute: bool, start: date = DEFAULT_START, today: date = None, only=None) -> Dict[str, List[Dict[str, Any]]]:
    """``{"changed", "skipped_money", "skipped_later_start", "skipped_openings", "untouched"}`` (lists of dicts with the id and the name of the organisation).

    A centre is SET only when: its state is not yet ``pending`` / ``done`` / ``skipped``; it has no live money event before the start; its CURRENT accounting
    start (explicit or derived from the creation day) is NOT after the start (moving a start EARLIER could count a day twice - the guard of
    ``PaymentService._check_openings_for_start``); and no live opening has a debt dated on / after the start.  Phase 1 only classifies (and computes the
    impact of each ``changed`` centre); phase 2 (``execute``) writes exactly the centres of ``changed``."""
    from sqlalchemy.orm.attributes import flag_modified

    from src.auth import payment_status as ps
    from src.auth.payments_service import PaymentService
    from src.models.auth import Organization

    today = today or ps.business_today()
    report: Dict[str, List[Dict[str, Any]]] = {"changed": [], "skipped_money": [], "skipped_later_start": [], "skipped_openings": [], "untouched": []}
    svc = PaymentService(db)
    for org in db.query(Organization).order_by(Organization.created_at, Organization.id).all():
        if only is not None and str(org.id) not in only:
            continue
        info = {"org_id": str(org.id), "name": org.name}
        block = dict((org.settings or {}).get("payments") or {})
        if block.get("openings_state") in ("pending", "done", "skipped"):
            report["untouched"].append({**info, "state": block["openings_state"]})
            continue
        event = earliest_money_event(db, org.id)
        if event is not None and event[0] < start:
            report["skipped_money"].append({**info, "earliest_money_event": event[0].isoformat(), "kind": event[1]})
            continue
        current = svc.get_settings(str(org.id)).accounting_start_date
        if current > start:
            report["skipped_later_start"].append({**info, "current_start": current.isoformat(), "explicit": bool(block.get("accounting_start_date"))})
            continue
        late = live_openings_dated_from(db, org.id, start)
        if late:
            report["skipped_openings"].append({**info, "openings_dated_from_start": late})
            continue
        report["changed"].append({
            **info, "accounting_start_before": block.get("accounting_start_date"), "current_start": current.isoformat(),
            "explicit": bool(block.get("accounting_start_date")), "new_start": start.isoformat(), "impact": impact_of_start(db, org.id, start)})
    if execute:
        for entry in report["changed"]:
            org = db.query(Organization).filter(Organization.id == entry["org_id"]).with_for_update().one()
            block = dict((org.settings or {}).get("payments") or {})
            if block.get("openings_state") in ("pending", "done", "skipped"):          # changed between the two phases: leave it
                continue
            block["accounting_start_date"] = start.isoformat()
            block["openings_state"] = "pending"
            block["openings_pending_since"] = today.isoformat()
            org.settings = {**(org.settings or {}), "payments": block}
            flag_modified(org, "settings")
        db.commit()
    else:
        db.rollback()
    return report


def _money(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def format_report(report: Dict[str, List[Dict[str, Any]]], *, start: date, execute: bool) -> List[str]:
    """The lines the operator reads (one place, so a test can read them too)."""
    mode = "EXECUTED" if execute else "DRY RUN (nothing written)"
    out = [f"{mode}: accounting start {start.isoformat()}, openings_state=pending", f"will be set / set: {len(report['changed'])}"]
    for o in report["changed"]:
        im = o["impact"]
        kind = "set" if o["explicit"] else "derived from the creation day"
        out.append(f"  + {o['org_id']}  {o['name']}")
        out.append(f"      start: {o['current_start']} ({kind}) -> {o['new_start']}")
        out.append(f"      impact: new debt in {im['memberships_with_new_debt']} memberships; advance changes in {im['memberships_with_changed_advance']}; "
                   f"openings to review {im['openings_to_review']}")
        out.append(f"      debt: {_money(im['debt_before'])} ({im['debtors_before']} debtors) -> {_money(im['debt_after'])} (delta {_money(im['debt_delta'])})")
    out.append(f"SKIPPED (money before the start; give this list to the owner): {len(report['skipped_money'])}")
    for o in report["skipped_money"]:
        out.append(f"  ! {o['org_id']}  {o['name']}  first {o['kind']} on {o['earliest_money_event']}")
    out.append(f"SKIPPED (the current accounting start is AFTER {start.isoformat()}; moving it earlier could count a day twice): {len(report['skipped_later_start'])}")
    for o in report["skipped_later_start"]:
        out.append(f"  ! {o['org_id']}  {o['name']}  start {o['current_start']} ({'set' if o['explicit'] else 'derived'})")
    out.append(f"SKIPPED (live openings with a debt dated on / after {start.isoformat()}): {len(report['skipped_openings'])}")
    for o in report["skipped_openings"]:
        out.append(f"  ! {o['org_id']}  {o['name']}  openings: {o['openings_dated_from_start']}")
    out.append(f"already in a state (untouched): {len(report['untouched'])}")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="write (the default is a dry run)")
    parser.add_argument("--org", action="append", default=None, help="only this organisation id (repeatable); default: all")
    parser.add_argument("--start", default=DEFAULT_START.isoformat(), help="the accounting start (default 2026-11-01)")
    args = parser.parse_args(argv)
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    from src.core.database import SessionLocal

    db = SessionLocal()
    try:
        report = run(db, execute=args.execute, start=start, only=set(args.org) if args.org else None)
    finally:
        db.close()
    print("\n".join(format_report(report, start=start, execute=args.execute)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
