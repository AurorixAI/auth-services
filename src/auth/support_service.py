"""«Поддержка» (K4a): the work with the tickets - for the author (a cabinet) and for the team (the console). No HTTP here, no letters (``support_mail``).

* The author sees ONLY his own tickets (``author_user_id``); a ticket of another person, of another centre, of the owner's employee is the same 404 as a missing one.
* The statuses: ``new`` -> ``in_progress`` (the team took it) -> ``waiting`` (the team answered; the author's turn) -> ``resolved``. An answer of the author in ``waiting`` returns
  ``in_progress``; ``waiting`` for 14 days without an answer of the author closes by itself (checked LAZILY on reading: ``sweep_waiting``); ``resolved`` is opened again by an
  answer of the author within 14 days.
* The limits are counted from the tickets that EXIST in the last 24 hours (a request that was refused spends nobody's budget), under an advisory lock, so that parallel
  requests cannot pass the limit together (LESSONS 69; the lock order is the person, then the centre).
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional
from uuid import UUID

from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from src.models.auth import Organization, OrganizationMember, User
from src.models.support import SupportAttachment, SupportMessage, SupportTicket

from . import platform_common as pc
from . import support_rules as rules
from .platform_errors import PlatformError
from .platform_validation import Form, parse_uuid_or_404

ROLE_WORDS = {"owner": "Владелец", "admin": "Администратор", "teacher": "Преподаватель"}
_SPACES = re.compile(r"\s+")


def now() -> datetime:
    return pc.utcnow()


def not_found() -> PlatformError:
    return PlatformError(404, rules.NOT_FOUND_TEXT, "not_found")


def preview(value: str, length: int = 140) -> str:
    one_line = _SPACES.sub(" ", value or "").strip()
    return one_line if len(one_line) <= length else one_line[: length - 1].rstrip() + "…"


def full_name(user: Optional[User]) -> str:
    return f"{user.first_name} {user.last_name}".strip() if user is not None else ""


# ── the clock: the lazy closing of «Ждём ответа» ─────────────────────────────────────────────────────
def sweep_waiting(db: Session, *, author_user_id=None, ticket_id=None, commit: bool = True) -> int:
    """«Ждём ответа» for 14 days (since the last change of the ticket) -> «Решено», closed by the clock (``resolved_by`` stays empty). ``resolved_at`` is the logical moment
    (+14 days), so the window of «Открыть снова» counts from it and does not depend on who looked first."""
    limit = now() - timedelta(days=rules.WAITING_AUTO_RESOLVE_DAYS)
    q = db.query(SupportTicket).filter(SupportTicket.status == "waiting", SupportTicket.updated_at <= limit)
    if author_user_id is not None:
        q = q.filter(SupportTicket.author_user_id == author_user_id)
    if ticket_id is not None:
        q = q.filter(SupportTicket.id == ticket_id)
    closed = 0
    for ticket in q.with_for_update(skip_locked=True).all():
        ticket.status = "resolved"
        ticket.resolved_at = ticket.updated_at + timedelta(days=rules.WAITING_AUTO_RESOLVE_DAYS)
        ticket.resolved_by = None
        ticket.updated_at = ticket.resolved_at
        closed += 1
    if closed and commit:
        db.commit()
    return closed


def can_reopen(ticket: SupportTicket) -> bool:
    return ticket.status == "resolved" and ticket.resolved_at is not None and now() - ticket.resolved_at <= timedelta(days=rules.REOPEN_DAYS)


# ── the author: create ─────────────────────────────────────────────────────────────────────────────────
def _advisory(db: Session, key: str) -> None:
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})


def _retry_after(moments: List[datetime]) -> int:
    """Seconds until the oldest of ``moments`` leaves the 24-hour window."""
    return max(int((min(moments) + timedelta(days=1) - now()).total_seconds()), 1) if moments else 60


def _too_many(moments: List[datetime], message: str = rules.TOO_MANY_TEXT) -> PlatformError:
    return PlatformError(429, message, "rate_limited", retry_after=_retry_after(moments))


def create_ticket(db: Session, user: dict, body: Any, user_agent: Optional[str]) -> tuple[SupportTicket, bool]:
    """(ticket, created). The role, the centre and the name come from the SESSION, never from the body. The same ``client_key`` returns the same ticket (a retry, a double click)."""
    form = Form(body)
    text_ = form.text("text", "Текст", min_len=rules.TEXT_MIN, max_len=rules.TEXT_MAX, multiline=True)
    client_key = form.uuid("client_key", "Ключ запроса", required=False)
    form.done()
    page = rules.page_template(body.get("page"))
    errors = rules.client_errors(body.get("errors"))

    author_id, org_id = UUID(str(user["user_id"])), UUID(str(user["org_id"]))
    _advisory(db, f"support:user:{author_id}")
    _advisory(db, f"support:org:{org_id}")
    if client_key is not None:
        same = db.query(SupportTicket).filter(SupportTicket.author_user_id == author_id, SupportTicket.client_key == client_key).first()
        if same is not None:
            return same, False
    since = now() - timedelta(days=1)
    mine = [t for (t,) in db.query(SupportTicket.created_at).filter(SupportTicket.author_user_id == author_id, SupportTicket.created_at > since)]
    if len(mine) >= rules.TICKETS_PER_AUTHOR_DAY:
        raise _too_many(mine)
    centre = [t for (t,) in db.query(SupportTicket.created_at).filter(SupportTicket.org_id == org_id, SupportTicket.created_at > since)]
    if len(centre) >= rules.TICKETS_PER_ORG_DAY:
        raise _too_many(centre)

    moment = now()
    ticket = SupportTicket(
        org_id=org_id, author_user_id=author_id, author_name=full_name(db.get(User, author_id))[:200], author_role=user["org_role"], status="new", text=text_,
        page_template=page, browser=rules.browser_of(user_agent), client_errors=errors, client_key=client_key, team_mail_status="pending",
        created_at=moment, updated_at=moment,
    )
    db.add(ticket)
    db.flush()
    return ticket, True


# ── the author: read ───────────────────────────────────────────────────────────────────────────────────
def own_ticket(db: Session, user: dict, ticket_id: str, *, lock: bool = False) -> SupportTicket:
    """The ticket of THIS person, else the single 404 (a missing one, somebody else's, another centre's)."""
    tid = parse_uuid_or_404(ticket_id, "Обращение")
    q = db.query(SupportTicket).filter(SupportTicket.id == tid, SupportTicket.author_user_id == UUID(str(user["user_id"])))
    if lock:
        q = q.with_for_update()
    ticket = q.first()
    if ticket is None:
        raise not_found()
    return ticket


def attachments_of(db: Session, ticket_ids: Iterable) -> Dict[Any, List[dict]]:
    """``{ticket_id: [{id, content_type, size, index}]}`` in one query, WITHOUT the bytes."""
    ids = list(ticket_ids)
    out: Dict[Any, List[dict]] = {i: [] for i in ids}
    if not ids:
        return out
    rows = (
        db.query(SupportAttachment.id, SupportAttachment.ticket_id, SupportAttachment.content_type, SupportAttachment.size)
        .filter(SupportAttachment.ticket_id.in_(ids))
        .order_by(SupportAttachment.created_at, SupportAttachment.id)
        .all()
    )
    for r in rows:
        out[r.ticket_id].append({"id": str(r.id), "content_type": r.content_type, "size": r.size, "index": len(out[r.ticket_id]) + 1})
    return out


def author_item(t: SupportTicket) -> dict:
    return {
        "id": str(t.id), "number": t.number, "status": t.status, "preview": preview(t.text), "created_at": pc.iso(t.created_at),
        "updated_at": pc.iso(t.updated_at), "last_reply_at": pc.iso(t.last_staff_reply_at),
    }


def list_own(db: Session, user: dict) -> List[dict]:
    author_id = UUID(str(user["user_id"]))
    sweep_waiting(db, author_user_id=author_id)
    rows = db.query(SupportTicket).filter(SupportTicket.author_user_id == author_id).order_by(SupportTicket.updated_at.desc(), SupportTicket.number.desc()).limit(200).all()
    return [author_item(t) for t in rows]


def author_card(db: Session, t: SupportTicket) -> dict:
    """What the AUTHOR sees: the text, the thread (the team is «Команда ALGO»: no names, no outcomes of letters), the screenshots, what he may do now. No fields of the team."""
    messages = db.query(SupportMessage).filter(SupportMessage.ticket_id == t.id).order_by(SupportMessage.created_at, SupportMessage.id).all()
    files = attachments_of(db, [t.id])[t.id]
    out = author_item(t)
    out.update(
        {
            "text": t.text,
            "resolved_at": pc.iso(t.resolved_at),
            "messages": [{"id": str(m.id), "from": "me" if m.author_kind == "user" else "team", "text": m.text, "created_at": pc.iso(m.created_at)} for m in messages],
            "attachments": files,
            "can_reply": t.status != "resolved",
            "can_reopen": can_reopen(t),
            "can_attach": t.status != "resolved" and len(files) < rules.MAX_ATTACHMENTS,
        }
    )
    return out


# ── the author: reply, screenshots ────────────────────────────────────────────────────────────────────
def add_author_reply(db: Session, user: dict, ticket_id: str, body: Any) -> tuple[SupportTicket, SupportMessage, bool]:
    form = Form(body)
    text_ = form.text("text", "Ответ", min_len=rules.REPLY_MIN, max_len=rules.REPLY_MAX, multiline=True)
    form.done()
    ticket = own_ticket(db, user, ticket_id, lock=True)
    sweep_waiting(db, ticket_id=ticket.id, commit=False)  # inside this transaction: the lock of the ticket is kept until the reply is written
    db.refresh(ticket)
    if ticket.status == "resolved" and not can_reopen(ticket):
        raise PlatformError(409, "Обращение закрыто. Создайте новое — нажмите «Поддержка».", "ticket_closed")
    moment = now()
    same = (
        db.query(SupportMessage)
        .filter(SupportMessage.ticket_id == ticket.id, SupportMessage.author_kind == "user", SupportMessage.text == text_,
                SupportMessage.created_at > moment - timedelta(seconds=rules.DEDUP_REPLY_SECONDS))
        .first()
    )
    if same is not None:  # the double click: the same words a moment ago
        return ticket, same, False
    recent = [t for (t,) in db.query(SupportMessage.created_at).filter(SupportMessage.ticket_id == ticket.id, SupportMessage.author_kind == "user", SupportMessage.created_at > moment - timedelta(days=1))]
    if len(recent) >= rules.AUTHOR_REPLIES_PER_TICKET_DAY:
        raise _too_many(recent, rules.TOO_MANY_REPLIES_TEXT)
    message = SupportMessage(ticket_id=ticket.id, author_kind="user", author_user_id=UUID(str(user["user_id"])), text=text_, created_at=moment, mail_status="pending")
    db.add(message)
    if ticket.status == "resolved":      # opened again by the author: the team sees it as new
        ticket.status, ticket.resolved_at, ticket.resolved_by = "new", None, None
    elif ticket.status == "waiting":
        ticket.status = "in_progress"
    ticket.last_author_reply_at = ticket.updated_at = moment
    db.flush()
    return ticket, message, True


def add_attachment(db: Session, user: dict, ticket: SupportTicket, data: bytes) -> SupportAttachment:
    """``ticket`` is the author's own (``own_ticket``) and locked."""
    if ticket.status == "resolved":
        raise PlatformError(409, "Обращение решено — снимок приложить нельзя.", "ticket_closed")
    if not data:
        raise PlatformError(422, "Файл пустой.", "empty_file")
    if len(data) > rules.MAX_ATTACHMENT_BYTES:
        raise PlatformError(413, rules.FILE_TOO_BIG_TEXT, "file_too_big")
    kind = rules.sniff_image(data)
    if kind is None:
        raise PlatformError(415, rules.FILE_TYPE_TEXT, "file_type")
    if db.query(func.count(SupportAttachment.id)).filter(SupportAttachment.ticket_id == ticket.id).scalar() >= rules.MAX_ATTACHMENTS:
        raise PlatformError(409, rules.FILE_LIMIT_TEXT, "too_many_files")
    row = SupportAttachment(
        ticket_id=ticket.id, uploaded_by_user_id=UUID(str(user["user_id"])), content_type=kind, size=len(data), sha256=hashlib.sha256(data).hexdigest(), data=data, created_at=now(),
    )
    db.add(row)
    db.flush()
    return row


