"""PAY-TAILS А6.9: the release check «было -> станет» (READ ONLY, nothing is written).

The calculation of the money changed (a forgiveness is not money; an archive of a group does not change a closed stay).  Run ``dump`` with the image that is on the server, then with the NEW image on a copy
of the same database (``scripts/pay_tails_before_after.py dump FILE``) and compare the two files (``scripts/pay_tails_before_after.py diff BEFORE AFTER``): every membership whose ``{charged, debt, advance,
refundable, written_off, covered_through, lessons_left, transferred_out}`` changed is a line «было -> станет».

Order of the release (А7.9): these two files (``src/auth/pay_tails_report.py``, ``scripts/pay_tails_before_after.py``) do not exist in the image that runs on the server: put them into the container of the
OLD image (they only read; ``written_off_granted`` is read with ``getattr``), make ``before.json`` there, then ``after.json`` with the NEW image on a copy of the database, then ``diff``.

Four more lists are made by the NEW image alone (they are the cases that the rule changes in a way a person must look at):
* ``refunds_over_limit`` - a refund that is larger than the limit of its own day under the new rule (it was allowed because a forgiveness stood as an advance);
* ``om_pause_forgiven`` - a membership of the common month with a live pause and a forgiveness (the account that the fix of the capacity per credit, А7.1, may change);
* ``crossing_archive`` - a stay of a membership that overlaps an archive interval of its group (the denominator of a closed stay no longer depends on it; expected: none);
* the transfers that carried an «advance of a forgiveness» are the memberships whose ``transferred_out`` changed (the carried sum is derived, never stored); the receiving membership shows it in ``transferred_in``.

Only counters, ids and sums - no names."""
from __future__ import annotations

import json
from datetime import date
from typing import Any, Dict, Iterable, List, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.models.auth import Group, Organization, StudentGroup

from . import billing_ledger as bl
from . import business_clock as bc
from . import ledger_service as ls
from . import payment_status as ps
from . import schedule_rules

FIELDS = ("charged", "debt", "advance", "refundable", "written_off", "covered_through", "lessons_left", "transferred_out", "transferred_in")
CHUNK = 150


def _row(ledger) -> Dict[str, Any]:
    return {
        "charged": int(ledger.charged), "debt": int(ledger.debt), "advance": int(ledger.advance), "refundable": int(ledger.refundable), "written_off": int(ledger.written_off),
        "written_off_granted": int(getattr(ledger, "written_off_granted", ledger.written_off)),
        "covered_through": ledger.covered_through.isoformat() if ledger.covered_through else None,
        "lessons_left": ledger.lessons.left if ledger.lessons else None, "transferred_out": int(ledger.transferred_out), "transferred_in": int(ledger.transferred_in)}


def _crossing(entry) -> List[str]:
    """The stays of the membership that overlap an archive interval of its group (business days)."""
    intervals = schedule_rules.parse_intervals(entry.group)
    if not intervals:
        return []
    out = []
    for stay in entry.inp.stays:
        first, last = stay.first, stay.last or date.max
        for lo, hi in intervals:
            lo_day = bc.utc_to_business_date(lo)
            hi_day = bc.utc_to_business_date(hi) if hi else date.max
            if first <= hi_day and lo_day <= last:
                out.append(f"{first.isoformat()}..{(stay.last.isoformat() if stay.last else '')}")
                break
    return out


def _om_pause_forgiven(entry) -> Optional[Dict[str, Any]]:
    """A membership of the common month (billing day) with a pause and a forgiveness: the ids and the sums only."""
    inp = entry.inp
    if inp is None or not inp.pauses or not any(r.anchor == ps.FIXED_DAY for r in inp.regimes):
        return None
    granted = int(getattr(entry.ledger, "written_off_granted", entry.ledger.written_off))
    return {"membership": str(entry.membership.id), "pauses": len(inp.pauses), "granted": granted} if granted > 0 else None


