#!/usr/bin/env python3
"""PAY-TAILS А6.9: «было -> станет» for the release (READ ONLY; counters, ids and sums only - see ``src/auth/pay_tails_report.py``).

    python -m scripts.pay_tails_before_after dump FILE [--org ID ...]   # a snapshot of every membership by the image that is installed
    python -m scripts.pay_tails_before_after diff BEFORE AFTER          # prints the summary line, the three lists and one line per changed membership

Order of the release check: ``dump`` with the image that runs on the server (-> before.json), ``dump`` with the NEW image on a copy of the same database (-> after.json), then ``diff``.
Every line of ``diff`` is explained in the report of the release (LESSONS 115, 133); the night reconciliation after the release is run with ``--apply`` and its whole summary line is quoted."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump")
    d.add_argument("file")
    d.add_argument("--org", action="append", default=None)
    f = sub.add_parser("diff")
    f.add_argument("before")
    f.add_argument("after")
    args = parser.parse_args(argv)

    from src.auth import pay_tails_report as report

    if args.cmd == "diff":
        before, after = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (args.before, args.after))
        print("\n".join(report.diff(before, after)))
        return 0
    from sqlalchemy.orm import Session

    from src.core.database import engine

    with Session(engine) as db:
        data = report.dump(db, args.org)
    report.save(args.file, data)
    print(f"memberships={len(data['memberships'])} refunds_over_limit={len(data['refunds_over_limit'])} crossing_archive={len(data['crossing_archive'])} file={args.file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