def attachment_for(db: Session, attachment_id: str, *, author_user_id=None) -> tuple[SupportAttachment, SupportTicket, int]:
    """(file, its ticket, its number in the ticket). ``author_user_id`` given: only the files of THAT person's tickets (else the 404); not given: the team's route."""
    aid = parse_uuid_or_404(attachment_id, "Снимок")
    q = db.query(SupportAttachment, SupportTicket).join(SupportTicket, SupportTicket.id == SupportAttachment.ticket_id).filter(SupportAttachment.id == aid)
    if author_user_id is not None:
        q = q.filter(SupportTicket.author_user_id == UUID(str(author_user_id)))
    found = q.first()
    if found is None:
        raise PlatformError(404, "Снимок не найден", "not_found")
    row, ticket = found
    ids = [i for (i,) in db.query(SupportAttachment.id).filter(SupportAttachment.ticket_id == ticket.id).order_by(SupportAttachment.created_at, SupportAttachment.id)]
    return row, ticket, ids.index(row.id) + 1


# ── the team ───────────────────────────────────────────────────────────────────────────────────────────
def access_of(db: Session, tickets: List[SupportTicket]) -> Dict[Any, Optional[str]]:
    """``{ticket_id: None}`` when the author still has access to the cabinet, else the reason in words (the team answers such a person by e-mail, by hand)."""
    out: Dict[Any, Optional[str]] = {}
    user_ids = {t.author_user_id for t in tickets if t.author_user_id}
    org_ids = {t.org_id for t in tickets}
    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
    orgs = {o.id: o for o in db.query(Organization).filter(Organization.id.in_(org_ids)).all()} if org_ids else {}
    members = set()
    if user_ids:
        members = {
            (m.user_id, m.org_id)
            for m in db.query(OrganizationMember).filter(OrganizationMember.user_id.in_(user_ids), OrganizationMember.org_id.in_(org_ids), OrganizationMember.is_active.is_(True))
        }
    for t in tickets:
        user, org = users.get(t.author_user_id), orgs.get(t.org_id)
        if user is None:
            out[t.id] = "аккаунт удалён"
        elif user.platform_blocked_at is not None:
            out[t.id] = "аккаунт заблокирован"
        elif not user.is_active:
            out[t.id] = "аккаунт отключён"
        elif (user.id, t.org_id) not in members:
            out[t.id] = "удалён из центра"
        elif org is None or org.blocked_at is not None:
            out[t.id] = "центр заблокирован"
        elif org.status != "active":
            out[t.id] = "центр не активен"
        else:
            out[t.id] = None
    return out


