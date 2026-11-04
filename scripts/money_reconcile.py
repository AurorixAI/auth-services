#!/usr/bin/env python3
"""Money-1 A7: the nightly reconciliation of the invariants I-1 .. I-4 of the money (``src/auth/money_reconcile.py``).

    python -m scripts.money_reconcile                 # DRY RUN (the default): checks every active centre, prints counts and ids, writes and sends NOTHING
    python -m scripts.money_reconcile --apply         # stores the run (the console section «Сверка» reads it)
    python -m scripts.money_reconcile --apply --mail  # ... and writes to the team when a finding is NEW (address: MONEY_RECONCILE_EMAIL, else the support mailbox of the mail settings)

Prints only counters and ids / months / sums - never a name of a person and never an address.  Exit code 1: a centre could not be checked (or the database was not reachable), 2: there are
findings, 3: another run is going (so that a scheduler can alarm).  It is NOT scheduled by the code.

THE TIME OF THE SCHEDULE IS THE TIME OF THE SERVER, not UTC and not Tashkent by itself: cron reads the time zone of the machine (``timedatectl``).  The night run at 02:10 Tashkent time is
    10 2 * * *    python -m scripts.money_reconcile --apply      (the server is in Asia/Tashkent)
    10 21 * * *   python -m scripts.money_reconcile --apply      (the server is in UTC: 21:10 UTC = 02:10 Tashkent)
The letter (``--mail``) is switched on only AFTER at least 3 nights of the dry run (no ``--apply``) on stage with 0 findings: until then the console is the only reader.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="store the run (default: a dry run)")
    parser.add_argument("--mail", action="store_true", help="with --apply: write to the team when there is a new finding")
    parser.add_argument("--org", action="append", default=None, help="check only this centre (its id); can be repeated")
    args = parser.parse_args(argv)

    from sqlalchemy.orm import Session

    from src.auth import money_reconcile
    from src.core.database import engine

    try:
        with Session(engine) as db:
            result = money_reconcile.run(db, apply=args.apply, mail=args.mail and args.apply, only=args.org)
    except Exception as exc:         # the database itself is not reachable: no run can be written, the exit code and stderr are the only signal (the scheduler mails stderr)
        print(f"FAILED: the reconciliation could not run: {type(exc).__name__}", file=sys.stderr)
        return 1
    if result.get("busy"):
        print("busy: another run of the reconciliation is going")
        return 3
    print(f"mode={'apply' if args.apply else 'dry-run'} centres={result['centres']} failed={len(result['failed'])} findings={result['findings']} new={result['new']} mail={result['mail']}")
    print("by invariant: " + " ".join(f"{k}={v}" for k, v in result["byInvariant"].items()))
    for item in result["items"][:200]:
        print(f"  org={item['org']} {item['invariant']} key={item['key']} month={item['month']} expected={item['expected']} actual={item['actual']}")
    if result["failed"]:
        print("failed centres: " + ", ".join(result["failed"]))
        return 1
    return 2 if result["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
