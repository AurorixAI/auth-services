"""``POST /api/v1/public/demo-requests``: the public «Запросить демо» form (no sign-in)."""
from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from src.api.rate_limit import client_bucket, hit_window
from src.core.database import get_db
from src.mail import letters

from . import demo_requests as service
from .platform_errors import PlatformError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/public", tags=["Public"])

PER_IP = 5      # requests per hour from one address (IPv6: per /64) ...
PER_EMAIL = 3   # ... and per e-mail address,
PER_DOMAIN = 10  # ... and per e-mail domain (one mailbox provider of a throw-away flood),
GLOBAL_PER_HOUR = 60  # ... and for the whole form (a botnet with a different address and e-mail every time)
WINDOW = 3600
THROTTLED_TEXT = "Слишком много заявок. Повторите попытку позже."


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


def _send_letters(mail: dict) -> None:
    """After the response: a failure of the mail server must not touch the request (it is logged by the mail module)."""
    try:
        letters.send_application_received(
            mail["email"], contact_name=service.split_name(mail["contact_name"])[3], centre_name=mail["centre_name"]
        )
    except Exception:  # noqa: BLE001
        logger.exception("demo request: the confirmation letter failed")
    try:
        letters.send_new_application(mail)
    except Exception:  # noqa: BLE001
        logger.exception("demo request: the notification letter failed")


@router.post("/demo-requests", status_code=201)
def create_demo_request(request: Request, background: BackgroundTasks, body: dict = Body(default=None), db: Session = Depends(get_db)):
    _throttle(request, body)
    accepted = JSONResponse({"ok": True, "message": service.ACCEPTED_TEXT}, status_code=201)
    if isinstance(body, dict) and str(body.get("website") or "").strip():
        logger.info("demo request: honeypot filled - dropped silently")  # a bot: the same answer, no record, no letters
        return accepted
    data, form = service.validate(body)
    form.done()
    source = service.clean_source(body.get("source") if isinstance(body, dict) else None)
    row = service.create(db, data, source, client_bucket(request))
    db.commit()
    background.add_task(_send_letters, service.mail_data(row))
    accepted.background = background  # a Response returned by hand carries its own background tasks
    return accepted
