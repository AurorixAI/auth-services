"""/api/v1/platform/demo-requests*: the requests for access - list, card, status, notes, approve, decline."""
from __future__ import annotations

import logging
import secrets
from typing import Dict, Optional

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.core.security import hash_password
from src.mail import letters
from src.models.auth import Organization, OrganizationMember, User
from src.models.platform import DEMO_REQUEST_STATUSES, DemoRequest, DemoRequestNote

from . import demo_requests as dr
from . import password_links as links
from . import platform_audit as audit
from .platform_common import contains, date_range, iso, not_found, page_params, page_response, query_form, utcnow
from .platform_errors import PlatformError
from .platform_security import Principal, require_platform
from .platform_validation import Form, parse_uuid_or_404

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/demo-requests")

OPEN_STATUSES = ("new", "contacted", "demo_done", "spam")
FINAL_STATUSES = ("approved", "declined")
SORTS = {
    "created_at": DemoRequest.created_at,
    "updated_at": DemoRequest.updated_at,
    "centre_name": func.lower(DemoRequest.centre_name),
    "status": DemoRequest.status,
}


def _users(db: Session, ids) -> Dict[str, dict]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = db.query(User.id, User.first_name, User.last_name, User.email).filter(User.id.in_(ids)).all()
    return {str(r.id): {"id": str(r.id), "name": f"{r.first_name} {r.last_name}".strip(), "email": r.email} for r in rows}


def _row(r: DemoRequest, people: Dict[str, dict]) -> dict:
    handler = people.get(str(r.handled_by)) if r.handled_by else None
    return {
        "id": str(r.id),
        "contact_name": r.contact_name,
        "centre_name": r.centre_name,
        "city": r.city,
        "phone": r.phone,
        "email": r.email,
        "students_count": r.students_count,
        "status": r.status,
        "is_repeat": bool(r.is_repeat),
        "source": r.source or {},
        "created_at": iso(r.created_at),
        "updated_at": iso(r.updated_at),
        "handled_by": {"id": handler["id"], "name": handler["name"]} if handler else None,
        "org_id": str(r.org_id) if r.org_id else None,
    }


def _card(db: Session, r: DemoRequest) -> dict:
    notes = db.query(DemoRequestNote).filter(DemoRequestNote.request_id == r.id).order_by(DemoRequestNote.created_at.asc()).all()
    same = (
        db.query(DemoRequest)
        .filter(DemoRequest.email == r.email, DemoRequest.id != r.id)
        .order_by(DemoRequest.created_at.desc())
        .limit(20)
        .all()
    )
    people = _users(db, [r.handled_by] + [n.author_id for n in notes])
    org = db.get(Organization, r.org_id) if r.org_id else None
    out = _row(r, people)
    out.update(
        {
            "comment": r.comment,
            "decline_reason": r.decline_reason,
            "notes": [
                {
                    "id": str(n.id),
                    "author": ({"id": people[str(n.author_id)]["id"], "name": people[str(n.author_id)]["name"]} if n.author_id and str(n.author_id) in people else None),
                    "text": n.text,
                    "created_at": iso(n.created_at),
                }
                for n in notes
            ],
            "history": audit.history(db, "demo_request", r.id),
            "same_email": [{"id": str(s.id), "created_at": iso(s.created_at), "status": s.status, "centre_name": s.centre_name} for s in same],
            "org": {"id": str(org.id), "name": org.name} if org else None,
        }
    )
    return out


def _get(db: Session, request_id: str, *, lock: bool = False) -> DemoRequest:
    uid = parse_uuid_or_404(request_id, "Заявка")
    q = db.query(DemoRequest).filter(DemoRequest.id == uid)
    if lock:
        q = q.with_for_update()
    row = q.first()
    if row is None:
        raise not_found("Заявка")
    return row


