"""E-mail change of an employee (K2-2, E): the owner asks, the new address confirms with the link of the letter."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Request
from sqlalchemy.orm import Session

from src.api.rate_limit import rate_limit
from src.core.database import get_db

from . import email_change as change
from .dependencies import require_org_owner
from .platform_errors import PlatformError
from .router import refuse_cross_site

router = APIRouter(prefix="/auth", tags=["Authentication"])

LINK_LIMIT = "60/minute"
RATE_ENDPOINT = "auth-email-change"


@router.post("/org/members/{member_id}/email-change")
def start_email_change(
    member_id: str,
    request: Request,
    background: BackgroundTasks,
    body: dict = Body(default=None),
    user: dict = Depends(require_org_owner),
    db: Session = Depends(get_db),
):
    """«Сотрудники → ⋯ → Изменить почту» (только владелец; админ / преподаватель его центра). Письмо со ссылкой — на НОВЫЙ адрес, уведомление — на старый."""
    refuse_cross_site(request)  # K2-2 R3 (4): a request started by another site is refused
    if not isinstance(body, dict):
        raise PlatformError(422, "Некорректный запрос", "invalid_body")
    return change.start(db, org_id=user["org_id"], actor=user, member_id=member_id, new_email_raw=body.get("email"), background=background)


@router.get("/email-change/{token}", dependencies=[Depends(rate_limit(LINK_LIMIT, endpoint=RATE_ENDPOINT))])
def email_change_info(token: str, db: Session = Depends(get_db)):
    """Страница «Подтвердить новый адрес»: GET ничего не расходует (почтовые сканеры открывают ссылки)."""
    return change.info(db, token)


@router.post("/email-change/{token}", dependencies=[Depends(rate_limit(LINK_LIMIT, endpoint=RATE_ENDPOINT))])
def email_change_confirm(token: str, background: BackgroundTasks, db: Session = Depends(get_db)):
    """Подтвердить: почта заменяется (тот же пользователь), все входы и ссылки закрываются, на новый адрес уходит «Почта изменена»."""
    return change.confirm(db, token, background=background)
