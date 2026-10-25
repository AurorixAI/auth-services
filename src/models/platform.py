"""Tables of the platform console (pack «Консоль-1») and of the public demo request.

``demo_requests`` / ``demo_request_notes``  the «Запросить демо» form and what the team does with it
``platform_roles`` / ``platform_staff``      who may use the console and what a role allows (permissions are DATA)
``platform_sessions``                        one row per console sign-in (12 h); revocable
``platform_audit``                           every change made in the console and every console sign-in attempt
``password_links``                           one-time «Задать пароль» (centre_invite, 72 h) / «Сброс пароля» (reset, 1 h) links

Every moment is naive UTC (TIME_CONVENTIONS.md, group "UTC"). Secrets (passwords, link tokens) are never stored in clear:
``password_links.token_hash`` is a SHA-256, and no password or token ever reaches ``platform_audit``.
"""

from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from src.models.base import Base

DEMO_REQUEST_STATUSES = ("new", "contacted", "demo_done", "approved", "declined", "spam")
PASSWORD_LINK_PURPOSES = ("centre_invite", "reset")


class DemoRequest(Base):
    """A request for access from the public form. ``is_repeat``: the same e-mail asked within the last 30 days."""

    __tablename__ = "demo_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    contact_name = Column(String(120), nullable=False)
    centre_name = Column(String(200), nullable=False)
    city = Column(String(100), nullable=False)
    phone = Column(String(30), nullable=False)
    email = Column(String(255), nullable=False)
    students_count = Column(Integer)
    comment = Column(Text)
    source = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    ip_hash = Column(String(64))  # SHA-256 of the client address bucket: enough to see a flood, no address kept
    status = Column(String(20), nullable=False, default="new", server_default="new")
    decline_reason = Column(Text)
    is_repeat = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    repeat_of_id = Column(UUID(as_uuid=True))
    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL"))
    handled_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())
    updated_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('new', 'contacted', 'demo_done', 'approved', 'declined', 'spam')", name="ck_demo_requests_status"
        ),
        Index("ix_demo_requests_status_created", "status", "created_at"),
        Index("ix_demo_requests_email", "email"),
    )


class DemoRequestNote(Base):
    __tablename__ = "demo_request_notes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id = Column(UUID(as_uuid=True), ForeignKey("demo_requests.id", ondelete="CASCADE"), nullable=False)
    author_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())

    __table_args__ = (Index("ix_demo_request_notes_request", "request_id", "created_at"),)


class PlatformRole(Base):
    """A console role. ``permissions`` is a JSON list of permission keys (the catalogue lives in ``platform_permissions``)."""

    __tablename__ = "platform_roles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String(80), nullable=False, unique=True)
    permissions = Column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    is_template = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    is_system = Column(Boolean, nullable=False, default=False, server_default=text("false"))  # «Владелец платформы»: not editable
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())
    updated_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now(), onupdate=func.now())


class PlatformStaff(Base):
    """A person who may sign in to the console. The account itself is a ``users`` row (role ``teacher``, no centre): the
    cabinets refuse it, the console accepts it only when this row exists and is active."""

    __tablename__ = "platform_staff"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    role_id = Column(UUID(as_uuid=True), ForeignKey("platform_roles.id"), nullable=False)
    is_owner = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    must_change_password = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    failed_logins = Column(Integer, nullable=False, default=0, server_default=text("0"))
    locked_until = Column(DateTime)
    last_login_at = Column(DateTime)
    totp_enabled = Column(Boolean, nullable=False, default=False, server_default=text("false"))  # 2FA is not built yet
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())
    updated_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now(), onupdate=func.now())


class PlatformSession(Base):
    """One console sign-in. The cookie JWT names it (``sid``); a revoked or expired row closes the session at once."""

    __tablename__ = "platform_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime)
    revoke_reason = Column(String(40))  # logout | logout_all | password_changed | staff_disabled | password_reset
    ip = Column(String(64))
    user_agent = Column(String(200))

    __table_args__ = (Index("ix_platform_sessions_user_active", "user_id", postgresql_where=text("revoked_at IS NULL")),)


class PlatformAudit(Base):
    """What the team did (and who tried to sign in). Never a password or a token."""

    __tablename__ = "platform_audit"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    actor_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    action = Column(String(60), nullable=False)
    target_type = Column(String(30))
    target_id = Column(String(64))
    reason = Column(Text)
    details = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    ip = Column(String(64))
    user_agent = Column(String(200))
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())

    __table_args__ = (
        Index("ix_platform_audit_created", "created_at"),
        Index("ix_platform_audit_target", "target_type", "target_id", "created_at"),
        Index("ix_platform_audit_actor", "actor_user_id", "created_at"),
    )


class PasswordLink(Base):
    """A one-time link that sets a password. At most one LIVE link per user (a new one revokes the old: ``revoked_at``)."""

    __tablename__ = "password_links"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    purpose = Column(String(20), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime)
    revoked_at = Column(DateTime)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())

    __table_args__ = (
        CheckConstraint("purpose IN ('centre_invite', 'reset')", name="ck_password_links_purpose"),
        Index("ix_password_links_user", "user_id", "created_at"),
    )
