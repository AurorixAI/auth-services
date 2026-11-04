"""/api/v1/platform/money-reconcile: the nightly reconciliation of the money for the console (Money-1 A7; right ``audit.view``).

A centre never reads it.  The answer has ids, months and sums, no names; the console links a centre by its id."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.models.auth import MoneyReconcileFinding, MoneyReconcileRun, Organization

from .money_reconcile import INVARIANT_TEXT
from .platform_security import Principal, require_platform

router = APIRouter(prefix="/money-reconcile")


def _run_out(run: MoneyReconcileRun) -> dict:
    return {"id": str(run.id), "startedAt": run.started_at.isoformat() if run.started_at else None, "finishedAt": run.finished_at.isoformat() if run.finished_at else None,
            "status": run.status, "centresChecked": run.centres_checked, "findings": run.findings_count, "newFindings": run.new_findings_count, "mail": run.mail_status, "error": run.error}


@router.get("")
def latest(p: Principal = Depends(require_platform("audit.view")), db: Session = Depends(get_db), limit: int = Query(30, ge=1, le=100)):
    """The last runs (newest first) and the findings of the newest one, grouped by invariant."""
    runs = db.query(MoneyReconcileRun).order_by(MoneyReconcileRun.started_at.desc()).limit(limit).all()
    findings = []
    if runs:
        rows = db.query(MoneyReconcileFinding, Organization.name).join(Organization, Organization.id == MoneyReconcileFinding.org_id).filter(
            MoneyReconcileFinding.run_id == runs[0].id).order_by(MoneyReconcileFinding.invariant, MoneyReconcileFinding.org_id, MoneyReconcileFinding.subject_key).all()
        findings = [{"id": str(f.id), "orgId": str(f.org_id), "orgName": name, "invariant": f.invariant, "what": INVARIANT_TEXT.get(f.invariant), "subjectKey": f.subject_key, "subjectId": f.subject_id,
                     "month": f.month, "expected": f.expected, "actual": f.actual, "details": f.details, "isNew": f.is_new} for f, name in rows]
    return {"runs": [_run_out(r) for r in runs], "latest": _run_out(runs[0]) if runs else None, "findings": findings}
