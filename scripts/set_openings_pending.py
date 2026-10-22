#!/usr/bin/env python3
"""OPENING-BULK release script: start the accounting of the centres on 01.11.2026 and ask them for the opening balances.

    python scripts/set_openings_pending.py                 # DRY RUN (default): prints what would change, writes nothing
    python scripts/set_openings_pending.py --execute       # writes
    python scripts/set_openings_pending.py --start 2026-11-01
    python scripts/set_openings_pending.py --org <uuid> --org <uuid>    # a canary: only these centres

For every organisation WITHOUT a live money event (payment, write-off, refund, transfer) dated before the start: ``payments.accounting_start_date`` =
the start and ``payments.openings_state`` = ``pending`` (+ ``openings_pending_since``).  The centres that already have money before the start are NOT
touched (their money would lose its place in the account): the list is printed for the owner of ALGO.  A centre whose state is already ``pending`` /
``done`` / ``skipped`` is left alone.  This is the ONLY place that sets ``pending`` (the owner of a centre can only close the checklist).
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


def run(db, *, execute: bool, start: date = DEFAULT_START, today: date = None, only=None) -> Dict[str, List[Dict[str, Any]]]:
    """``{"changed": [...], "skipped_money": [...], "untouched": [...]}`` (a list of dicts with the id and the name of the organisation)."""
    from sqlalchemy.orm.attributes import flag_modified

    from src.auth import payment_status as ps
    from src.models.auth import Organization

    today = today or ps.business_today()
    report: Dict[str, List[Dict[str, Any]]] = {"changed": [], "skipped_money": [], "untouched": []}
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
        report["changed"].append({**info, "accounting_start_before": block.get("accounting_start_date")})
        if execute:
            block["accounting_start_date"] = start.isoformat()
            block["openings_state"] = "pending"
            block["openings_pending_since"] = today.isoformat()
            org.settings = {**(org.settings or {}), "payments": block}
            flag_modified(org, "settings")
    if execute:
        db.commit()
    else:
        db.rollback()
    return report


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
    mode = "EXECUTED" if args.execute else "DRY RUN (nothing written)"
    print(f"{mode}: accounting start {start.isoformat()}, openings_state=pending")
    print(f"will be set / set: {len(report['changed'])}")
    for o in report["changed"]:
        print(f"  + {o['org_id']}  {o['name']}  (start before: {o['accounting_start_before'] or '-'})")
    print(f"SKIPPED (money before the start; give this list to the owner): {len(report['skipped_money'])}")
    for o in report["skipped_money"]:
        print(f"  ! {o['org_id']}  {o['name']}  first {o['kind']} on {o['earliest_money_event']}")
    print(f"already in a state (untouched): {len(report['untouched'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
