"""Text for SQL ``LIKE`` / ``ILIKE`` - no imports, so any module can take it. Use with ``escape="\\\\"``."""
from __future__ import annotations


def like_escape(value: str) -> str:
    """``%``, ``_`` and the backslash of the typed text become ordinary characters."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def contains(value: str) -> str:
    """The pattern «the text contains what was typed» (trimmed): ``column.ilike(contains(text), escape="\\\\")``."""
    return f"%{like_escape(value.strip())}%"