def _refunds_over_limit(loader, entry) -> List[Dict[str, Any]]:
    mid = entry.membership.id
    found = []
    mine = list(loader.refunds.get(mid, []))
    for refund in mine:
        loader.refunds[mid] = [x for x in mine if x.id != refund.id]
        loader._x_memo.clear()
        try:
            inp = loader.build_input(mid)
            limit = bl.refund_limit(inp, refund.effective_on).amount if inp is not None else 0
        finally:
            loader.refunds[mid] = mine
            loader._x_memo.clear()
        if int(refund.amount) > limit:
            found.append({"membership": str(mid), "refund": str(refund.id), "receipt": f"В-{refund.receipt_no}" if refund.receipt_no else None, "amount": int(refund.amount), "limit": int(limit),
                          "on": refund.effective_on.isoformat()})
    return found


def dump(db: Session, org_ids: Optional[Iterable[Any]] = None, today: Optional[date] = None) -> Dict[str, Any]:
    """One snapshot of every membership of the chosen centres (all active ones by default)."""
    db.execute(text("SET TRANSACTION READ ONLY"))
    today = today or ps.business_today()
    wanted = [UUID(str(o)) for o in org_ids] if org_ids else [o for (o,) in db.query(Organization.id).all()]
    out: Dict[str, Any] = {"as_of": today.isoformat(), "memberships": {}, "refunds_over_limit": [], "crossing_archive": [], "om_pause_forgiven": []}
    for org_id in wanted:
        ids = [m for (m,) in db.query(StudentGroup.id).join(Group, Group.id == StudentGroup.group_id).filter(Group.org_id == org_id).all()]
        for start in range(0, len(ids), CHUNK):
            loader = ls.LedgerLoader(db, str(org_id), today)
            for entry in loader.run(ids[start:start + CHUNK]):
                key = str(entry.membership.id)
                out["memberships"][key] = {"org": str(org_id), **_row(entry.ledger)}
                out["refunds_over_limit"].extend(_refunds_over_limit(loader, entry))
                paused = _om_pause_forgiven(entry)
                if paused:
                    out["om_pause_forgiven"].append(paused)
                crossing = _crossing(entry)
                if crossing:
                    out["crossing_archive"].append({"membership": key, "stays": crossing})
    db.rollback()
    return out


def diff(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    """The lines «было -> станет» of every membership that changed (ids and sums only), then the three lists."""
    lines: List[str] = []
    for key, new in sorted(after["memberships"].items()):
        old = before["memberships"].get(key)
        if old is None:
            lines.append(f"{key} NEW in the second file")
            continue
        changes = [f"{f}: {old.get(f)} -> {new.get(f)}" for f in FIELDS if old.get(f) != new.get(f)]
        if changes:
            lines.append(f"{key} org={new['org']} " + "; ".join(changes))
    lines.extend(f"{key} GONE from the second file" for key in sorted(set(before["memberships"]) - set(after["memberships"])))
    changed = len([ln for ln in lines if " -> " in ln])
    summary = [f"memberships={len(after['memberships'])} changed={changed} refunds_over_limit={len(after['refunds_over_limit'])} "
               f"transfers_changed={sum(1 for ln in lines if 'transferred_out:' in ln)} crossing_archive={len(after['crossing_archive'])} om_pause_forgiven={len(after.get('om_pause_forgiven', []))}"]
    summary.extend(f"REFUND OVER LIMIT membership={r['membership']} refund={r['refund']} receipt={r.get('receipt')} amount={r['amount']} limit={r['limit']} on={r['on']}" for r in after["refunds_over_limit"])
    summary.extend(f"OM PAUSE + FORGIVENESS membership={c['membership']} pauses={c['pauses']} granted={c['granted']}" for c in after.get("om_pause_forgiven", []))
    summary.extend(f"CROSSES ARCHIVE membership={c['membership']} stays={','.join(c['stays'])}" for c in after["crossing_archive"])
    return summary + lines


def save(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, sort_keys=True, indent=1)
