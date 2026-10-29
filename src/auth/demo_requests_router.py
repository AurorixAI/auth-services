"""``POST /api/v1/public/demo-requests`` (steps 1-2) and ``PATCH /api/v1/public/demo-requests/{id}`` (step 3): the public «Запросить демо» form (no sign-in).

Contract: ``docs/K3_API.md``.  Order of the checks of POST: honeypot -> repeat of ``request_id`` -> Turnstile -> limits -> unknown keys -> values."""
from __future__ import annotations

import hashlib
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from src.api.rate_limit import client_bucket, hit_window
from src.core.database import get_db
from src.mail import letters
from src.models.platform import DemoRequest

from . import demo_requests as service
from . import turnstile
from .platform_common import utcnow
from .platform_errors import PlatformError
from .platform_validation import parse_uuid_or_404

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/public", tags=["Public"])

PER_IP = 5      # requests per hour from one address (IPv6: per /64) ...
PER_EMAIL = 3   # ... and per e-mail address,
PER_DOMAIN = 10  # ... and per e-mail domain (one mailbox provider of a throw-away flood),
GLOBAL_PER_HOUR = 60  # ... and for the whole form (a botnet with a different address and e-mail every time)
PATCH_PER_IP = 20  # step-3 requests per hour from one address
WINDOW = 3600
THROTTLED_TEXT = "Слишком много заявок. Повторите попытку позже."
CAPTCHA_TEXT = "Проверка не пройдена. Обновите страницу и отправьте ещё раз."
FINAL_STATUSES = ("approved", "declined")


def _throttle(request: Request, body) -> None:
    email = body.get("email") if isinstance(body, dict) else None
    keys = [(f"rl:demo_request_ip:{client_bucket(request)}", PER_IP)]
    if isinstance(email, str) and email.strip():
        digest = hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:32]  # no e-mail in a Redis key
        keys.append((f"rl:demo_request_email:{digest}", PER_EMAIL))
    if isinstance(email, str) and "@" in email:
        domain = hashlib.sha256(email.rsplit("@", 1)[1].strip().lower().encode("utf-8")).hexdigest()[:32]
        keys.append((f"rl:demo_request_domain:{domain}", PER_DOMAIN))
    keys.append(("rl:demo_request_global", GLOBAL_PER_HOUR))
    for key, limit in keys:
        count, left = hit_window(key, WINDOW)
        if count > limit:
            raise PlatformError(429, THROTTLED_TEXT, "rate_limited", retry_after=left)


def _send_letters(request_id: str, mail: dict, send_applicant: bool) -> None:
    """After the response: a failure of the mail server must not touch the request (it is logged by the mail module); the outcome goes to the row."""
    applicant = None
    if send_applicant:
        applicant = "failed"
        try:
            sent = letters.send_application_received(
                mail["email"], contact_name=service.split_name(mail["contact_name"])[3], centre_name=mail["centre_name"]
            )
            applicant = "sent" if sent else "failed"
        except Exception:  # noqa: BLE001
            logger.exception("demo request: the confirmation letter failed")
    team = "failed"
    try:
        team = "sent" if letters.send_new_application(mail) else "failed"
    except Exception:  # noqa: BLE001
        logger.exception("demo request: the notification letter failed")
    service.record_mail_outcome(request_id, team=team, applicant=applicant)


def _request_id_of(body) -> Optional[UUID]:
    """The ``request_id`` of the body when it is a well-formed uuid (a malformed one is reported by the validation later)."""
    try:
        return UUID(str(body.get("request_id"))) if isinstance(body, dict) and body.get("request_id") else None
    except (ValueError, AttributeError, TypeError):
        return None


@router.post("/demo-requests", status_code=201)
def create_demo_request(request: Request, background: BackgroundTasks, body: dict = Body(default=None), db: Session = Depends(get_db)):
    if isinstance(body, dict) and str(body.get("website") or "").strip():
        logger.info("demo request: honeypot filled - dropped silently")  # a bot: the same answer, no record, no letters, no limit spent
        return JSONResponse({"ok": True, "message": service.ACCEPTED_TEXT}, status_code=201)
    request_id = _request_id_of(body)
    if request_id is not None:
        repeat = service.find_by_request_id(db, request_id)
        if repeat is not None:  # a retry after a lost answer / a double click: the same request, no letter, no limit, no Turnstile token (it is single-use)
            return JSONResponse(service.created_body(repeat), status_code=200)
    if not turnstile.verify(body.get("turnstile_token") if isinstance(body, dict) else None):
        raise PlatformError(400, CAPTCHA_TEXT, "captcha_failed")
    _throttle(request, body)
    service.refuse_unknown(body, service.CREATE_KEYS)
    data, form = service.validate(body)
    form.done()
    source = service.clean_source(body.get("source") if isinstance(body, dict) else None)
    send_applicant = service.reserve_applicant_letter(data["email"])
    row, created = service.save_new(db, data, source, client_bucket(request), applicant_mail="pending" if send_applicant else "cap")
    if not created:  # the twin request with the same request_id was faster
        return JSONResponse(service.created_body(row), status_code=200)
    response = JSONResponse(service.created_body(row), status_code=201)
    response.background = background  # a Response returned by hand carries its own background tasks
    background.add_task(_send_letters, str(row.id), service.mail_data(row), send_applicant)
    return response


@router.patch("/demo-requests/{request_id}")
def add_demo_request_details(request_id: str, request: Request, body: dict = Body(default=None), db: Session = Depends(get_db)):
    """Step 3: «Задачи» are written into the request created at step 2 (the token of step 2 says who may)."""
    count, left = hit_window(f"rl:demo_details_ip:{client_bucket(request)}", WINDOW)
    if count > PATCH_PER_IP:
        raise PlatformError(429, THROTTLED_TEXT, "rate_limited", retry_after=left)
    service.refuse_unknown(body, service.PATCH_KEYS)
    uid = parse_uuid_or_404(request_id, "Заявка")
    service.check_details_token(body.get("details_token") if isinstance(body, dict) else None, uid)
    data, form = service.validate_details(body)
    form.done()
    row = db.query(DemoRequest).filter(DemoRequest.id == uid).with_for_update().first()
    if row is None:
        raise PlatformError(404, "Заявка не найдена", "not_found")
    if row.status in FINAL_STATUSES:
        raise PlatformError(409, "Заявка уже обработана", "already_processed")
    for key, value in data.items():
        setattr(row, key, value)
    row.updated_at = utcnow()
    db.commit()
    return {"ok": True, "id": str(row.id)}
