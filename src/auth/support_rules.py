"""«Поддержка» (K4a): the rules that need neither the database nor the network - limits, texts, the cleaning of what the browser reports about itself.

The browser reports the page and the last failed calls; the server keeps only what carries no personal data:
* the page is a TEMPLATE of the path (every segment that is an id, a number, a code or a token becomes ``[id]``), never a query string or a fragment;
* an error is ``{status, method, route_template, at}`` - never the text or the body of an answer (a 409 names a group or a student);
* the browser is the ``User-Agent`` header cut to 200 characters.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, List, Optional
from urllib.parse import urlsplit

from .attendance_excuses import sniff_content_type as _sniff_any

# ── limits (the owner's decisions 31.10: K4_SPEC §3) ──────────────────────────────────────────────
TEXT_MIN, TEXT_MAX = 10, 2000
REPLY_MIN, REPLY_MAX = 1, 2000
STAFF_REPLY_MAX = 4000
TICKETS_PER_AUTHOR_DAY = 5
TICKETS_PER_ORG_DAY = 20
AUTHOR_REPLIES_PER_TICKET_DAY = 30
AUTHOR_REPLIES_PER_AUTHOR_DAY = 30  # LESSONS 72: the limit of the one who may attack is per PERSON (and per centre), not only per ticket
AUTHOR_REPLIES_PER_ORG_DAY = 100
MAX_ATTACHMENTS = 3
MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024
WAITING_AUTO_RESOLVE_DAYS = 14
REOPEN_DAYS = 14
# Two SEPARATE budgets (LESSONS 72): what a noisy author spends (the letters to the team) must not touch the letters «есть ответ» to the authors of other centres.
# Both are separate from the «Забыли пароль» budget (150).
TEAM_MAIL_PER_DAY = 30  # letters to the mailbox of the support, the whole platform
TEAM_MAIL_KEY = "rl:support_team_mail_day"
TEAM_MAIL_PER_TICKET_SECONDS = 3600  # one letter an hour about one ticket: the next reply of the author inside the hour sends none (the console shows it as new / in work anyway)
AUTHOR_MAIL_PER_DAY = 50  # letters «есть ответ» to the authors, the whole platform (only the team spends it)
AUTHOR_MAIL_KEY = "rl:support_author_mail_day"
AUTHOR_MAIL_PER_TICKET_SECONDS = 1800
MAX_IMAGE_SIDE = 8000
MAX_IMAGE_PIXELS = 40_000_000
PREVIEW_LENGTH = 300
PENDING_MAIL_SHOWN_FAILED_AFTER_SECONDS = 120  # a letter «pending» for longer than this was lost with the process: the console shows it as failed
DEDUP_REPLY_SECONDS = 30  # the same text from the same person in the same thread within this time is the double click, not a second reply

STATUS_WORDS = {"new": "Новое", "in_progress": "В работе", "waiting": "Ждём ответа", "resolved": "Решено"}
STAFF_SETTABLE = ("in_progress", "waiting", "resolved")

TOO_MANY_TEXT = "Слишком много обращений за сутки — напишите на support@algolab.me"
TOO_MANY_REPLIES_TEXT = "Слишком много ответов за сутки. Подождите или напишите на support@algolab.me"
FILE_TOO_BIG_TEXT = "Снимок слишком большой (макс. 2 МБ)."
FILE_TYPE_TEXT = "Нужен снимок в формате JPEG или PNG."
FILE_PIXELS_TEXT = "Изображение слишком большое."
FILE_BROKEN_TEXT = "Не удалось прочитать изображение."
TEXT_CHARS_TEXT = "Недопустимые символы в тексте"
REPLY_FIRST_TEXT = "Сначала ответьте автору."
FILE_LIMIT_TEXT = "К обращению можно приложить не больше 3 снимков."
NOT_FOUND_TEXT = "Обращение не найдено"

# ── the cleaning of the page and of the errors ─────────────────────────────────────────────────────
_STATIC_SEGMENT = re.compile(r"(?:[a-z][a-z-]{0,23}|v[0-9]{1,2})")  # the names of pages and of API routes: lower-case words with a hyphen (a long run of letters is a token, not a word: at most 24), the version «v1»; anything else may be an id or a code
# The segment after these is a one-time code or a token.  The SAME words as the front (src/lib/public-pages.ts) and the masks of nginx: change together (LESSONS 77; the tests
# tests/test_k4a_support_unit.py and algo-infrastructure/tests/test_k4a_token_masks.py compare the lists).
_TOKEN_PREFIXES = frozenset({
    "d", "e", "invite", "diagnostic", "set-password", "confirm-email", "password-links", "email-change", "by-code", "session-status", "support-login", "reset",
})
_MAX_SEGMENTS = 8
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
MAX_ERRORS = 5
ID = "[id]"


def page_template(value: Any) -> str:
    """``https://x/teacher/students/6f1c…?redirect=/a#b`` -> ``/teacher/students/[id]``. Nothing but a path of lower-case words and ``[id]`` comes out."""
    if not isinstance(value, str) or not value.strip():
        return ""
    raw = value.strip()[:2000]
    try:
        path = urlsplit(raw).path if "://" in raw[:12] or raw.startswith("//") else raw.split("?", 1)[0].split("#", 1)[0]
    except ValueError:
        return ""
    segments = [s for s in path.split("/") if s]
    out: List[str] = []
    after_token_prefix = False
    for segment in segments[:_MAX_SEGMENTS]:
        keep = _STATIC_SEGMENT.fullmatch(segment) is not None and not after_token_prefix
        out.append(segment if keep else ID)
        after_token_prefix = keep and segment in _TOKEN_PREFIXES
    return "/" + "/".join(out) if out else "/"


