#!/usr/bin/env python3
"""Money-1 V7 (PAY-F): which SAVED payroll sheets get another fresh total after the release.

Before V7 payroll did not count a lesson that the student took instead of a lesson cancelled by the centre (the deduction) as PAID - the teacher lost it.  After V7 he is paid.  For a sheet
that is saved and closed this means «saved N, fresh M» (the sheet says «изменилась» and the nightly reconciliation I-3 sees a drift) until the owner presses «Сохранить новую сумму».
Run it BEFORE the release on stage and prod, so that the first night does not surprise anybody.

    python -m scripts.payf_payroll_report                  # read only (the default and the only mode): every centre, the last 12 months
    python -m scripts.payf_payroll_report --dry-run        # the same (the script never writes; the flag is accepted so that a runbook can name it)
    python -m scripts.payf_payroll_report --org <id> --months 6

Prints one line per sheet whose fresh total changes:  центр · месяц · преподаватель · было → станет (сохранено N)
«было» = the fresh total with the OLD rule, «станет» = with the new one.  A centre without the setting «вычитать уроки, отменённые центром» is skipped (nothing changes there).
The transaction is READ ONLY.  Exit code 0 = nothing changes, 2 = some sheets change (not a failure), 1 = the database could not be read."""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="accepted for the runbook; the script is always read only")
    parser.add_argument("--org", action="append", default=None, help="only this centre (its id); can be repeated")
    parser.add_argument("--months", type=int, default=12, help="how many months back from the current one (default 12)")
    args = parser.parse_args(argv)

    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from src.auth import payment_status as ps
    from src.auth import payments_service
    from src.auth.payroll_service import PayrollService
    from src.core.database import engine
    from src.models.auth import Organization, PayrollSheet, User

    today = ps.business_today()
    since = date(today.year - (args.months // 12) - (1 if today.month - args.months % 12 < 1 else 0), (today.month - args.months % 12 - 1) % 12 + 1, 1)
    changed = checked = 0
    try:
        with Session(engine) as db:
            db.execute(text("SET TRANSACTION READ ONLY"))
            query = db.query(Organization)
            if args.org:
                query = query.filter(Organization.id.in_(args.org))
            for org in query.order_by(Organization.name):
                history = ((org.settings or {}).get("payments") or {}).get("recalc_history") or []
                if not any(v.get("on") for v in history if isinstance(v, dict)):
                    continue
                sheets = db.query(PayrollSheet).filter(PayrollSheet.org_id == org.id, PayrollSheet.period_month >= since).order_by(PayrollSheet.period_month, PayrollSheet.teacher_id).all()
                names = {u.id: f"{u.last_name or ''} {u.first_name or ''}".strip() for u in db.query(User).filter(User.id.in_({s.teacher_id for s in sheets}))} if sheets else {}
                service = PayrollService(db)
                for sheet in sheets:
                    checked += 1
                    payments_service.PAYROLL_COUNTS_DEDUCTION = False
                    try:
                        was = service._fresh_total(str(org.id), sheet)
                    finally:
                        payments_service.PAYROLL_COUNTS_DEDUCTION = True
                    now = service._fresh_total(str(org.id), sheet)
                    if was != now:
                        changed += 1
                        print(f"{org.name} · {sheet.period_month.strftime('%Y-%m')} · {names.get(sheet.teacher_id) or sheet.teacher_id} · было {was} → станет {now} (сохранено {int(sheet.total_amount)})")
            db.rollback()
    except Exception as exc:
        print(f"FAILED: the report could not run: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"sheets checked={checked} changed={changed} mode=read-only")
    return 2 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
