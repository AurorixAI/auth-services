"""
ALGO - cheques attached to payments (N13 P2.2).

A photo / scan of the cheque (JPEG, PNG or PDF, up to 2 MB, up to 3 per payment) is kept in the database
next to the payment, exactly like the certificates of attendance excuses: the type comes from the first
bytes (the client's name and type are ignored), the name is a display name only.  The endpoints live in
``payments_router.py``, the writes in ``PaymentService``; this module holds the limits, the pure helpers
and the one-query loader used by every response that carries ``PaymentOut.files``.

Privacy: logs carry ids only - never file content or names.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional
from urllib.parse import quote
from uuid import UUID

from sqlalchemy.orm import Session

from src.models.auth import StudentPaymentFile, User

from .attendance_excuses import MAX_FILE_BYTES, safe_filename, sniff_content_type
from .payments_schemas import PaymentFileOut

__all__ = [
    "MAX_FILE_BYTES",
    "MAX_FILES_PER_PAYMENT",
    "MAX_BODY_BYTES",
    "DEFAULT_FILE_NAME",
    "sniff_content_type",
    "stored_filename",
    "content_disposition",
    "file_headers",
    "load_files",
]

MAX_FILES_PER_PAYMENT = 3
# The request body is multipart: file + framing.  A declared length above this is refused before a
# single byte of the body is read.
MAX_BODY_BYTES = MAX_FILE_BYTES + 64 * 1024
DEFAULT_FILE_NAME = "check"


def stored_filename(raw: Optional[str], content_type: str) -> str:
    """The name kept in the database: no path, no separators, no control characters, right extension."""
    return safe_filename(raw, content_type, default=DEFAULT_FILE_NAME)


def content_disposition(name: Optional[str]) -> str:
    """``inline`` header for a stored name: ASCII fallback plus an RFC 5987 UTF-8 name.

    The stored name has no control characters, quotes or backslashes (``stored_filename``); the fallback is
    still reduced to printable ASCII without ``"`` / ``\\`` / ``;`` here, so a name that came from somewhere
    else can never break out of the header.
    """
    name = name or DEFAULT_FILE_NAME
    ascii_name = "".join(
        ch for ch in name.encode("ascii", "ignore").decode() if 0x20 <= ord(ch) < 0x7F and ch not in '"\\;'
    ).strip()
    return f"inline; filename=\"{ascii_name or DEFAULT_FILE_NAME}\"; filename*=UTF-8''{quote(name, safe='')}"


def file_headers(name: Optional[str]) -> Dict[str, str]:
    """Response headers for serving a cheque: it can neither be sniffed into HTML nor run anything."""
    return {
        "Content-Disposition": content_disposition(name),
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
    }


def person_name(last: Optional[str], first: Optional[str], patronymic: Optional[str] = None) -> Optional[str]:
    """«Фамилия Имя [Отчество]» - the same order as ``User.full_name`` / ``Student.full_name``; None when empty."""
    return " ".join(part.strip() for part in (last, first, patronymic) if part and part.strip()) or None


def load_files(db: Session, payment_ids: Iterable[UUID]) -> Dict[UUID, List[PaymentFileOut]]:
    """``{payment_id: [files, oldest first]}`` in ONE query, without the bytes (no N+1, no ``data``)."""
    ids = {i for i in payment_ids if i}
    if not ids:
        return {}
    rows = (
        db.query(
            StudentPaymentFile.id,
            StudentPaymentFile.payment_id,
            StudentPaymentFile.filename,
            StudentPaymentFile.content_type,
            StudentPaymentFile.size_bytes,
            StudentPaymentFile.uploaded_at,
            User.last_name,
            User.first_name,
            User.patronymic,
        )
        .outerjoin(User, User.id == StudentPaymentFile.uploaded_by)
        .filter(StudentPaymentFile.payment_id.in_(ids))
        .order_by(StudentPaymentFile.uploaded_at, StudentPaymentFile.id)
        .all()
    )
    out: Dict[UUID, List[PaymentFileOut]] = {}
    for row in rows:
        out.setdefault(row.payment_id, []).append(
            PaymentFileOut(
                id=str(row.id),
                filename=row.filename,
                content_type=row.content_type,
                size_bytes=row.size_bytes,
                uploaded_at=row.uploaded_at.isoformat() if row.uploaded_at else None,
                uploaded_by_name=person_name(row.last_name, row.first_name, row.patronymic),
            )
        )
    return out
