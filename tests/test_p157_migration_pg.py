"""Revision ``p157a1b2c3d4`` (#157): the CHECK of ``lesson_substitution_events.action`` accepts ``lesson_restored`` and ``day_off_voided``; up, down, up again; the models agree."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from src.core.db_timezone import utc_connect_args

BASE_URL = os.environ.get("A5_AUTH_MIGRATION_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not BASE_URL, reason="Requires isolated a5_auth_migrations PostgreSQL")

REVISION = "p157a1b2c3d4"
BEFORE = "w1a2b3c4d5e"
DB_NAME = "a5_auth_p157_migrations"
NEW_ACTIONS = ("lesson_restored", "day_off_voided")


@pytest.fixture
def engine(monkeypatch):
    base = make_url(BASE_URL)
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT", connect_args=utc_connect_args(BASE_URL))
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {DB_NAME}"))
    url = base.set(database=DB_NAME).render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)
    eng = create_engine(url, connect_args=utc_connect_args(url))
    yield eng
    eng.dispose()
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)"))
    admin.dispose()


def config():
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    return cfg


def version(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def seed(engine):
    """An organisation, a user, a group, one request - Core inserts (only the columns given)."""
    from src.models.auth import Group, Organization, User

    org, group, user, request = uuid4(), uuid4(), uuid4(), uuid4()
    with engine.begin() as c:
        c.execute(Organization.__table__.insert().values(id=org, name="Центр", status="active"))
        c.execute(User.__table__.insert().values(id=user, email=f"{user}@p157.invalid", password_hash="x", first_name="А", last_name="Б", role="owner"))
        c.execute(Group.__table__.insert().values(id=group, org_id=org, name="Группа", class_level=9))
        c.execute(text("INSERT INTO lesson_substitution_requests (id, org_id, group_id, lesson_id, lesson_date, start_time, end_time, starts_at, requester_id, origin, batch_id, reason_code, status, "
                       "created_at, updated_at) VALUES (:i, :o, :g, 'l1', '2026-11-02', '10:00', '11:00', '2026-11-02 05:00', :u, 'teacher', :b, 'sick', 'pending', now(), now())"),
                  {"i": request, "o": org, "g": group, "u": user, "b": uuid4()})
    return org, request


def add_event(engine, org, request, action):
    with engine.begin() as c:
        c.execute(text("INSERT INTO lesson_substitution_events (id, request_id, org_id, action, snapshot, created_at) VALUES (gen_random_uuid(), :r, :o, :a, '{}', now())"),
                  {"r": request, "o": org, "a": action})


def test_up_down_up_the_two_actions_are_accepted_then_refused(engine):
    cfg = config()
    command.upgrade(cfg, "head")
    assert version(engine) == REVISION
    org, request = seed(engine)
    for action in NEW_ACTIONS:
        add_event(engine, org, request, action)
    command.downgrade(cfg, BEFORE)
    assert version(engine) == BEFORE
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM lesson_substitution_events WHERE action IN ('lesson_restored', 'day_off_voided')")).scalar_one() == 0      # information only: gone with it
    with pytest.raises(IntegrityError):
        add_event(engine, org, request, "lesson_restored")                          # the old CHECK is back
    add_event(engine, org, request, "closed")                                       # an old action still goes
    command.downgrade(cfg, BEFORE)                                                  # repeated: harmless
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")                                                    # repeated: harmless
    assert version(engine) == REVISION
    add_event(engine, org, request, "day_off_voided")


def test_the_check_is_validated_and_the_model_agrees(engine):
    from src.models.auth import LessonSubstitutionEvent

    command.upgrade(config(), "head")
    with engine.connect() as c:
        assert c.execute(text("SELECT convalidated FROM pg_constraint WHERE conname='ck_lse_action'")).scalar_one() is True
        definition = c.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_lse_action'")).scalar_one()
    model = " ".join(str(c.sqltext) for c in LessonSubstitutionEvent.__table__.constraints if getattr(c, "name", None) == "ck_lse_action")
    for action in NEW_ACTIONS + ("requested", "closed", "assignment_cleared", "reopened"):
        assert f"'{action}'" in definition and f"'{action}'" in model, action