def _moment(value: Any) -> Optional[str]:
    """An ISO moment (``Z``) or an epoch in milliseconds -> ``2026-10-31T09:30:00Z``; else None."""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
        elif isinstance(value, str) and value.strip():
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        else:
            return None
        return dt.astimezone(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"
    except (ValueError, OverflowError, OSError):
        return None


def client_errors(value: Any) -> List[dict]:
    """The last failed calls the browser reports: at most 5, each ``{status, method, route_template, at}``; every other key (a text, a body, a header) is dropped."""
    if not isinstance(value, list):
        return []
    out: List[dict] = []
    for item in value[-MAX_ERRORS:]:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        method = str(item.get("method") or "").upper()
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599 or method not in _METHODS:
            continue
        out.append({"status": status, "method": method, "route_template": page_template(item.get("route_template")), "at": _moment(item.get("at"))})
    return out


def browser_of(user_agent: Optional[str]) -> str:
    return "".join(ch for ch in (user_agent or "") if ord(ch) >= 32 and ord(ch) != 127)[:200]


# ── the screenshots ────────────────────────────────────────────────────────────────────────────────
def sniff_image(data: bytes) -> Optional[str]:
    """JPEG / PNG by the first bytes (the name and the type the client gave are ignored); a PDF or anything else is None here."""
    kind = _sniff_any(data)
    return kind if kind in ("image/jpeg", "image/png") else None


def image_size(data: bytes, kind: str) -> Optional[tuple]:
    """(width, height) from the header of a PNG (IHDR) or a JPEG (the first SOFn marker), without decoding a pixel; None when the header cannot be read."""
    import struct

    try:
        if kind == "image/png":
            if data[12:16] != b"IHDR":
                return None
            return struct.unpack(">II", data[16:24])
        i, n = 2, len(data)
        while i + 4 <= n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker == 0xFF:
                i += 1
                continue
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[i + 5:i + 9])
                return width, height
            if marker == 0xDA:
                return None
            i += 2 + length
    except (struct.error, IndexError):
        return None
    return None


def download_headers(content_type: str, number: int, index: int) -> dict:
    """A screenshot is served to its author and to the team only, as a download that can neither be sniffed into HTML nor run anything."""
    ext = "png" if content_type == "image/png" else "jpg"
    return {
        "Content-Disposition": f'attachment; filename="support-{number}-{index}.{ext}"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
        "Content-Security-Policy": "sandbox",
    }
