#!/usr/bin/env python3
"""K2-1 (A4): how many users carry the old platform role ``superadmin`` (they can no longer sign in to a cabinet). READ ONLY: one SELECT, no row is
changed, no e-mail address is printed (only counts and ids).

    DATABASE_URL=... python scripts/count_superadmins.py        # prints the number, the number of active ones and their ids; exit 0

Run it on the production and the stage database; a non-zero number says which accounts the owner should look at (they keep working in the console
only through their ``platform_staff`` row, if they have one).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from sqlalchemy import create_engine, text

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    engine = create_engine(url)
    with engine.connect() as c:
        c.execute(text("SET TRANSACTION READ ONLY"))
        rows = c.execute(
            text(
                "SELECT u.id, u.is_active, EXISTS (SELECT 1 FROM platform_staff s WHERE s.user_id = u.id) AS in_console "
                "FROM users u WHERE u.role = 'superadmin' ORDER BY u.created_at"
            )
        ).all()
    print(f"superadmin users: {len(rows)} (active: {sum(1 for r in rows if r.is_active)}, with a console account: {sum(1 for r in rows if r.in_console)})")
    for r in rows:
        print(f"  {r.id} active={r.is_active} console={r.in_console}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
