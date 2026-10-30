"""``POST /api/v1/auth/password/forgot`` (public) and ``POST /api/v1/auth/org/members/{member_id}/password-link`` (the leadership of a centre). K2-2, B and C."""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from src.api.rate_limit import client_bucket, client_ip, rate_limit
from src.core.database import get_db

from . import password_reset as reset
from . import turnstile
from .dependencies import require_org_member
from .platform_errors import PlatformError
from .platform_validation import EMAIL_RE
from .router import refuse_cross_site

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])

CAPTCHA_TEXT = "Не удалось пройти проверку. Обновите страницу и попробуйте ещё раз."
FORGOT_IP_BURST = "30/minute"  # a generous guard per client address (the hourly budgets are counted after the check)
FORGOT_RATE_ENDPOINT = "auth-password-forgot"


def _neutral() -> JSONResponse:
    """The ONE answer of «Забыли пароль»: the same code, body and headers for every address (an existing one, an unknown one, a blocked one, a console one,
    one over its limit, a malformed one)."""
    return JSONResponse({"ok": True, "message": reset.NEUTRAL_TEXT}, status_code=200)


@router.post("/password/forgot", dependencies=[Depends(rate_limit(FORGOT_IP_BURST, endpoint=FORGOT_RATE_ENDPOINT))])
def forgot_password(request: Request, background: BackgroundTasks, body: dict = Body(default=None)):
    """Ссылка для нового пароля на почту сотрудника. Ответ ВСЕГДА один и тот же; поиск, выпуск ссылки и письмо — в фоне.

    Проверка Turnstile (одинаково для любой почты) → счётчики: ящик 3/ч, адрес клиента 10/ч, домен 30/ч (при превышении — тот же ответ, письма нет)."""
    if not isinstance(body, dict):
        raise PlatformError(422, "Некорректный запрос", "invalid_body")
    if not turnstile.verify(body.get("turnstile_token"), client_ip(request)):
        raise PlatformError(400, CAPTCHA_TEXT, "captcha_failed")
    email = reset.normalize(body.get("email"))
    shaped = bool(email) and len(email) <= 255 and bool(EMAIL_RE.match(email)) and ".." not in email
    within = reset.count_request(email if shaped else "invalid", client_bucket(request))
    if shaped and within:
        background.add_task(reset.forgot_in_background, email)
    return _neutral()


@router.post("/org/members/{member_id}/password-link")
def send_password_link(
    member_id: str,
    request: Request,
    background: BackgroundTasks,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """«Сотрудники → ⋯ → Отправить ссылку для смены пароля»: владелец → админ / преподаватель своего центра, админ → преподаватель; всё остальное — один и тот же 404.
    Письмо уходит СОТРУДНИКУ (называет отправителя); пароль не меняется, пока сотрудник сам не задаст новый. Чужой Origin — 403 (K2-2 R3, 4)."""
    refuse_cross_site(request)
    return reset.send_to_member(db, org_id=user["org_id"], actor=user, member_id=member_id, background=background)
