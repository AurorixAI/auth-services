"""Revision ``cn1a2b3c4d5`` (pack «Консоль-1»): up, down, up again; the template roles; no drift from the models; data of other tables survives."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from src.core.db_timezone import utc_connect_args

BASE_URL = os.environ.get("A5_AUTH_MIGRATION_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not BASE_URL, reason="Requires isolated a5_auth_migrations PostgreSQL")

HEAD = "k2b1b2c3d4e5"       # the head of the chain (W1, on top of PAY-F pf1a2b3c4d5 and the console pack cn1a2b3c4d5, which this file is about)
BEFORE = "sa1b2c3d4e5"
DB_NAME = "a5_auth_console_migrations"
NEW_TABLES = {"demo_requests", "demo_request_notes", "platform_roles", "platform_staff", "platform_sessions", "platform_audit", "password_links"}


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


def columns(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_up_down_up_and_the_template_roles(engine):
    cfg = config()
    command.upgrade(cfg, "head")
    assert version(engine) == HEAD and NEW_TABLES <= set(inspect(engine).get_table_names())
    assert {"blocked_at", "blocked_reason"} <= columns(engine, "organizations") and {"platform_blocked_at", "platform_block_reason", "platform_block_was_active"} <= columns(engine, "users")
    with engine.connect() as c:
        roles = {r.name: (r.permissions, r.is_template, r.is_system) for r in c.execute(text("SELECT name, permissions, is_template, is_system FROM platform_roles"))}
    assert set(roles) == {"Владелец платформы", "Продажи", "Поддержка", "Контент", "Финансы"} and all(v[1] for v in roles.values())
    assert roles["Владелец платформы"][2] is True and len(roles["Владелец платформы"][0]) == 17 and not any(v[2] for k, v in roles.items() if k != "Владелец платформы")
    assert roles["Контент"][0] == ["content.view"] and "team.manage" not in roles["Продажи"][0]

    # data of the old tables lives through the whole round trip
    org_id, user_id = uuid4(), uuid4()
    from src.models.auth import Organization, User

    with Session(engine) as db:
        db.add(Organization(id=org_id, name="Центр-свидетель", status="active"))
        db.add(User(id=user_id, email="witness@cn1.test", password_hash="x", first_name="А", last_name="Б", role="owner"))
        db.flush()
        db.execute(text("UPDATE organizations SET blocked_at = now(), blocked_reason = 'тест' WHERE id = :i"), {"i": org_id})
        db.commit()

    command.downgrade(cfg, BEFORE)
    assert version(engine) == BEFORE
    assert not (NEW_TABLES & set(inspect(engine).get_table_names()))
    assert not ({"blocked_at", "blocked_reason"} & columns(engine, "organizations")) and not ({"platform_blocked_at", "platform_block_reason", "platform_block_was_active"} & columns(engine, "users"))
    command.downgrade(cfg, BEFORE)                                                      # repeated: harmless
    with engine.connect() as c:
        assert c.execute(text("SELECT name FROM organizations WHERE id = :i"), {"i": org_id}).scalar_one() == "Центр-свидетель"
        assert c.execute(text("SELECT email FROM users WHERE id = :i"), {"i": user_id}).scalar_one() == "witness@cn1.test"

    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")                                                        # repeated: harmless
    assert version(engine) == HEAD and NEW_TABLES <= set(inspect(engine).get_table_names())
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM platform_roles")).scalar_one() == 5
        assert c.execute(text("SELECT blocked_at FROM organizations WHERE id = :i"), {"i": org_id}).scalar_one() is None   # the block mark went with the downgrade


def test_a_database_built_from_the_models_and_stamped_back_upgrades_without_failing(engine):
    from src.models.base import Base
    import src.models  # noqa: F401

    Base.metadata.create_all(engine)                                                    # tables AND the new columns already there
    cfg = config()
    command.stamp(cfg, BEFORE)
    command.upgrade(cfg, "head")
    assert version(engine) == HEAD
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM platform_roles WHERE is_template")).scalar_one() == 5


def test_the_models_and_the_migration_agree(engine):
    from src.models import platform as models

    command.upgrade(config(), "head")
    insp = inspect(engine)
    for model in (models.DemoRequest, models.DemoRequestNote, models.PlatformRole, models.PlatformStaff, models.PlatformSession, models.PlatformAudit, models.PasswordLink):
        table = model.__table__
        db_cols = {c["name"]: c for c in insp.get_columns(table.name)}
        assert set(db_cols) == {c.name for c in table.columns}, table.name
        for col in table.columns:
            assert db_cols[col.name]["nullable"] == col.nullable, (table.name, col.name)
        db_indexes = {i["name"] for i in insp.get_indexes(table.name)}
        assert {i.name for i in table.indexes} <= db_indexes, table.name
    checks = {c["name"] for c in insp.get_check_constraints("demo_requests")} | {c["name"] for c in insp.get_check_constraints("password_links")}
    assert {"ck_demo_requests_status", "ck_password_links_purpose"} <= checks
    assert [i for i in insp.get_unique_constraints("platform_staff") if i["column_names"] == ["user_id"]]


def test_foreign_keys_cascade_where_a_person_or_a_request_goes(engine):
    cfg = config()
    command.upgrade(cfg, "head")
    user_id, req_id = uuid4(), uuid4()
    with Session(engine) as db:
        role = db.execute(text("SELECT id FROM platform_roles WHERE is_system")).scalar_one()
        from src.models.auth import User

        db.add(User(id=user_id, email="gone@cn1.test", password_hash="x", first_name="А", last_name="Б", role="teacher"))
        db.flush()
        db.execute(text("INSERT INTO platform_staff (id, user_id, role_id) VALUES (:s, :u, :r)"), {"s": uuid4(), "u": user_id, "r": role})
        db.execute(text("INSERT INTO platform_sessions (id, user_id, created_at, expires_at) VALUES (:s, :u, now(), now())"), {"s": uuid4(), "u": user_id})
        db.execute(text("INSERT INTO platform_audit (id, actor_user_id, action) VALUES (:s, :u, 'console.login.success')"), {"s": uuid4(), "u": user_id})
        db.execute(text("INSERT INTO password_links (id, user_id, purpose, token_hash, expires_at) VALUES (:s, :u, 'reset', :h, now())"), {"s": uuid4(), "u": user_id, "h": "h" * 64})
        db.execute(text("INSERT INTO demo_requests (id, contact_name, centre_name, city, phone, email) VALUES (:i, 'А', 'Б', 'В', '+998', 'e@e.test')"), {"i": req_id})
        db.execute(text("INSERT INTO demo_request_notes (id, request_id, author_id, text) VALUES (:s, :r, :u, 'n')"), {"s": uuid4(), "r": req_id, "u": user_id})
        db.commit()
        db.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
        db.commit()
        for table, where in (("platform_staff", "user_id"), ("platform_sessions", "user_id"), ("password_links", "user_id")):
            assert db.execute(text(f"SELECT count(*) FROM {table} WHERE {where} = :i"), {"i": user_id}).scalar_one() == 0, table
        row = db.execute(text("SELECT actor_user_id FROM platform_audit")).one()
        assert row.actor_user_id is None                                                # the journal outlives the person
        assert db.execute(text("SELECT author_id FROM demo_request_notes")).scalar_one() is None
        db.execute(text("DELETE FROM demo_requests WHERE id = :i"), {"i": req_id})
        db.commit()
        assert db.execute(text("SELECT count(*) FROM demo_request_notes")).scalar_one() == 0
        with pytest.raises(Exception):
            db.execute(text("INSERT INTO demo_requests (id, contact_name, centre_name, city, phone, email, status) VALUES (:i, 'А','Б','В','+998','e@e.test','bogus')"), {"i": uuid4()})
            db.commit()