def mail_shown(status: Optional[str], since: Optional[datetime]) -> Optional[str]:
    """A letter «pending» for longer than two minutes was lost with its process: the team sees it as «failed»."""
    if status == "pending" and since is not None and now() - since > timedelta(seconds=rules.PENDING_MAIL_SHOWN_FAILED_AFTER_SECONDS):
        return "failed"
    return status


def team_items(db: Session, tickets: List[SupportTicket]) -> List[dict]:
    orgs = {o.id: o for o in db.query(Organization).filter(Organization.id.in_({t.org_id for t in tickets})).all()} if tickets else {}
    user_ids = {t.author_user_id for t in tickets if t.author_user_id}
    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
    access = access_of(db, tickets)
    files = attachments_of(db, [t.id for t in tickets])
    out = []
    for t in tickets:
        author, org = users.get(t.author_user_id), orgs.get(t.org_id)
        out.append(
            {
                "id": str(t.id), "number": t.number, "status": t.status, "preview": preview(t.text, 160),
                "org": {"id": str(t.org_id), "name": org.name if org else ""},
                "author": {
                    "id": str(t.author_user_id) if t.author_user_id else None, "name": full_name(author) or t.author_name, "email": author.email if author else None,
                    "role": t.author_role, "role_text": ROLE_WORDS.get(t.author_role, ""), "has_access": access.get(t.id) is None, "access_note": access.get(t.id),
                },
                "attachments_count": len(files.get(t.id, [])),
                "created_at": pc.iso(t.created_at), "updated_at": pc.iso(t.updated_at), "last_author_reply_at": pc.iso(t.last_author_reply_at),
                "last_staff_reply_at": pc.iso(t.last_staff_reply_at), "team_mail": mail_shown(t.team_mail_status, t.created_at),
            }
        )
    return out


