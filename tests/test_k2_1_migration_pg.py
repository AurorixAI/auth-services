"""Revision ``k2a1b2c3d4e5`` (K2-1, D): ``user_sessions.remember``. Only adds one NOT NULL column with a default; old rows get false; down drops it; a database built
from the models and stamped back upgrades; repeating is harmless."""
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


def old_session(engine):
    user, session = uuid4(), uuid4()
    with engine.begin() as c:
        c.execute(text("INSERT INTO users (id, email, password_hash, first_name, last_name, role, must_change_password) VALUES (:i, :e, 'x', 'A', 'B', 'teacher', false)"),
                  {"i": user, "e": f"{user}@k2.test"})
        c.execute(text("INSERT INTO user_sessions (id, user_id, created_at, last_used_at, expires_at) VALUES (:i, :u, now(), now(), now() + interval '30 days')"),
                  {"i": session, "u": user})
    return session


def test_the_revision_follows_k3_and_is_the_head():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config())
    assert script.get_heads() == [REVISION] and script.get_revision(REVISION).down_revision == BEFORE


def test_up_down_up_an_old_session_gets_false_and_the_column_is_not_null_with_a_default(engine):
    cfg = config()
    command.upgrade(cfg, BEFORE)
    assert "remember" not in session_columns(engine)
    session = old_session(engine)
    command.upgrade(cfg, "head")
    assert version(engine) == REVISION
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
    assert version(engine) == REVISION and "remember" in session_columns(engine)


def test_a_database_built_from_the_models_and_stamped_back_upgrades_without_failing(engine):
    from src.models.base import Base
    import src.models  # noqa: F401

    Base.metadata.create_all(engine)                                                            # the column is already there
    cfg = config()
    command.stamp(cfg, BEFORE)
    command.upgrade(cfg, "head")
    assert version(engine) == REVISION and "remember" in session_columns(engine)
