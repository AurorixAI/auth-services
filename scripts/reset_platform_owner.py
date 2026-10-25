#!/usr/bin/env python3
"""Reset the password of an existing platform owner and lift his sign-in lock (run by hand on the server):

    docker exec -it <auth-container> python -m scripts.reset_platform_owner

Asks for the e-mail and the new password (twice, hidden; at least 12 characters). Ends every console session of that owner, clears the
failure counts and locks of the address. Prints one confirmation line and never the password.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    import src.api.main_v2  # noqa: F401  (loads the application first: the console modules import from src.api, which imports them back)
    from src.auth.platform_bootstrap import OwnerError, reset_owner
    from src.core.database import db_session

    try:
        email = input("Почта владельца консоли: ").strip()
        password = getpass.getpass("Новый пароль (не короче 12 символов): ")
        again = getpass.getpass("Новый пароль ещё раз: ")
    except (EOFError, KeyboardInterrupt):
        print("\nОтменено", file=sys.stderr)
        return 1
    if password != again:
        print("Пароли не совпадают", file=sys.stderr)
        return 1
    try:
        with db_session() as db:
            reset_owner(db, email, password)
    except OwnerError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Пароль владельца консоли {email.strip().lower()} сброшен, блокировка входа снята")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
