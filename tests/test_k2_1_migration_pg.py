"""Revision ``k2a1b2c3d4e5`` (K2-1): ``user_sessions.remember`` (NOT NULL, default false), the revocation of every live session (cookie cut-over), the invitation code as a hash,
the dead activation columns; up / down / up, repeating is harmless, a database built from the models and stamped back upgrades."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from src.core.db_timezone import utc_connect_args

BASE_URL = os.environ.get("A5_AUTH_MIGRATION_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not BASE_URL, reason="Requires isolated a5_auth_migrations PostgreSQL")

REVISION, BEFORE = "k2a1b2c3d4e5", "k3a1b2c3d4e5"
DB_NAME = "a5_auth_k2_migrations"


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


def session_columns(engine):
    return {c["name"]: c for c in inspect(engine).get_columns("user_sessions")}


def old_session(engine, user=None):
    user, session = user or uuid4(), uuid4()
    with engine.begin() as c:
        if not c.execute(text("SELECT 1 FROM users WHERE id = :i"), {"i": user}).first():
            c.execute(text("INSERT INTO users (id, email, password_hash, first_name, last_name, role, must_change_password) VALUES (:i, :e, 'x', 'A', 'B', 'teacher', false)"),
                      {"i": user, "e": f"{user}@k2.test"})
        c.execute(text("INSERT INTO user_sessions (id, user_id, created_at, last_used_at, expires_at) VALUES (:i, :u, now(), now(), now() + interval '30 days')"),
                  {"i": session, "u": user})
    return session


def old_invitation(engine, code, status="pending"):
    """An invitation of the OLD shape (the plain code in ``code``): needs an organisation and an inviter."""
    org, user = uuid4(), uuid4()
    with engine.begin() as c:
        c.execute(text("INSERT INTO users (id, email, password_hash, first_name, last_name, role, must_change_password) VALUES (:i, :e, 'x', 'A', 'B', 'owner', false)"),
                  {"i": user, "e": f"{user}@k2.test"})
        c.execute(text("INSERT INTO organizations (id, name, org_type, status) VALUES (:i, 'Old', 'center', 'active')"), {"i": org})
        c.execute(text("INSERT INTO invitations (id, org_id, invited_by, email, org_role, code, status, expires_at) VALUES (:i, :o, :u, 'old@x.uz', 'teacher', :c, :s, now() + interval '3 days')"),
                  {"i": uuid4(), "o": org, "u": user, "c": code, "s": status})


def to_old_shape(engine):
    """The chain builds the early tables from the CURRENT models, so at the revision before K2-1 the tables already have the new shape: put the old one back
    (the plain ``code`` of an invitation and the two activation columns), exactly as a production database that was never built from the models has them."""
    with engine.begin() as c:
        c.execute(text("ALTER TABLE invitations DROP COLUMN IF EXISTS code_hash"))
        c.execute(text("ALTER TABLE invitations ADD COLUMN IF NOT EXISTS code VARCHAR(20) NOT NULL DEFAULT ''"))
        c.execute(text("ALTER TABLE invitations ALTER COLUMN code DROP DEFAULT"))
        c.execute(text("ALTER TABLE invitations ADD CONSTRAINT invitations_code_key UNIQUE (code)"))
        c.execute(text("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS activation_token VARCHAR(500)"))
        c.execute(text("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS activation_token_expires_at TIMESTAMP"))


def table_columns(engine, table):
    return {c["name"]: c for c in inspect(engine).get_columns(table)}


def test_the_revision_follows_k3_and_k2_2_follows_it():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config())
    assert script.get_heads() == ["k2b1b2c3d4e5"] and script.get_revision("k2b1b2c3d4e5").down_revision == REVISION
    assert script.get_revision(REVISION).down_revision == BEFORE


def test_up_down_up_an_old_session_gets_false_and_the_column_is_not_null_with_a_default(engine):
    cfg = config()
    command.upgrade(cfg, BEFORE)
    assert "remember" not in session_columns(engine)
    session = old_session(engine)
    command.upgrade(cfg, "head")
    assert version(engine) == "k2b1b2c3d4e5"
    column = session_columns(engine)["remember"]
    assert column["nullable"] is False and "false" in str(column["default"]).lower()
    with engine.connect() as c:
        assert c.execute(text("SELECT remember FROM user_sessions WHERE id = :i"), {"i": session}).scalar_one() is False
    command.downgrade(cfg, BEFORE)
    assert version(engine) == BEFORE and "remember" not in session_columns(engine)
    command.downgrade(cfg, BEFORE)                                                              # repeated: harmless
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM user_sessions WHERE id = :i"), {"i": session}).scalar_one() == 1
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")                                                                # repeated: harmless
    assert version(engine) == "k2b1b2c3d4e5" and "remember" in session_columns(engine)


def test_m2_every_live_session_is_revoked_by_the_cookie_cut_over_and_an_old_revocation_is_kept(engine):
    cfg = config()
    command.upgrade(cfg, BEFORE)
    live, other = old_session(engine), old_session(engine)
    already = old_session(engine)
    with engine.begin() as c:
        c.execute(text("UPDATE user_sessions SET revoked_at = now() - interval '2 days', revoke_reason = 'logout' WHERE id = :i"), {"i": already})
    command.upgrade(cfg, "head")
    with engine.connect() as c:
        rows = {r.id: r for r in c.execute(text("SELECT id, revoked_at, revoke_reason FROM user_sessions"))}
    for sid in (live, other):
        assert rows[sid].revoked_at is not None and rows[sid].revoke_reason == "k2_cookie_switch"
    assert rows[already].revoke_reason == "logout"                                              # (an older, honest revocation is not rewritten)
    # the moment is naive UTC like every other: within a few minutes of "now" in UTC
    with engine.connect() as c:
        delta = c.execute(text("SELECT abs(extract(epoch FROM (revoked_at - (now() AT TIME ZONE 'utc')))) FROM user_sessions WHERE id = :i"), {"i": live}).scalar_one()
    assert delta < 300


def test_n3_n4_the_invitation_code_becomes_a_hash_pending_ones_are_cancelled_and_the_activation_columns_go(engine):
    import hashlib

    cfg = config()
    command.upgrade(cfg, BEFORE)
    to_old_shape(engine)
    assert {"activation_token", "activation_token_expires_at"} <= set(table_columns(engine, "organizations"))
    old_invitation(engine, "OLDCODE12345678", "pending")
    old_invitation(engine, "ACCEPTED123456789", "accepted")
    command.upgrade(cfg, "head")
    columns = table_columns(engine, "invitations")
    assert "code" not in columns and columns["code_hash"]["nullable"] is False
    assert not ({"activation_token", "activation_token_expires_at"} & set(table_columns(engine, "organizations")))
    with engine.connect() as c:
        rows = {r.code_hash: r.status for r in c.execute(text("SELECT code_hash, status FROM invitations"))}
        unique = c.execute(text("SELECT count(*) FROM pg_constraint WHERE conname = 'invitations_code_hash_key'")).scalar_one()
    assert rows[hashlib.sha256(b"OLDCODE12345678").hexdigest()] == "cancelled"                  # a pending test invitation is dead: its plain code is gone
    assert rows[hashlib.sha256(b"ACCEPTED123456789").hexdigest()] == "accepted" and unique == 1
    command.downgrade(cfg, BEFORE)                                                              # the SHAPE comes back (not the data)
    assert "code" in table_columns(engine, "invitations") and "code_hash" not in table_columns(engine, "invitations")
    assert {"activation_token", "activation_token_expires_at"} <= set(table_columns(engine, "organizations"))
    command.upgrade(cfg, "head")
    assert "code_hash" in table_columns(engine, "invitations")


def test_a_database_built_from_the_models_and_stamped_back_upgrades_without_failing(engine):
    from src.models.base import Base
    import src.models  # noqa: F401

    Base.metadata.create_all(engine)                                                            # the column is already there
    cfg = config()
    command.stamp(cfg, BEFORE)
    command.upgrade(cfg, "head")
    assert version(engine) == "k2b1b2c3d4e5" and "remember" in session_columns(engine)