def team_card(db: Session, t: SupportTicket, history: List[dict]) -> dict:
    item = team_items(db, [t])[0]
    messages = db.query(SupportMessage).filter(SupportMessage.ticket_id == t.id).order_by(SupportMessage.created_at, SupportMessage.id).all()
    staff_ids = {m.author_user_id for m in messages if m.author_kind == "staff" and m.author_user_id}
    staff = {u.id: full_name(u) for u in db.query(User).filter(User.id.in_(staff_ids)).all()} if staff_ids else {}
    resolver = db.get(User, t.resolved_by) if t.resolved_by else None
    item.update(
        {
            "text": t.text, "page_template": t.page_template, "browser": t.browser, "client_errors": list(t.client_errors or []),
            "resolved_at": pc.iso(t.resolved_at), "resolved_by": ({"id": str(resolver.id), "name": full_name(resolver)} if resolver else None),
            "attachments": attachments_of(db, [t.id])[t.id],
            "messages": [
                {
                    "id": str(m.id), "from": "author" if m.author_kind == "user" else "staff", "text": m.text, "created_at": pc.iso(m.created_at),
                    "staff_name": staff.get(m.author_user_id) if m.author_kind == "staff" else None, "mail": mail_shown(m.mail_status, m.mail_status_at or m.created_at),
                }
                for m in messages
            ],
            "history": history,
        }
    )
    return item


