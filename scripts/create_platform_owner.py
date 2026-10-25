#!/usr/bin/env python3
"""Create the first platform owner of the console (run by hand on the server):

    docker exec -it <auth-container> python -m scripts.create_platform_owner

Asks for the e-mail (the login), the name and the password (twice, hidden; at least 12 characters). The password is never printed, logged or
stored anywhere but as a hash. Prints only «Создан владелец консоли <почта>». An e-mail that already exists is refused.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    import src.api.main_v2  # noqa: F401  (loads the application first: the console modules import from src.api, which imports them back)
    from src.auth.platform_bootstrap import OwnerError, create_owner
    from src.core.database import db_session

    try:
        email = input("Почта (логин в консоль): ").strip()
        first = input("Имя [Владелец]: ").strip() or "Владелец"
        last = input("Фамилия [ALGO]: ").strip() or "ALGO"
        password = getpass.getpass("Пароль (не короче 12 символов): ")
        again = getpass.getpass("Пароль ещё раз: ")
    except (EOFError, KeyboardInterrupt):
        print("\nОтменено", file=sys.stderr)
        return 1
    if password != again:
        print("Пароли не совпадают", file=sys.stderr)
        return 1
    try:
        with db_session() as db:
            create_owner(db, email, password, first, last)
    except OwnerError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Создан владелец консоли {email.strip().lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
