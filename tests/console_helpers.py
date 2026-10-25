"""Fixtures of the platform-console tests (pack «Консоль-1»): the real application on the disposable ``a2_auth`` PostgreSQL.

``cw`` ("console world") gives a client that talks to the console host, the database session the application uses, an outbox that
captures every letter (no SMTP), a platform owner, and factories for staff, centres and cabinet users.
The console tables are emptied before each test; centres and users are created with unique e-mails and stay.
"""
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from passlib.context import CryptContext
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

import src.api.internal_router  # noqa: E402,F401  (import order: avoids a pre-existing router cycle)
from src.api import main_v2  # noqa: E402
from src.core import security  # noqa: E402
from src.core.database import get_db  # noqa: E402
from src.core.db_timezone import utc_connect_args  # noqa: E402
from src.models.auth import Organization, OrganizationMember, User  # noqa: E402
from src.models.platform import PlatformRole, PlatformStaff  # noqa: E402

DB_URL = os.getenv("A2_AUTH_TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(not DB_URL, reason="Requires disposable A2 auth PostgreSQL")

CONSOLE_HOST = "console.algolab.me"
CONSOLE = f"http://{CONSOLE_HOST}"
API = "/api/v1/platform"
PASSWORD = "correct-horse-battery-staple"
CONSOLE_TABLES = "password_links, demo_request_notes, demo_requests, platform_audit, platform_sessions, platform_staff, platform_roles"


@pytest.fixture
def cw(monkeypatch):
    from src.models.base import Base

    assert DB_URL and DB_URL.endswith("/a2_auth"), "Use only the disposable a2_auth database"
    engine = create_engine(DB_URL, connect_args=utc_connect_args(DB_URL))
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text(f"TRUNCATE {CONSOLE_TABLES} RESTART IDENTITY CASCADE"))
    session = sessionmaker(engine, autoflush=False)()

    monkeypatch.setattr(security, "_pwd_context", CryptContext(schemes=["bcrypt"], bcrypt__rounds=4))  # a test creates many accounts
    monkeypatch.setenv("CONSOLE_HOSTS", f"{CONSOLE_HOST},console.stage.algolab.me")
    monkeypatch.setenv("ADMIN_NOTIFICATION_EMAIL", "owner-notify@example.test")
    monkeypatch.setenv("FRONTEND_URL", "https://app.test")
    monkeypatch.setenv("CONSOLE_URL", "https://console.test")
    monkeypatch.setenv("MAIL_REPLY_TO", "hello@example.test")

    outbox = []

    def capture(msg, cfg=None):
        outbox.append(msg)
        return True

    from src.mail import sender

    monkeypatch.setattr(sender, "deliver", capture)

    def override():
        try:
            yield session
        except Exception:
            session.rollback()
            raise

    main_v2.app.dependency_overrides[get_db] = override
    clients = []

    def new_client(host=CONSOLE_HOST):
        client = TestClient(main_v2.app, base_url=f"http://{host}")
        clients.append(client)
        return client

    from src.auth import platform_permissions as perms

    perms.ensure_template_roles(session)
    session.commit()

    def make_user(email=None, password=PASSWORD, first="Тест", last="Сотрудник", role="teacher", active=True):
        user = User(
            email=email or f"{uuid4().hex[:12]}@cn1.test", password_hash=security.hash_password(password), first_name=first,
            last_name=last, role=role, is_active=active, is_verified=True,
        )
        session.add(user)
        session.flush()
        return user

    def make_staff(permissions=None, *, owner=False, must_change=False, active=True, email=None, password=PASSWORD, role_name=None):
        if owner:
            role = session.query(PlatformRole).filter(PlatformRole.is_system.is_(True)).one()
        else:
            role = PlatformRole(name=role_name or f"Роль {uuid4().hex[:6]}", permissions=list(permissions or []))
            session.add(role)
            session.flush()
        user = make_user(email=email, password=password, first="Сотр", last=("Владелец" if owner else "Команды"))
        staff = PlatformStaff(user_id=user.id, role_id=role.id, is_owner=owner, is_active=active, must_change_password=must_change)
        session.add(staff)
        session.commit()
        return SimpleNamespace(user=user, staff=staff, role=role, email=user.email, password=password)

    def make_centre(name=None, owner_password=PASSWORD, members=("owner", "admin", "teacher"), city="Ташкент"):
        org = Organization(name=name or f"Центр {uuid4().hex[:6]}", status="active", city=city, phone="+998901112233")
        session.add(org)
        session.flush()
        people = {}
        for org_role in members:
            u = make_user(password=owner_password, first=org_role.capitalize(), last="Центра", role=org_role if org_role != "teacher" else "teacher")
            u.default_org_id = org.id
            session.add(OrganizationMember(org_id=org.id, user_id=u.id, org_role=org_role))
            people[org_role] = u
        if "owner" in people:
            org.owner_id = people["owner"].id
        session.commit()
        return SimpleNamespace(org=org, **people, password=owner_password)

    def login(client, who, password=None):
        return client.post(f"{API}/auth/login", json={"email": who.email, "password": password or who.password})

    def signed_in(who):
        """A new client that is already signed in as ``who``."""
        client = new_client()
        r = login(client, who)
        assert r.status_code == 200, r.text
        return client

    world = SimpleNamespace(
        db=session, new_client=new_client, outbox=outbox, make_user=make_user, make_staff=make_staff, make_centre=make_centre,
        login=login, signed_in=signed_in, engine=engine,
    )
    world.owner = make_staff(owner=True, email=f"owner-{uuid4().hex[:8]}@cn1.test")
    world.client = signed_in(world.owner)
    try:
        yield world
    finally:
        main_v2.app.dependency_overrides.pop(get_db, None)
        for c in clients:
            c.close()
        session.close()
        engine.dispose()