@router.get("")
def list_requests(request: Request, p: Principal = Depends(require_platform("requests.view")), db: Session = Depends(get_db)):
    form = query_form(request)
    page = page_params(request, tuple(SORTS), "created_at")
    q = form.text("q", "Поиск", required=False, max_len=100)
    statuses = [s.strip() for s in str(form.data.get("status") or "").split(",") if s.strip()]
    for s in statuses:
        if s not in DEMO_REQUEST_STATUSES:
            form.fail("status", "Недопустимый статус")
    city = form.text("city", "Город", required=False, max_len=100)
    source = form.text("source", "Источник", required=False, max_len=300)
    repeat = form.choice("repeat", "repeat", ("true", "false"), required=False)
    start, end = date_range(form)
    form.done()

    query = db.query(DemoRequest)
    if q:
        like = contains(q)
        query = query.filter(
            or_(
                DemoRequest.contact_name.ilike(like, escape="\\"), DemoRequest.centre_name.ilike(like, escape="\\"),
                DemoRequest.email.ilike(like, escape="\\"), DemoRequest.phone.ilike(like, escape="\\"),
            )
        )
    if statuses:
        query = query.filter(DemoRequest.status.in_(statuses))
    if city:
        query = query.filter(DemoRequest.city.ilike(contains(city), escape="\\"))
    if source:
        query = query.filter(DemoRequest.source["utm_source"].astext == source)
    if repeat:
        query = query.filter(DemoRequest.is_repeat.is_(repeat == "true"))
    if start:
        query = query.filter(DemoRequest.created_at >= start)
    if end:
        query = query.filter(DemoRequest.created_at < end)
    total = query.count()
    column = SORTS[page.sort]
    ordered = column.asc() if page.order == "asc" else column.desc()
    rows = query.order_by(ordered, DemoRequest.id).offset(page.offset).limit(page.size).all()
    people = _users(db, [r.handled_by for r in rows])

    counts = {s: 0 for s in DEMO_REQUEST_STATUSES}
    for status, n in db.query(DemoRequest.status, func.count()).group_by(DemoRequest.status).all():
        counts[status] = n
    counts["all"] = sum(counts.values())
    return page_response([_row(r, people) for r in rows], total, page, counts=counts)


@router.get("/{request_id}")
def get_request(request_id: str, p: Principal = Depends(require_platform("requests.view")), db: Session = Depends(get_db)):
    return _card(db, _get(db, request_id))


@router.patch("/{request_id}/status")
def set_status(
    request_id: str, request: Request, body: dict = Body(default=None),
    p: Principal = Depends(require_platform("requests.manage")), db: Session = Depends(get_db),
):
    form = Form(body)
    new_status = form.choice("status", "Статус", DEMO_REQUEST_STATUSES)
    if new_status in FINAL_STATUSES:
        form.fail("status", "Одобрение и отказ выполняются отдельными действиями")
    form.done()
    row = _get(db, request_id, lock=True)
    if row.status in FINAL_STATUSES:
        raise PlatformError(409, "Заявка уже обработана — статус изменить нельзя", "final_status")
    if row.status != new_status:
        before = row.status
        row.status = new_status
        row.handled_by = p.user.id
        row.updated_at = utcnow()
        audit.record(
            db, actor_id=p.user.id, action="demo_request.status_changed", target_type="demo_request", target_id=row.id,
            details={"from": before, "to": new_status}, request=request,
        )
        db.commit()
    return _card(db, row)


@router.post("/{request_id}/notes", status_code=201)
def add_note(
    request_id: str, request: Request, body: dict = Body(default=None),
    p: Principal = Depends(require_platform("requests.manage")), db: Session = Depends(get_db),
):
    form = Form(body)
    text = form.text("text", "Заметка", max_len=2000, multiline=True)
    form.done()
    row = _get(db, request_id)
    note = DemoRequestNote(request_id=row.id, author_id=p.user.id, text=text, created_at=utcnow())
    db.add(note)
    db.flush()
    row.updated_at = utcnow()
    audit.record(
        db, actor_id=p.user.id, action="demo_request.note_added", target_type="demo_request", target_id=row.id,
        details={"note_id": str(note.id), "preview": text[:120]}, request=request,
    )
    db.commit()
    return {"id": str(note.id), "author": {"id": str(p.user.id), "name": p.name}, "text": note.text, "created_at": iso(note.created_at)}


def _greeting(row: DemoRequest, first_name: Optional[str]) -> str:
    return first_name or dr.split_name(row.contact_name)[3]


