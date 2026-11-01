#!/usr/bin/env python3
"""Drop the per-worker copies (<name>_gw0, <name>_gw1 ...) of the test databases that ``scripts/test_fast.sh`` made on a server it did not start itself.

    drop_worker_databases.py URL [URL ...]      the base URLs (.../a2_auth, .../a5_auth_migrations); only names ``<base>_gw<digits>`` are touched
"""
import re
import sys
from urllib.parse import urlsplit, urlunsplit

import psycopg2


def main(urls):
    for url in urls:
        parts = urlsplit(url)
        base = parts.path.lstrip("/")
        if not re.fullmatch(r"(a2_auth|a5_auth_migrations)", base):
            print(f"skip {base}: not a test database", file=sys.stderr)
            continue
        conn = psycopg2.connect(urlunsplit(parts._replace(scheme="postgresql", path="/postgres")))
        conn.autocommit = True
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT datname FROM pg_database WHERE datname ~ %s", (f"^{base}_gw[0-9]+$",))
                for (name,) in cursor.fetchall():
                    cursor.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                    print(f"dropped {name}")
        finally:
            conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
