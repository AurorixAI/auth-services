"""Password recovery endpoints (N13). The database logic is ``password_reset.py``.

Public (no login):
    POST /auth/password/forgot        {email}                  - ask for a link by e-mail
    POST /auth/password/reset/check   {token}                  - is the link alive, whose account
    POST /auth/password/reset         {token, new_password}    - spend the link

For staff (owner / admin / superadmin):
    POST /auth/staff/{user_id}/password-reset-link             - hand a link over in person

CSRF: nothing here is authenticated by an ambient cookie except the last route, which is a POST with
SameSite=Lax cookies AND refuses a request that another site started (Sec-Fetch-Site / Origin), like
``/auth/logout``. The public POSTs carry no credentials a third-party page could ride on.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from src.api.rate_limit import client_ip, enforce_rate_limit, rate_limit
from src.core import email as email_module
from src.core.config import get_settings
from src.core.database import get_db

from . import password_reset as pr
from .dependencies import require_org_admin
from .password_policy import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH
from .service import hash_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Password recovery"])

# «Забыли пароль?»: per mailbox and per client address. Over the limit the answer is the same 200 - the
# only difference is that nothing is sent - so the limit itself tells an outsider nothing.
FORGOT_EMAIL_LIMIT = "3/hour"
FORGOT_IP_LIMIT = "10/hour"
# Spending / looking at links: a 256-bit token cannot be guessed, the limit only stops hammering.
RESET_IP_LIMIT = "20/hour"
CHECK_IP_LIMIT = "60/hour"

NO_STORE = "no-store"


# ----------------------------------------------------------------------------- schemas


class ForgotPasswordRequest(BaseModel):
    email: str = Field(..., min_length=1, max_length=255)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.lower().strip()


class CheckResetRequest(BaseModel):
    # Not a query parameter: the token must not reach an access log. Anything that is not a token of ours
    # is simply "not valid" (no 422 that would tell a probe more).
    token: str = Field(..., max_length=4096)


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=512)
    new_password: str = Field(..., min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


# ----------------------------------------------------------------------------- helpers


def _within_limit(subject: str, rate: str, endpoint: str) -> bool:
    """Count one request against a limit; False (instead of a 429) when it is over."""
    try:
        enforce_rate_limit(subject, rate, endpoint)
        return True
    except HTTPException as exc:
        if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
            return False
        raise


def _is_cross_site_request(request: Request) -> bool:
    """A browser says another site started this request (same rule as ``/auth/logout``).

    ``Sec-Fetch-Site: cross-site`` cannot be set by a page's script; Origin (sent on every POST) must be
    one of the CORS origins - which also stops a sibling sub-domain that shares our cookies. Requests
    with neither header (curl, server-to-server) carry no cookies of a third party's making.
    """
    if request.headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
        return True
    origin = request.headers.get("origin")
    if origin is not None:
        allowed = {o.rstrip("/") for o in get_settings().cors_origins}
        return "*" not in allowed and origin.strip().rstrip("/") not in allowed
    return False


def _email_ref(email: str) -> str:
    """A log-safe reference to a mailbox (the address itself stays out of the limiter keys / logs)."""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:16]


def _delivery_base_url(request: Request) -> Optional[str]:
    """The site the e-mailed link opens, if e-mail can be sent at all: SMTP configured AND a trusted base URL."""
    if not email_module.smtp_configured():
        return None
    return pr.frontend_base_url(request.headers.get("origin"))


# ----------------------------------------------------------------------------- public endpoints


@router.post("/password/forgot")
def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """«Забыли пароль?». ALWAYS the same 200 ``{ok, email_delivery}`` - whether the account exists, is
    active, or the request was rate-limited. ``email_delivery`` is global configuration (SMTP + a
    trusted site address), the same for every address, so it cannot be used to enumerate accounts.
    """
    base_url = _delivery_base_url(request)
    result = {"ok": True, "email_delivery": base_url is not None}
    if base_url is None:
        logger.info("password reset requested, e-mail delivery is not configured (SMTP / FRONTEND_URL)")
        return result

    ip = client_ip(request)
    ip_ok = _within_limit(ip, FORGOT_IP_LIMIT, "pw-forgot-ip")
    mailbox_ok = _within_limit(hash_token(body.email), FORGOT_EMAIL_LIMIT, "pw-forgot-email")
    if not (ip_ok and mailbox_ok):
        logger.warning("password reset request rate-limited: mailbox=%s ip=%s", _email_ref(body.email), ip)
        return result

    user = pr.find_active_user_by_email(db, body.email)
    if user is None:
        logger.info("password reset requested for an unknown or disabled account: mailbox=%s", _email_ref(body.email))
        return result

    raw, expires_at = pr.issue_token(
        db, user.id, pr.PURPOSE_EMAIL, pr.EMAIL_LINK_TTL, created_by=None, ip=ip
    )
    # After the response: the SMTP round trip must not tell an outsider that the account exists.
    background.add_task(
        email_module.send_password_reset_email,
        user.email,
        user.first_name,
        pr.reset_url(base_url, raw),
        int(pr.EMAIL_LINK_TTL.total_seconds() // 60),
    )
    logger.info("password reset link e-mailed: user=%s expires=%s", user.id, expires_at.isoformat())
    return result


@router.post("/password/reset/check")
def check_reset_link(
    body: CheckResetRequest,
    response: Response,
    db: Session = Depends(get_db),
    _rl=Depends(rate_limit(CHECK_IP_LIMIT, endpoint="pw-reset-check")),
):
    """Is the link alive? ``{valid, email_masked, expires_at}``; ``{valid: false}`` for any other case."""
    response.headers["Cache-Control"] = NO_STORE
    found = pr.find_live_token(db, body.token)
    if found is None:
        return {"valid": False}
    row, user = found
    return {"valid": True, "email_masked": pr.mask_email(user.email), "expires_at": row.expires_at.isoformat()}


@router.post("/password/reset")
def reset_password(
    body: ResetPasswordRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    _rl=Depends(rate_limit(RESET_IP_LIMIT, endpoint="pw-reset")),
):
    """Spend the link: new password, every old login of the account ends. Does NOT sign anybody in."""
    response.headers["Cache-Control"] = NO_STORE
    try:
        user = pr.spend_token(db, body.token, body.new_password)
    except pr.InvalidResetLink:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, pr.INVALID_LINK_MESSAGE)
    logger.info("password reset completed: user=%s ip=%s", user.id, client_ip(request))
    return {"ok": True}


# ----------------------------------------------------------------------------- staff endpoint


@router.post("/staff/{user_id}/password-reset-link")
def issue_staff_reset_link(
    user_id: str,
    request: Request,
    response: Response,
    actor: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Hand a one-time link (24 h) to a colleague who cannot get one by e-mail.

    owner -> admins and teachers of their organisation; admin -> teachers of theirs; superadmin ->
    organisation owners. Anyone else (another organisation, another owner, a teacher, nobody) is the
    same 404. The link is in the response only; the log line names actor and target, never the token.
    """
    if _is_cross_site_request(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Запрос должен приходить с сайта ALGO")
    response.headers["Cache-Control"] = NO_STORE

    target = pr.staff_target_for(db, actor, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, pr.STAFF_NOT_FOUND_MESSAGE)
    base_url = pr.frontend_base_url(request.headers.get("origin"))
    if base_url is None:
        logger.error("password reset link not issued: FRONTEND_URL is not configured and Origin is not a CORS origin")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Адрес сайта не настроен. Обратитесь к администратору платформы.",
        )

    raw, expires_at = pr.issue_token(
        db,
        target.id,
        pr.PURPOSE_ISSUED,
        pr.ISSUED_LINK_TTL,
        created_by=UUID(str(actor["user_id"])),
        ip=client_ip(request),
    )
    logger.info(
        "staff password reset link issued: actor=%s actor_role=%s org=%s target=%s expires=%s",
        actor.get("user_id"),
        actor.get("org_role") or actor.get("role"),
        actor.get("org_id"),
        target.id,
        expires_at.isoformat(),
    )
    return {"url": pr.reset_url(base_url, raw), "expires_at": expires_at.isoformat()}
