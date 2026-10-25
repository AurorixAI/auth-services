"""«Запросить демо»: validation, normalisation and storage of a public request, and the helpers the console uses on it."""
from __future__ import annotations

import hashlib
import hmac
import re
from datetime import timedelta
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from src.core.config import get_settings
from src.models.platform import DemoRequest

from . import platform_audit as audit
from .platform_common import iso, utcnow
from .platform_validation import Form, strip_format_controls

REPEAT_WINDOW = timedelta(days=30)
SOURCE_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "referrer", "landing")
PHONE_TEXT = "Телефон: формат +998 XX XXX-XX-XX"
ACCEPTED_TEXT = "Заявка принята. Мы свяжемся с вами в течение 1 рабочего дня."


def normalise_phone(raw: str) -> Optional[str]:
    """``+998 90 123-45-67`` / ``(90) 123 45 67`` / ``998901234567`` -> ``+998901234567``; another country's number only with its ``+``."""
    text = (raw or "").strip()
    digits = re.sub(r"\D", "", text)
    if not digits or re.search(r"[^\d\s()+\-.]", text):
        return None
    if digits.startswith("998") and len(digits) == 12:
        return "+" + digits
    if len(digits) == 9 and not text.startswith("+"):
        return "+998" + digits
    if text.startswith("+") and 8 <= len(digits) <= 15 and not digits.startswith("998"):
        return "+" + digits
    return None


def clean_source(raw: Any) -> Dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in SOURCE_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = re.sub(r"[\x00-\x1f\x7f]", " ", strip_format_controls(value).strip())[:300]
    return out


def ip_digest(bucket: str) -> str:
    """HMAC of the client bucket: shows a flood from one address, cannot be turned back into the address."""
    key = get_settings().secret_key.encode("utf-8")
    return hmac.new(key, bucket.encode("utf-8"), hashlib.sha256).hexdigest()


def validate(body: Any) -> Tuple[Dict[str, Any], Form]:
    form = Form(body)
    data = {
        "contact_name": form.text("contact_name", "Имя", min_len=2, max_len=120),
        "centre_name": form.text("centre_name", "Название центра", min_len=2, max_len=200),
        "city": form.text("city", "Город", min_len=2, max_len=100),
        "email": form.email("email", "Почта"),
        "students_count": form.integer("students_count", "Количество учеников", minimum=0, maximum=100000),
        "comment": form.text("comment", "Комментарий", required=False, max_len=2000, multiline=True),
    }
    phone_raw = form.text("phone", "Телефон", max_len=40)
    data["phone"] = None
    if phone_raw is not None:
        data["phone"] = normalise_phone(phone_raw)
        if data["phone"] is None:
            form.fail("phone", PHONE_TEXT)
    return data, form


def find_previous(db: Session, email: str) -> Optional[DemoRequest]:
    since = utcnow() - REPEAT_WINDOW
    return (
        db.query(DemoRequest)
        .filter(DemoRequest.email == email, DemoRequest.created_at >= since)
        .order_by(DemoRequest.created_at.desc())
        .first()
    )


def create(db: Session, data: Dict[str, Any], source: Dict[str, str], bucket: str) -> DemoRequest:
    previous = find_previous(db, data["email"])
    row = DemoRequest(
        contact_name=data["contact_name"],
        centre_name=data["centre_name"],
        city=data["city"],
        phone=data["phone"],
        email=data["email"],
        students_count=data["students_count"],
        comment=data["comment"],
        source=source,
        ip_hash=ip_digest(bucket),
        status="new",
        is_repeat=previous is not None,
        repeat_of_id=previous.id if previous is not None else None,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(row)
    db.flush()
    audit.record(
        db, actor_id=None, action="demo_request.received", target_type="demo_request", target_id=row.id,
        details={"is_repeat": row.is_repeat, "centre_name": row.centre_name}, ip=None,
    )
    return row


def mail_data(row: DemoRequest) -> dict:
    """What the letters need, as plain values (safe to hand to a background task after the session is gone)."""
    return {
        "id": str(row.id),
        "contact_name": row.contact_name,
        "centre_name": row.centre_name,
        "city": row.city,
        "phone": row.phone,
        "email": row.email,
        "students_count": row.students_count,
        "comment": row.comment,
        "source": dict(row.source or {}),
        "created_at": row.created_at,
        "is_repeat": bool(row.is_repeat),
    }


def split_name(contact_name: str) -> Tuple[str, str, Optional[str], str]:
    """``(first, last, patronymic, greeting)`` from the free-text name of the form.

    «Фамилия Имя Отчество» (three words): the greeting is «Имя Отчество». Two words are read as «Имя Фамилия» and greeted in full (the order
    cannot be known); one word is both. The console can override everything when it approves the request."""
    words = contact_name.split()
    if len(words) >= 3:
        last, first, patronymic = words[0], words[1], " ".join(words[2:])
        return first, last, patronymic, f"{first} {patronymic}"
    if len(words) == 2:
        return words[0], words[1], None, contact_name.strip()
    word = words[0] if words else "—"
    return word, "—", None, word
