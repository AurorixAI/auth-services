"""/api/v1/platform/support/*: «Обращения» of the console - the list, the card, the status, the answer, the screenshot (right ``support.manage``; nothing without it)."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Request, Response
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.models.auth import Organization
from src.models.support import SupportTicket

from . import platform_audit as audit
from . import support_mail as mail
from . import support_rules as rules
from . import support_service as svc
from . import platform_common as pc
from .platform_common import page_params, page_response, query_form
from .platform_security import Principal, require_platform
from .platform_validation import Form

router = APIRouter(prefix="/support")
RIGHT = "support.manage"
SORTS = {"updated_at": SupportTicket.updated_at, "created_at": SupportTicket.created_at, "number": SupportTicket.number, "status": SupportTicket.status}
ROLES = ("owner", "admin", "teacher")


def _card(db: Session, ticket: SupportTicket) -> dict:
    return svc.team_card(db, ticket, audit.history(db, "support_ticket", ticket.id))


@router.get("/summary")
def summary(p: Principal = Depends(require_platform(RIGHT)), db: Session = Depends(get_db)):
    """The number of new tickets for the menu (one light query)."""
    svc.sweep_waiting(db)
    counts = svc.counts(db)
    return {"new": counts["new"], "open": counts["new"] + counts["in_progress"] + counts["waiting"]}


@router.get("/tickets")
def list_tickets(request: Request, p: Principal = Depends(require_platform(RIGHT)), db: Session = Depends(get_db)):
    form = query_form(request)
    page = page_params(request, tuple(SORTS), "updated_at")
    q = form.text("q", "Поиск", required=False, max_len=100)
    statuses = [s.strip() for s in str(form.data.get("status") or "").split(",") if s.strip()]
    for s in statuses:
        if s not in rules.STATUS_WORDS:
            form.fail("status", "Недопустимый статус")
    org_id = form.uuid("org_id", "Центр", required=False)
    org_name = form.text("org", "Название центра", required=False, max_len=100)
    role = form.choice("role", "Роль", ROLES, required=False)
    form.done()

    svc.sweep_waiting(db)
    query = db.query(SupportTicket)
    if q:
        query = query.filter(svc.search_clause(q))
    if statuses:
        query = query.filter(SupportTicket.status.in_(statuses))
    if org_id:
        query = query.filter(SupportTicket.org_id == org_id)
    if org_name:
        query = query.filter(SupportTicket.org_id.in_(db.query(Organization.id).filter(Organization.name.ilike(pc.contains(org_name), escape="\\"))))
    if role:
        query = query.filter(SupportTicket.author_role == role)
    total = query.count()
    column = SORTS[page.sort]
    rows = query.order_by(column.asc() if page.order == "asc" else column.desc(), SupportTicket.number.desc()).offset(page.offset).limit(page.size).all()
    return page_response(svc.team_items(db, rows), total, page, counts=svc.counts(db))


@router.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str, p: Principal = Depends(require_platform(RIGHT)), db: Session = Depends(get_db)):
    ticket = svc.team_ticket(db, ticket_id)
    svc.sweep_waiting(db, ticket_id=ticket.id)
    db.refresh(ticket)
    return _card(db, ticket)


@router.patch("/tickets/{ticket_id}/status")
def set_status(
    ticket_id: str, request: Request, body: dict = Body(default=None),
    p: Principal = Depends(require_platform(RIGHT)), db: Session = Depends(get_db),
):
    form = Form(body)
    status = form.choice("status", "Статус", rules.STAFF_SETTABLE)
    form.done()
    ticket = svc.team_ticket(db, ticket_id, lock=True)
    old = svc.set_status(db, p.user.id, ticket, status)
    if old is not None:
        audit.record(
            db, actor_id=p.user.id, action="support.status_changed", target_type="support_ticket", target_id=ticket.id,
            details={"number": ticket.number, "from": old, "to": status}, request=request,
        )
    db.commit()
    return _card(db, ticket)


@router.post("/tickets/{ticket_id}/messages")
def reply(
    ticket_id: str, request: Request, response: Response, background: BackgroundTasks, body: dict = Body(default=None),
    p: Principal = Depends(require_platform(RIGHT)), db: Session = Depends(get_db),
):
    ticket = svc.team_ticket(db, ticket_id, lock=True)
    message, created, has_access = svc.add_staff_reply(db, p.user.id, ticket, body)
    if created:
        audit.record(
            db, actor_id=p.user.id, action="support.replied", target_type="support_ticket", target_id=ticket.id,
            details={"number": ticket.number, "author_has_access": has_access}, request=request,  # who and which ticket - never the words
        )
    db.commit()
    if created and has_access:
        background.add_task(mail.deliver_staff_reply, str(message.id))
    response.status_code = 201 if created else 200
    return _card(db, ticket)


@router.get("/attachments/{attachment_id}")
def download_attachment(attachment_id: str, p: Principal = Depends(require_platform(RIGHT)), db: Session = Depends(get_db)):
    row, ticket, index = svc.attachment_for(db, attachment_id)
    kind = rules.sniff_image(row.data)
    if kind is None:
        raise svc.PlatformError(404, "Снимок не найден", "not_found")
    return Response(content=row.data, media_type=kind, headers=rules.download_headers(kind, ticket.number, index))
