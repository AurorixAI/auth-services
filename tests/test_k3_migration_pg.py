"""Revision ``k3a1b2c3d4e5`` (K3, A1): the steps of the demo request.  Only adds: the old rows live through up, down and up; the new columns are nullable;
``request_id`` is unique where set; ``org_type`` is checked; a database built from the models and stamped back upgrades."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from src.core.db_timezone import utc_connect_args

BASE_URL = os.environ.get("A5_AUTH_MIGRATION_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not BASE_URL, reason="Requires isolated a5_auth_migrations PostgreSQL")

REVISION, BEFORE = "k3a1b2c3d4e5", "p159a1b2c3d4"
HEAD = "k4a1b2c3d4e5"   # K4a (the support) sits on top of K2-2, which sits on K2-1 (the mode of a session) on top of this revision
DB_NAME = "a5_auth_k3_migrations"
NEW = {
    "org_type", "address", "branches_count", "position", "preferred_contact_time", "needs", "current_tool", "heard_from", "locale", "consent_at",
    "request_id", "team_mail_status", "applicant_mail_status",
}


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


def columns(engine):
    return {c["name"]: c for c in inspect(engine).get_columns("demo_requests")}


def old_row(engine, email="old@k3.test"):
    with engine.begin() as c:
        c.execute(
            text("INSERT INTO demo_requests (id, contact_name, centre_name, city, phone, email, status) VALUES (:i, 'Иван', 'Центр', 'Ташкент', '+998901234567', :e, 'new')"),
            {"i": uuid4(), "e": email},
        )


def test_the_revision_follows_159_and_is_the_head():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config())
    assert script.get_heads() == [HEAD] and script.get_revision(REVISION).down_revision == BEFORE and script.get_revision('k2a1b2c3d4e5').down_revision == REVISION


def test_up_down_up_old_rows_survive_and_the_columns_are_nullable(engine):
    cfg = config()
    command.upgrade(cfg, BEFORE)
    assert not (NEW & set(columns(engine)))
    old_row(engine)
    command.upgrade(cfg, "head")
    assert version(engine) == HEAD
    cols = columns(engine)
    assert NEW <= set(cols) and all(cols[name]["nullable"] for name in NEW)
    with engine.connect() as c:
        row = c.execute(text("SELECT status, org_type, needs, request_id, team_mail_status, locale FROM demo_requests WHERE email = 'old@k3.test'")).one()
    assert tuple(row) == ("new", None, None, None, None, None)                                                             # the old row is untouched, the new fields empty
    command.downgrade(cfg, BEFORE)
    assert version(engine) == BEFORE and not (NEW & set(columns(engine)))
    command.downgrade(cfg, BEFORE)                                                                                         # repeated: harmless
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM demo_requests WHERE email = 'old@k3.test'")).scalar_one() == 1
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")                                                                                           # repeated: harmless
    assert version(engine) == HEAD and NEW <= set(columns(engine))


def test_request_id_is_unique_where_set_and_org_type_is_checked(engine):
    command.upgrade(config(), "head")

    def insert(email, **extra):
        keys = ", ".join(["id", "contact_name", "centre_name", "city", "phone", "email"] + list(extra))
        marks = ", ".join([":i", "'Иван'", "'Центр'", "'Ташкент'", "'+998901234567'", ":e"] + [f":{k}" for k in extra])
        with engine.begin() as c:
            c.execute(text(f"INSERT INTO demo_requests ({keys}) VALUES ({marks})"), {"i": uuid4(), "e": email, **extra})

    same = uuid4()
    insert("a@k3.test", request_id=same)
    with pytest.raises(IntegrityError):
        insert("b@k3.test", request_id=same)
    insert("c@k3.test")
    insert("d@k3.test")                                                                                                    # NULL request ids do not collide
    insert("e@k3.test", org_type="tutor")
    with pytest.raises(IntegrityError):
        insert("f@k3.test", org_type="bank")


def test_a_database_built_from_the_models_and_stamped_back_upgrades_without_failing(engine):
    from src.models.base import Base
    import src.models  # noqa: F401

    Base.metadata.create_all(engine)                                                                                       # the new columns, the index and the check are already there
    cfg = config()
    command.stamp(cfg, BEFORE)
    command.upgrade(cfg, "head")
    assert version(engine) == HEAD and NEW <= set(columns(engine))
