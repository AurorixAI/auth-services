"""«Запросить демо»: validation, normalisation and storage of a public request, and the helpers the console uses on it."""
from __future__ import annotations

import calendar
import hashlib
import hmac
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.rate_limit import hit_window
from src.core.config import get_settings
from src.core.database import SessionLocal
from src.models.platform import DemoRequest

from . import platform_audit as audit
from .demo_request_choices import LOCALES, NEEDS, ORG_TYPES, POSITIONS, TOOLS
from .platform_common import iso, utcnow
from .platform_errors import PlatformError
from .platform_validation import Form, strip_format_controls

logger = logging.getLogger(__name__)

REPEAT_WINDOW = timedelta(days=30)
DETAILS_TTL = timedelta(hours=2)  # how long the token of step 3 lives (A4)
APPLICANT_LETTERS_PER_ADDRESS = 1  # per 24 hours (A5)
APPLICANT_LETTERS_PER_DAY = 100  # the letters of the form to applicants, for everybody, per 24 hours (A5)
DAY = 24 * 3600
# Every key of the body of ``POST``; anything else is refused with 400 (A2).  Step 3 keys may come with the first request as well.
STEP3_KEYS = ("needs", "current_tool", "heard_from", "comment")
CREATE_KEYS = frozenset(
    (
        "centre_name", "org_type", "city", "address", "students_count", "branches_count", "contact_name", "position", "phone", "email",
        "preferred_contact_time", "locale", "consent", "source", "request_id", "turnstile_token", "website",
    )
    + STEP3_KEYS
)
PATCH_KEYS = frozenset(("details_token",) + STEP3_KEYS)
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


def unknown_keys(body: Any, allowed: Iterable[str]) -> List[str]:
    """Keys of the body that the contract does not know (A2): a client newer than the server must be told, not silently cut."""
    if not isinstance(body, dict):
        return []
    return [str(key) for key in body if key not in allowed]


def refuse_unknown(body: Any, allowed: Iterable[str]) -> None:
    extra = unknown_keys(body, allowed)
    if extra:
        fields = {key[:50]: "Неизвестное поле" for key in extra[:10]}
        raise PlatformError(400, "Неизвестные поля в заявке", "unknown_fields", fields=fields)


def _needs(form: Form) -> Optional[List[str]]:
    value = form.data.get("needs")
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 20 or any(not isinstance(v, str) or v not in NEEDS for v in value):
        form.fail("needs", f"Задачи: допустимо {', '.join(NEEDS)}")
        return None
    return list(dict.fromkeys(value))  # no repeats, the order of the form


def _step3(form: Form) -> Dict[str, Any]:
    return {
        "needs": _needs(form),
        "current_tool": form.choice("current_tool", "Чем пользуетесь", TOOLS, required=False),
        "heard_from": form.text("heard_from", "Откуда узнали", required=False, max_len=200),
        "comment": form.text("comment", "Комментарий", required=False, max_len=2000, multiline=True),
    }


def validate_details(body: Any) -> Tuple[Dict[str, Any], Form]:
    """Step 3 sent later (``PATCH``): only the fields that came are returned (``needs: []`` is a value: it clears the list)."""
    form = Form(body)
    data = _step3(form)
    data = {key: value for key, value in data.items() if value is not None}
    if not data and not form.errors:
        form.fail("_", "Нечего дописывать: пришлите задачи, инструмент, источник или комментарий")
    return data, form


