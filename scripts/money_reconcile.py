#!/usr/bin/env python3
"""Money-1 A7: the nightly reconciliation of the invariants I-1 .. I-4 of the money (``src/auth/money_reconcile.py``).

    python -m scripts.money_reconcile                 # DRY RUN (the default): checks every active centre, prints counts and ids, writes and sends NOTHING
    python -m scripts.money_reconcile --apply         # stores the run (the console section «Сверка» reads it)
    python -m scripts.money_reconcile --apply --mail  # ... and writes to the team when a finding is NEW (address: MONEY_RECONCILE_EMAIL, else the support mailbox of the mail settings)
    python -m scripts.money_reconcile --accept KIND:ID:CODE --reason "text"   # the team accepts ONE finding of I-5: the baseline of that row is taken again (the journal of the centre gets the reason)
    python -m scripts.money_reconcile --rebaseline --reason "text" [--org ID]  # after a migration of data / a restore: the baseline of the money rows is taken again (rows and reason are printed)

The FIRST night after the release takes the baseline of the money rows (I-5) without findings and says so in the summary line (``first_centres=N``).  A row with a finding keeps its old baseline and the
finding repeats every night until it is accepted.

Prints only counters and ids / months / sums - never a name of a person and never an address.  Exit code 1: a centre could not be checked (or the database was not reachable), 2: there are
findings, 3: another run is going (so that a scheduler can alarm).  The beginning of the summary line (``mode= centres= failed= findings= new= mail=``) is read by the scripts of the release; what the night did not check
(``not_checked``) follows it.  It is NOT scheduled by the code.

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
    parser.add_argument("--accept", metavar="KEY", default=None, help="accept ONE finding of I-5 (kind:row id:code); needs --reason")
    parser.add_argument("--rebaseline", action="store_true", help="take the baseline of the money rows again (after a migration of data / a restore); needs --reason")
    parser.add_argument("--reason", default=None, help="why (for --accept and --rebaseline)")
    args = parser.parse_args(argv)

    from sqlalchemy.orm import Session

    from src.auth import money_reconcile
    from src.core.database import engine

    if args.accept or args.rebaseline:
        if not args.reason:
            print("--reason is required", file=sys.stderr)
            return 4
        try:
            with Session(engine) as db:
                done = money_reconcile.accept(db, args.accept, args.reason) if args.accept else money_reconcile.rebaseline(db, args.reason, only=args.org)
        except ValueError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 4
        except Exception as exc:
            print(f"FAILED: {type(exc).__name__}", file=sys.stderr)
            return 1
        if done.get("busy"):
            print("busy: another run of the reconciliation is going")
            return 3
        print(f"accepted key={done['key']} org={done['org']} old={done['oldFingerprint']} new={done['newFingerprint']}" if args.accept else f"rebaseline centres={done['centres']} rows={done['rows']} reason={args.reason!r}")
        return 0
    try:
        with Session(engine) as db:
            result = money_reconcile.run(db, apply=args.apply, mail=args.mail and args.apply, only=args.org)
    except Exception as exc:         # the database itself is not reachable: no run can be written, the exit code and stderr are the only signal (the scheduler mails stderr)
        print(f"FAILED: the reconciliation could not run: {type(exc).__name__}", file=sys.stderr)
        return 1
    if result.get("busy"):
        print("busy: another run of the reconciliation is going")
        return 3
    print(f"mode={'apply' if args.apply else 'dry-run'} centres={result['centres']} failed={len(result['failed'])} findings={result['findings']} new={result['new']} mail={result['mail']} "
          f"rows={result['rows']} baselined={result['baselined']} first_centres={result['firstCentres']} not_checked={sum(result['notChecked'].values())}")
    if result["notChecked"]:
        print("not checked: " + " ".join(f"{k}={v}" for k, v in result["notChecked"].items()))
    if result["firstCentres"]:
        print(f"baseline: the first night of {result['firstCentres']} centres - the money rows were taken as they are, no comparison")
    print("by invariant: " + " ".join(f"{k}={v}" for k, v in result["byInvariant"].items()))
    for item in result["items"][:200]:
        print(f"  org={item['org']} {item['invariant']} key={item['key']} month={item['month']} expected={item['expected']} actual={item['actual']}")
    if result["failed"]:
        print("failed centres: " + ", ".join(result["failed"]))
        for org_id in result["failed"]:
            print(f"  not checked: org={org_id} {result.get('failedWhy', {}).get(org_id, '')}")
        print(f"FAILED: {len(result['failed'])} centres could not be checked", file=sys.stderr)       # stderr: the scheduler mails it
        return 1
    return 2 if result["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
