"""/api/v1/auth/support/*: «Поддержка» of the cabinets - an owner, an administrator or a teacher writes to the ALGO team and reads the answers («Мои обращения»).

Every route is the person's OWN: the ticket is found by ``author_user_id`` and anything else is the same 404 as a missing one (the owner of a centre does not see the tickets of his
employees; nobody sees another centre's). The screenshots go one per request to ``…/attachments`` (JPEG / PNG, 2 MB; the size is refused BEFORE the body is read).
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

from src.core.database import get_db

from . import support_mail as mail
from . import support_rules as rules
from . import support_service as svc
from .dependencies import require_org_teacher
from .platform_errors import PlatformError
from .router import _is_cross_site_request

router = APIRouter(prefix="/auth/support", tags=["Support"])

CROSS_SITE_TEXT = "Запрос должен приходить с сайта ALGO"


def _same_site(request: Request) -> None:
    """A mutating route works on the cookies: a request that another site started is refused (K2-2 R3, 4)."""
    if _is_cross_site_request(request):
        raise PlatformError(403, CROSS_SITE_TEXT, "bad_origin")


@router.post("/tickets")
def create_ticket(
    request: Request, response: Response, background: BackgroundTasks, body: dict = Body(default=None),
    user: dict = Depends(require_org_teacher), db: Session = Depends(get_db),
):
    _same_site(request)
    ticket, created = svc.create_ticket(db, user, body, request.headers.get("user-agent"))
    db.commit()
    if created:
        background.add_task(mail.deliver_new_ticket, str(ticket.id))
    response.status_code = 201 if created else 200
    return {"id": str(ticket.id), "number": ticket.number, "status": ticket.status, "created_at": svc.pc.iso(ticket.created_at)}


@router.get("/tickets")
def list_tickets(user: dict = Depends(require_org_teacher), db: Session = Depends(get_db)):
    return {"items": svc.list_own(db, user)}


@router.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str, user: dict = Depends(require_org_teacher), db: Session = Depends(get_db)):
    ticket = svc.own_ticket(db, user, ticket_id)
    svc.sweep_waiting(db, ticket_id=ticket.id)
    db.refresh(ticket)
    return svc.author_card(db, ticket)


@router.post("/tickets/{ticket_id}/messages")
def reply(
    ticket_id: str, request: Request, response: Response, background: BackgroundTasks, body: dict = Body(default=None),
    user: dict = Depends(require_org_teacher), db: Session = Depends(get_db),
):
    _same_site(request)
    ticket, message, created = svc.add_author_reply(db, user, ticket_id, body)
    db.commit()
    if created:
        background.add_task(mail.deliver_author_reply, str(message.id))
    response.status_code = 201 if created else 200
    return svc.author_card(db, ticket)


@router.post(
    "/tickets/{ticket_id}/attachments",
    openapi_extra={"requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {"type": "object", "required": ["file"], "properties": {"file": {"type": "string", "format": "binary"}}}}}}},
)
async def upload_attachment(
    ticket_id: str, request: Request, user: dict = Depends(require_org_teacher), db: Session = Depends(get_db),
):
    """One screenshot per request. The size is refused before the body is read (``UploadSizeLimitMiddleware``); the type is judged by the first bytes."""
    _same_site(request)
    await run_in_threadpool(svc.own_ticket, db, user, ticket_id)  # somebody else's ticket: 404 BEFORE a byte of the body is read
    form = await request.form()
    try:
        upload = form.get("file")
        if not isinstance(upload, StarletteUploadFile):
            raise PlatformError(422, "Передайте файл в поле file", "no_file")
        data = await upload.read(rules.MAX_ATTACHMENT_BYTES + 1)  # never more than one byte past the limit in memory
    finally:
        await form.close()

    def store():
        ticket = svc.own_ticket(db, user, ticket_id, lock=True)
        row = svc.add_attachment(db, user, ticket, data)
        db.commit()
        return {"id": str(row.id), "content_type": row.content_type, "size": row.size}

    return await run_in_threadpool(store)


@router.get("/attachments/{attachment_id}")
def download_attachment(attachment_id: str, user: dict = Depends(require_org_teacher), db: Session = Depends(get_db)):
    row, ticket, index = svc.attachment_for(db, attachment_id, author_user_id=user["user_id"])
    kind = rules.sniff_image(row.data)
    if kind is None:
        raise PlatformError(404, "Снимок не найден", "not_found")
    return Response(content=row.data, media_type=kind, headers=rules.download_headers(kind, ticket.number, index))