@router.post("/{request_id}/approve")
def approve(
    request_id: str, request: Request, body: dict = Body(default=None),
    p: Principal = Depends(require_platform("requests.manage")), db: Session = Depends(get_db),
):
    form = Form(body)
    org_name = form.text("org_name", "Название центра", required=False, max_len=200)
    first_override = form.text("owner_first_name", "Имя владельца", required=False, max_len=100)
    last_override = form.text("owner_last_name", "Фамилия владельца", required=False, max_len=100)
    email_override = form.email("owner_email", "Почта владельца", required=False)
    form.done()

    row = _get(db, request_id, lock=True)  # a second click waits here and then finds the request approved
    if row.status in FINAL_STATUSES:
        raise PlatformError(409, "Заявка уже обработана", "already_processed")
    owner_email = email_override or row.email
    if db.query(User.id).filter(func.lower(User.email) == owner_email.lower()).first() is not None:
        raise PlatformError(
            409, "Пользователь с такой почтой уже есть в ALGO. Укажите другую почту владельца.", "email_in_use"
        )
    first, last, patronymic, greeting = dr.split_name(row.contact_name)
    if first_override:
        first, greeting = first_override, first_override
    if last_override:
        last = last_override
    first, last = first[:100], last[:100]
    patronymic = patronymic[:100] if patronymic else None
    now = utcnow()
    org = Organization(
        name=org_name or row.centre_name, org_type="center", city=row.city, phone=row.phone, email=owner_email,
        status="active", approved_at=now, approved_by=p.user.id,
    )
    db.add(org)
    db.flush()
    owner = User(
        email=owner_email,
        password_hash=hash_password(secrets.token_urlsafe(48)),  # nobody knows it: the owner sets his own through the link
        first_name=first, last_name=last, patronymic=patronymic if not first_override else None, role="owner",
        default_org_id=org.id, language="ru", is_active=True, is_verified=False, must_change_password=False,
    )
    db.add(owner)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise PlatformError(409, "Пользователь с такой почтой уже есть в ALGO. Укажите другую почту владельца.", "email_in_use") from None
    db.add(OrganizationMember(org_id=org.id, user_id=owner.id, org_role="owner"))
    org.owner_id = owner.id
    token, link = links.issue(db, owner.id, "centre_invite", created_by=p.user.id)
    row.status = "approved"
    row.org_id = org.id
    row.handled_by = p.user.id
    row.updated_at = now
    entry = audit.record(
        db, actor_id=p.user.id, action="demo_request.approved", target_type="demo_request", target_id=row.id,
        details={"org_id": str(org.id), "owner_user_id": str(owner.id), "owner_email": owner_email, "link_expires_at": iso(link.expires_at)},
        request=request,
    )
    db.commit()

    sent = letters.send_welcome(
        owner_email, greeting=greeting, centre_name=org.name, token=token, expires_at=link.expires_at, email=owner_email
    )
    entry.details = {**(entry.details or {}), "email_sent": bool(sent)}
    db.commit()
    return {
        "request": _card(db, row),
        "org": {"id": str(org.id), "name": org.name},
        "owner": {"id": str(owner.id), "email": owner.email, "name": f"{owner.first_name} {owner.last_name}".strip()},
        "invite": {"expires_at": iso(link.expires_at), "email_sent": bool(sent)},
    }


@router.post("/{request_id}/decline")
def decline(
    request_id: str, request: Request, body: dict = Body(default=None),
    p: Principal = Depends(require_platform("requests.manage")), db: Session = Depends(get_db),
):
    form = Form(body)
    reason = form.text("reason", "Причина", required=False, max_len=500, multiline=True)
    send_email = form.boolean("send_email")
    form.done()
    row = _get(db, request_id, lock=True)
    if row.status in FINAL_STATUSES:
        raise PlatformError(409, "Заявка уже обработана", "already_processed")
    row.status = "declined"
    row.decline_reason = reason
    row.handled_by = p.user.id
    row.updated_at = utcnow()
    entry = audit.record(
        db, actor_id=p.user.id, action="demo_request.declined", target_type="demo_request", target_id=row.id,
        reason=reason, details={"send_email": send_email}, request=request,
    )
    db.commit()
    if send_email:
        sent = letters.send_application_declined(
            row.email, greeting=dr.split_name(row.contact_name)[3], centre_name=row.centre_name, reason=reason
        )
        entry.details = {**(entry.details or {}), "email_sent": bool(sent)}
        db.commit()
    return _card(db, row)