def actions(db, **filters):
    """The ``action`` of every journal row (oldest first), optionally of one target."""
    from src.models.platform import PlatformAudit

    q = db.query(PlatformAudit).order_by(PlatformAudit.created_at, PlatformAudit.id)
    for key, value in filters.items():
        q = q.filter(getattr(PlatformAudit, key) == value)
    return [row.action for row in q.all()]


def audit_rows(db, action=None, **filters):
    from src.models.platform import PlatformAudit

    db.expire_all()
    q = db.query(PlatformAudit).order_by(PlatformAudit.created_at, PlatformAudit.id)
    if action:
        q = q.filter(PlatformAudit.action == action)
    for key, value in filters.items():
        q = q.filter(getattr(PlatformAudit, key) == value)
    return q.all()


def request_body(**over):
    body = {
        "contact_name": "Иванов Иван Иванович", "centre_name": "Учебный центр «Альфа»", "city": "Ташкент", "phone": "+998 90 123-45-67",
        "email": f"lead-{uuid4().hex[:8]}@example.test", "students_count": 120, "comment": "Хотим попробовать", "website": "",
        "source": {"utm_source": "telegram", "utm_medium": "post"},
    }
    body.update(over)
    return body


def letters_to(outbox, address):
    return [m for m in outbox if address in m["To"]]


def body_of(msg, kind="plain"):
    part = msg.get_body(preferencelist=(kind,))
    return part.get_content() if part is not None else None


def advance_clock(monkeypatch, seconds):
    """Move 'now' of the console code (``platform_common.utcnow``) and of the rate-limit windows forward by ``seconds``."""
    from datetime import datetime as real_datetime, timedelta

    from src.api import rate_limit
    from src.auth import platform_common

    class Later(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(seconds=seconds)

    monkeypatch.setattr(platform_common, "datetime", Later)
    real_monotonic = rate_limit._monotonic
    monkeypatch.setattr(rate_limit, "_monotonic", lambda: real_monotonic() + seconds)