def team_ticket(db: Session, ticket_id: str, *, lock: bool = False) -> SupportTicket:
    tid = parse_uuid_or_404(ticket_id, "Обращение")
    q = db.query(SupportTicket).filter(SupportTicket.id == tid)
    if lock:
        q = q.with_for_update()
    ticket = q.first()
    if ticket is None:
        raise not_found()
    return ticket


def add_staff_reply(db: Session, staff_user_id, ticket: SupportTicket, body: Any) -> tuple[SupportMessage, bool, bool]:
    """(message, created, author_has_access). The answer is in the cabinet whatever happens to the letter; the ticket is «Ждём ответа»."""
    form = Form(body)
    text_ = form.text("text", "Ответ", min_len=1, max_len=rules.STAFF_REPLY_MAX, multiline=True)
    form.done()
    moment = now()
    same = (
        db.query(SupportMessage)
        .filter(SupportMessage.ticket_id == ticket.id, SupportMessage.author_kind == "staff", SupportMessage.author_user_id == staff_user_id, SupportMessage.text == text_,
                SupportMessage.created_at > moment - timedelta(seconds=rules.DEDUP_REPLY_SECONDS))
        .first()
    )
    if same is not None:  # a second click on «Ответить»
        return same, False, True
    has_access = access_of(db, [ticket])[ticket.id] is None
    message = SupportMessage(
        ticket_id=ticket.id, author_kind="staff", author_user_id=staff_user_id, text=text_, created_at=moment,
        mail_status="pending" if has_access else "skipped", mail_status_at=None if has_access else moment,
    )
    db.add(message)
    ticket.status = "waiting"
    ticket.last_staff_reply_at = ticket.updated_at = moment
    ticket.resolved_at = ticket.resolved_by = None
    db.flush()
    return message, True, has_access


def set_status(db: Session, staff_user_id, ticket: SupportTicket, new_status: str) -> Optional[str]:
    """The previous status when it changed, None when it was the same."""
    old = ticket.status
    if old == new_status:
        return None
    moment = now()
    ticket.status = new_status
    ticket.updated_at = moment
    if new_status == "resolved":
        ticket.resolved_at, ticket.resolved_by = moment, staff_user_id
    else:
        ticket.resolved_at = ticket.resolved_by = None
    db.flush()
    return old


def counts(db: Session) -> Dict[str, int]:
    out = {s: 0 for s in ("new", "in_progress", "waiting", "resolved")}
    for status, n in db.query(SupportTicket.status, func.count()).group_by(SupportTicket.status).all():
        out[status] = n
    out["all"] = sum(out.values())
    return out


def search_clause(q: str):
    """A number (``12``, ``#12``, ``№12``) or words of the text / the author's name."""
    like = pc.contains(q)
    clauses = [SupportTicket.text.ilike(like, escape="\\"), SupportTicket.author_name.ilike(like, escape="\\")]
    digits = q.strip().lstrip("#№").strip()
    if digits.isdigit() and len(digits) <= 9:
        clauses.append(SupportTicket.number == int(digits))
    return or_(*clauses)
