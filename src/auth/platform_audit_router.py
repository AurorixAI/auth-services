"""/api/v1/platform/audit*: the journal of the platform (right ``audit.view``)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import Text, cast, func, or_
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.models.auth import User
from src.models.platform import PlatformAudit

from . import platform_audit as audit
from .platform_common import contains, date_range, page_params, page_response, query_form
from .platform_security import Principal, require_platform

router = APIRouter(prefix="/audit")


@router.get("")
def list_audit(request: Request, p: Principal = Depends(require_platform("audit.view")), db: Session = Depends(get_db)):
    form = query_form(request)
    page = page_params(request, ("created_at",), "created_at")
    actor_id = form.uuid("actor_id", "Сотрудник", required=False)
    actions = [a.strip() for a in str(form.data.get("action") or "").split(",") if a.strip()]
    for a in actions:
        if a not in audit.ACTION_LABELS:
            form.fail("action", f"Неизвестное действие: {a}")
    target_type = form.choice("target_type", "Объект", audit.TARGET_TYPES, required=False)
    target_id = form.text("target_id", "Идентификатор объекта", required=False, max_len=64)
    q = form.text("q", "Поиск", required=False, max_len=100)
    start, end = date_range(form)
    form.done()

    query = db.query(PlatformAudit)
    if actor_id:
        query = query.filter(PlatformAudit.actor_user_id == actor_id)
    if actions:
        query = query.filter(PlatformAudit.action.in_(actions))
    if target_type:
        query = query.filter(PlatformAudit.target_type == target_type)
    if target_id:
        query = query.filter(PlatformAudit.target_id == target_id)
    if start:
        query = query.filter(PlatformAudit.created_at >= start)
    if end:
        query = query.filter(PlatformAudit.created_at < end)
    if q:
        like = contains(q)
        actor_ids = db.query(User.id).filter(User.email.ilike(like, escape="\\"))
        query = query.filter(
            or_(
                PlatformAudit.reason.ilike(like, escape="\\"), cast(PlatformAudit.details, Text).ilike(like, escape="\\"),
                PlatformAudit.ip.ilike(like, escape="\\"), PlatformAudit.actor_user_id.in_(actor_ids),
            )
        )
    total = query.count()
    order = PlatformAudit.created_at.asc() if page.order == "asc" else PlatformAudit.created_at.desc()
    rows = query.order_by(order, PlatformAudit.id).offset(page.offset).limit(page.size).all()
    return page_response(audit.serialize(db, rows), total, page)


@router.get("/actions")
def list_actions(p: Principal = Depends(require_platform("audit.view"))):
    return {"items": [{"action": a, "label": label} for a, label in audit.ACTION_LABELS.items()]}
