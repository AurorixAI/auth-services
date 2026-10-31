"""Tables of «Поддержка» (pack K4a): an employee of a centre writes to the ALGO team, the team answers in the console.

``support_tickets``      one request: the text, the fields the server took from the session, the page / browser / last errors (no personal data)
``support_messages``     the thread: the author's replies and the team's answers (``mail_status`` = the outcome of the letter that goes with it)
``support_attachments``  up to 3 screenshots per request (JPEG / PNG, <= 2 MB), kept in the database like the cheques (``bytea``)

Every moment is naive UTC. The author sees only his own requests; the team sees them with the right ``support.manage``.
"""
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Sequence,
    String,
    Text,
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from src.models.base import Base

SUPPORT_STATUSES = ("new", "in_progress", "waiting", "resolved")
SUPPORT_AUTHOR_KINDS = ("user", "staff")
SUPPORT_MAIL_STATUSES = ("pending", "sent", "failed", "skipped", "throttled")  # throttled: not sent on purpose - this ticket had a letter of this kind recently
SUPPORT_MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024
TICKET_NUMBER_SEQUENCE = "support_ticket_number_seq"


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    number = Column(Integer, Sequence(TICKET_NUMBER_SEQUENCE), nullable=False, unique=True)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    author_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    author_name = Column(String(200), nullable=False, default="", server_default="")  # a copy: the request outlives the account
    author_role = Column(String(20), nullable=False)  # the role in the centre at the moment of writing
    status = Column(String(20), nullable=False, default="new", server_default="new")
    text = Column(Text, nullable=False)
    page_template = Column(String(300), nullable=False, default="", server_default="")
    browser = Column(String(200), nullable=False, default="", server_default="")
    client_errors = Column(JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb"))
    client_key = Column(UUID(as_uuid=True))  # the client's idempotency key: the same key returns the same request
    team_mail_status = Column(String(10))  # the letter to the team about the new request
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())
    updated_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())
    last_author_reply_at = Column(DateTime)
    last_staff_reply_at = Column(DateTime)
    resolved_at = Column(DateTime)
    resolved_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))  # SET NULL when the employee of the console is deleted: «who» is lost, «how» is not
    resolved_kind = Column(String(10))  # manual (a person of the team) / auto (14 days without an answer); NULL while not resolved
    author_seen_at = Column(DateTime)  # the author opened the card at this moment: an answer of the team newer than this is «unread» (the badge of the menu)
    team_mail_at = Column(DateTime)  # the last letter to the team about this ticket (at most one an hour)
    author_mail_at = Column(DateTime)  # the last letter «есть ответ» to the author about this ticket (at most one in 30 minutes)

    __table_args__ = (
        CheckConstraint("status IN ('new', 'in_progress', 'waiting', 'resolved')", name="ck_support_tickets_status"),
        CheckConstraint("author_role IN ('owner', 'admin', 'teacher')", name="ck_support_tickets_author_role"),
        CheckConstraint("team_mail_status IS NULL OR team_mail_status IN ('pending', 'sent', 'failed', 'skipped', 'throttled')", name="ck_support_tickets_mail_status"),
        CheckConstraint("resolved_kind IS NULL OR resolved_kind IN ('manual', 'auto')", name="ck_support_tickets_resolved_kind"),
        Index("ix_support_tickets_author", "author_user_id", "created_at"),
        Index("ix_support_tickets_org", "org_id", "created_at"),
        Index("ix_support_tickets_status", "status", "updated_at"),
        Index("ux_support_tickets_client_key", "author_user_id", "client_key", unique=True, postgresql_where=sql_text("client_key IS NOT NULL")),
    )


class SupportMessage(Base):
    __tablename__ = "support_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    ticket_id = Column(UUID(as_uuid=True), ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False)
    author_kind = Column(String(10), nullable=False)
    author_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))  # the author, or the console employee for ``staff``
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())
    mail_status = Column(String(10))  # of a team answer: the letter to the author; of an author's reply: the letter to the team
    mail_status_at = Column(DateTime)

    __table_args__ = (
        CheckConstraint("author_kind IN ('user', 'staff')", name="ck_support_messages_author_kind"),
        CheckConstraint("mail_status IS NULL OR mail_status IN ('pending', 'sent', 'failed', 'skipped', 'throttled')", name="ck_support_messages_mail_status"),
        Index("ix_support_messages_ticket", "ticket_id", "created_at"),
    )


class SupportAttachment(Base):
    __tablename__ = "support_attachments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    ticket_id = Column(UUID(as_uuid=True), ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False)
    uploaded_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    content_type = Column(String(50), nullable=False)
    size = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False)
    data = Column(LargeBinary, nullable=False)
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())

    __table_args__ = (
        CheckConstraint("size > 0 AND size <= 2097152", name="ck_support_attachments_size"),
        CheckConstraint("content_type IN ('image/jpeg', 'image/png')", name="ck_support_attachments_type"),
        Index("ix_support_attachments_ticket", "ticket_id", "created_at"),
    )
