#!/usr/bin/env python3
"""LESSONS 194 (a): the lists of «Замены» are read to the END on a COPY of a real database - every centre, every filter, pages of 50 - and only the failures are printed.

    DATABASE_URL=<a throwaway COPY> python -m scripts.lr_read_probe            # exit 1 on any failure

Per centre: «История» (status ``decided``, with and without ``backdated_only``), «Ждут решения» (``pending``) - with and without the right of money - by pages of 50 until ``total``;
``mine`` of every active teacher of the centre.  Counted: the pages, the rows; a failure is an exception, a lost / repeated row, a row of the history without its lesson date.
The reads of the list may re-validate stale requests (a write): run it on a copy only, never on a server (``scripts/lr_read.sh`` makes the copies).
The last line: ``centres=N rows=M failures=K``."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PAGE = 50
MAX_PAGES = 400
SHOWN = 15


def main() -> int:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.auth.lesson_substitutions import Substitutions
    from src.auth.models import OrganizationMember
    from src.auth.service import AuthService
    from src.core.database import engine

    failures, rows_seen, centres = [], 0, 0

    def fail(where: str, exc: BaseException) -> None:
        failures.append(f"{where}: {type(exc).__name__}: {str(exc)[:160]}")

    with Session(engine) as db:
        orgs = [o for (o,) in db.execute(select(OrganizationMember.org_id).distinct()).all()]
        for org in orgs:
            centres += 1
            teachers = [u for (u,) in db.execute(select(OrganizationMember.user_id).where(OrganizationMember.org_id == org, OrganizationMember.org_role == "teacher",
                                                                                          OrganizationMember.is_active.is_(True))).all()]
            for status, backdated_only, money in (("pending", False, True), ("pending", False, False), ("decided", False, True), ("decided", True, True), ("decided", False, False)):
                where = f"org={str(org)[:8]} status={status} backdated_only={backdated_only} money={money}"
                offset, total, seen = 0, None, []
                for _ in range(MAX_PAGES):
                    try:
                        svc = Substitutions(AuthService(db))
                        out = svc.list(org, status=status, group_id=None, teacher_id=None, date_from=None, date_to=None, urgent=None, search=None, limit=PAGE, offset=offset,
                                       backdated_only=backdated_only, money_right=money, actor_id=None, org_role="owner")
                    except Exception as exc:          # noqa: BLE001 - the probe names every failure
                        db.rollback()
                        fail(f"{where} offset={offset}", exc)
                        break
                    total = out["total"]
                    for it in out["items"]:
                        seen.append((it["source"], it["id"]))
                        if it["source"] == "audit" and not (it.get("lesson") or {}).get("date"):
                            failures.append(f"{where}: a row of the journal without the date of its lesson id={it['id']}")
                    offset += PAGE
                    if offset >= total:
                        break
                rows_seen += len(seen)
                if len(seen) != len(set(seen)):
                    failures.append(f"{where}: {len(seen) - len(set(seen))} repeated rows over the pages")
                if total is not None and len(seen) < total and not any(f.startswith(where) for f in failures):
                    failures.append(f"{where}: {len(seen)} rows of {total}")
            for teacher in teachers:
                for status in ("all", "pending"):
                    try:
                        out = Substitutions(AuthService(db)).mine(org, teacher, status)
                        rows_seen += len(out["items"]) + len(out["assignedToMe"])
                    except Exception as exc:          # noqa: BLE001
                        db.rollback()
                        fail(f"org={str(org)[:8]} mine teacher={str(teacher)[:8]} status={status}", exc)
    for line in failures[:SHOWN]:
        print("FAIL", line)
    if len(failures) > SHOWN:
        print(f"... and {len(failures) - SHOWN} more")
    print(f"centres={centres} rows={rows_seen} failures={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
