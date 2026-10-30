"""ONE rule for a password a person sets in the cabinets (K2-2, F; LESSONS 38): the invitation, the link, the reset, the settings.

At least 8 characters, at most 128 characters and 72 bytes (bcrypt reads no more than 72 bytes: a longer password would be cut silently, so it is
refused out loud), no NUL byte, not equal to the e-mail of the account. The console has its own, stricter rule (12) in ``platform_auth``.
"""
from __future__ import annotations

from typing import Optional

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
MAX_PASSWORD_BYTES = 72
HINT = f"Минимум {MIN_PASSWORD_LENGTH} символов"
BYTES_TEXT = "Пароль слишком длинный: не более 72 байт (латиница — 72 символа, кириллица — 36)"
NUL_TEXT = "Пароль содержит недопустимый символ"
SAME_AS_EMAIL_TEXT = "Пароль не должен совпадать с почтой"


def problem(password: object, *, email: str = "", min_length: int = MIN_PASSWORD_LENGTH) -> Optional[str]:
    """None when the password is acceptable, else the Russian reason."""
    if not isinstance(password, str) or not password:
        return "Укажите пароль"
    if "\x00" in password:
        return NUL_TEXT
    if len(password) < min_length:
        return f"Пароль не короче {min_length} символов"
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Пароль не длиннее {MAX_PASSWORD_LENGTH} символов"
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return BYTES_TEXT
    if email and password.strip().lower() == email.strip().lower():
        return SAME_AS_EMAIL_TEXT
    return None