def validate(body: Any) -> Tuple[Dict[str, Any], Form]:
    form = Form(body)
    data = {
        "contact_name": form.text("contact_name", "Имя", min_len=2, max_len=120),
        "centre_name": form.text("centre_name", "Название центра", min_len=2, max_len=200),
        "city": form.text("city", "Город", min_len=2, max_len=100),
        "email": form.email("email", "Почта"),
        "students_count": form.integer("students_count", "Количество учеников", minimum=0, maximum=100000),
        "org_type": form.choice("org_type", "Тип центра", ORG_TYPES, required=False),
        "address": form.text("address", "Адрес", required=False, max_len=300),
        "branches_count": form.integer("branches_count", "Количество филиалов", minimum=0, maximum=999),
        "position": form.choice("position", "Должность", POSITIONS, required=False),
        "preferred_contact_time": form.text("preferred_contact_time", "Удобное время для звонка", required=False, max_len=100),
        "locale": form.choice("locale", "Язык", LOCALES, required=False) or "ru",
        "consent": form.boolean("consent"),
        "request_id": form.uuid("request_id", "Идентификатор заявки", required=False),
        **_step3(form),
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


def find_by_request_id(db: Session, request_id: UUID) -> Optional[DemoRequest]:
    return db.query(DemoRequest).filter(DemoRequest.request_id == request_id).first()


def create(db: Session, data: Dict[str, Any], source: Dict[str, str], bucket: str, *, applicant_mail: str = "pending") -> DemoRequest:
    previous = find_previous(db, data["email"])
    now = utcnow()
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
        org_type=data.get("org_type"),
        address=data.get("address"),
        branches_count=data.get("branches_count"),
        position=data.get("position"),
        preferred_contact_time=data.get("preferred_contact_time"),
        needs=data.get("needs"),
        current_tool=data.get("current_tool"),
        heard_from=data.get("heard_from"),
        locale=data.get("locale") or "ru",
        consent_at=now if data.get("consent") else None,
        request_id=data.get("request_id"),
        team_mail_status="pending",
        applicant_mail_status=applicant_mail,
        created_at=now,
        updated_at=now,
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
        "org_type": row.org_type,
        "address": row.address,
        "branches_count": row.branches_count,
        "position": row.position,
        "preferred_contact_time": row.preferred_contact_time,
        "needs": list(row.needs or []),
        "current_tool": row.current_tool,
        "heard_from": row.heard_from,
        "locale": row.locale,
        "consent_at": row.consent_at,
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


# ---- the token of step 3 (A4) -------------------------------------------------------------------------------------------------------------------

def _timestamp(moment: datetime) -> int:
    return calendar.timegm(moment.utctimetuple())


def _signature(request_id: UUID, expires: int) -> str:
    key = get_settings().secret_key.encode("utf-8")
    return hmac.new(key, f"demo-details:{request_id.hex}:{expires}".encode("utf-8"), hashlib.sha256).hexdigest()


def details_token(row: DemoRequest) -> Tuple[Optional[str], str]:
    """``(token, expires_at)`` of a request: signed, derived from the row (so a repeat of the request returns the very same token), 2 hours from
    its creation.  After that time the token is ``None`` (a repeat of an old request has nothing to offer for step 3)."""
    expires = _timestamp(row.created_at + DETAILS_TTL)
    expires_at = iso(row.created_at + DETAILS_TTL)
    if _timestamp(utcnow()) >= expires:
        return None, expires_at
    return f"{row.id.hex}.{expires}.{_signature(row.id, expires)}", expires_at


def check_details_token(token: Any, request_id: UUID) -> None:
    """403 ``invalid_details_token`` (absent, malformed, forged, of another request) or ``details_token_expired``."""
    invalid = PlatformError(403, "Ссылка для дополнения заявки недействительна. Отправьте заявку заново.", "invalid_details_token")
    if not isinstance(token, str) or token.count(".") != 2 or len(token) > 200:
        raise invalid
    hex_id, expires_text, signature = token.split(".")
    if hex_id != request_id.hex or not expires_text.isdigit() or len(expires_text) > 12:
        raise invalid
    expires = int(expires_text)
    if not hmac.compare_digest(signature, _signature(request_id, expires)):
        raise invalid
    if _timestamp(utcnow()) >= expires:
        raise PlatformError(403, "Время для дополнения заявки вышло. Мы свяжемся с вами по указанным контактам.", "details_token_expired")


def created_body(row: DemoRequest) -> Dict[str, Any]:
    token, expires_at = details_token(row)
    return {"ok": True, "message": ACCEPTED_TEXT, "id": str(row.id), "details_token": token, "details_token_expires_at": expires_at}


def save_new(db: Session, data: Dict[str, Any], source: Dict[str, str], bucket: str, *, applicant_mail: str) -> Tuple[DemoRequest, bool]:
    """``(row, created)``.  A concurrent request with the same ``request_id`` that won the unique index leaves ``created=False`` and its row."""
    try:
        row = create(db, data, source, bucket, applicant_mail=applicant_mail)
        db.commit()
        return row, True
    except IntegrityError:
        db.rollback()
        existing = find_by_request_id(db, data["request_id"]) if data.get("request_id") else None
        if existing is None:
            raise
        return existing, False


# ---- the letters to the applicant: the ceilings (A5) and the outcome -------------------------------------------------------------------------------

def reserve_applicant_letter(email: str) -> bool:
    """One letter per address per 24 hours and at most 100 of the form per 24 hours for everybody; ``False`` = over the ceiling (the request is kept)."""
    digest = hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:32]  # no e-mail in a Redis key
    count, _ = hit_window(f"rl:demo_mail_addr:{digest}", DAY)
    if count > APPLICANT_LETTERS_PER_ADDRESS:
        return False
    count, _ = hit_window("rl:demo_mail_day", DAY)
    return count <= APPLICANT_LETTERS_PER_DAY


def background_session() -> Session:
    """A session of its own for the work after the response (tests replace it)."""
    return SessionLocal()


def record_mail_outcome(request_id: str, *, team: str, applicant: Optional[str]) -> None:
    """What happened to the letters, for the card in the console (A8).  ``applicant=None`` keeps what is there (``cap``)."""
    db = background_session()
    try:
        values: Dict[str, Any] = {"team_mail_status": team, "updated_at": DemoRequest.updated_at}  # the explicit updated_at: this is not an edit by a person
        if applicant is not None:
            values["applicant_mail_status"] = applicant
        db.query(DemoRequest).filter(DemoRequest.id == UUID(request_id)).update(values, synchronize_session=False)
        db.commit()
    except Exception:  # noqa: BLE001 - the card then shows «pending»; the request itself is safe
        logger.exception("demo request: the outcome of the letters was not recorded")
        db.rollback()
    finally:
        db.close()
