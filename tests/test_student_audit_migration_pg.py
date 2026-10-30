"""``student_audit`` (IA fix pack, revision sa1b2c3d4e5): the migration is reversible (downgrade -> upgrade, repeated, stamped back) and the model has no drift
from it (same columns, nullability, defaults, index, check constraint).  Own disposable database."""
import os
import re
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

HEAD = "k2a1b2c3d4e5"  # the head of the chain (W1 on top of PAY-F, on top of the console pack); this file is about the revision sa1b2c3d4e5
BEFORE = "lb3c4d5e6f7"
DB_NAME = "a5_auth_studentaudit_migrations"


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


def _config():
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    return config


def _version(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def _norm(default):
    """``timezone('utc'::text, now())`` and ``timezone('utc', now())`` / ``'x'::character varying`` and ``x`` are the same default."""
    value = re.sub(r"::[a-z ]+(\[\])?", "", str(default)).replace("'", "").replace('"', "").replace(" ", "").lower()
    return value


def _seed(engine):
    from sqlalchemy.orm import Session

    from src.models.auth import Organization, Student, User

    ids = {k: uuid4() for k in ("org", "user", "student")}
    with Session(engine) as db:
        db.add(Organization(id=ids["org"], name="Sentinel", status="active"))
        db.add(User(id=ids["user"], email=f"{ids['user']}@m.invalid", password_hash="synthetic-only", first_name="A", last_name="B", role="owner"))
        db.flush()
        db.add(Student(id=ids["student"], org_id=ids["org"], first_name="S", last_name="T", class_level=9, status="active"))
        db.commit()
    return ids


def test_the_migration_is_reversible_downgrade_then_upgrade_repeated_and_stamped_back(engine):
    config = _config()
    command.upgrade(config, "head")
    assert _version(engine) == HEAD and "student_audit" in inspect(engine).get_table_names()
    ids = _seed(engine)
    with engine.begin() as c:                                                       # the defaults of the migration do their job
        c.execute(text("INSERT INTO student_audit (id, org_id, student_id, actor_id, actor_role, changes) VALUES (gen_random_uuid(), :o, :s, :u, 'owner', '{}'::jsonb)"),
                  {"o": ids["org"], "s": ids["student"], "u": ids["user"]})
    with engine.connect() as c:
        action, age = c.execute(text("SELECT action, abs(extract(epoch FROM (created_at - timezone('utc', now())))) FROM student_audit")).one()
    assert action == "profile_updated" and age < 120                                  # created_at is naive UTC like the app's own timestamps
    # downgrade: the table (and its index) goes, nothing else is touched; repeated is harmless
    command.downgrade(config, BEFORE)
    assert _version(engine) == BEFORE and "student_audit" not in inspect(engine).get_table_names()
    command.downgrade(config, BEFORE)
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM students WHERE id = :s"), {"s": ids["student"]}).scalar_one() == 1
        assert c.execute(text("SELECT count(*) FROM organizations WHERE id = :o"), {"o": ids["org"]}).scalar_one() == 1
    # upgrade again: the table is back (empty - the journal was dropped), with the index and the CHECK; a second upgrade changes nothing
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    insp = inspect(engine)
    assert _version(engine) == HEAD and "student_audit" in insp.get_table_names()
    assert "idx_student_audit_student" in {i["name"] for i in insp.get_indexes("student_audit")}
    assert "ck_student_audit_actor_role" in {c["name"] for c in insp.get_check_constraints("student_audit")}
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM student_audit")).scalar_one() == 0
    # a database built by create_all and stamped back: the revision finds the table in place and does not fail
    command.stamp(config, BEFORE)
    command.upgrade(config, "head")
    assert _version(engine) == HEAD
    # the foreign keys cascade: the journal goes with its student / centre
    with engine.begin() as c:
        c.execute(text("INSERT INTO student_audit (id, org_id, student_id, actor_id, actor_role, changes) VALUES (gen_random_uuid(), :o, :s, :u, 'admin', '{}'::jsonb)"),
                  {"o": ids["org"], "s": ids["student"], "u": ids["user"]})
        c.execute(text("DELETE FROM students WHERE id = :s"), {"s": ids["student"]})
        assert c.execute(text("SELECT count(*) FROM student_audit")).scalar_one() == 0


def test_the_model_has_no_drift_from_the_migration(engine):
    """Same columns, nullability, server defaults, index and CHECK on ``student_audit`` whether the schema comes from alembic or from ``create_all``."""
    from src.models.auth import StudentAudit

    command.upgrade(_config(), "head")
    insp = inspect(engine)
    live = {c["name"]: c for c in insp.get_columns("student_audit")}
    table = StudentAudit.__table__
    assert set(live) == set(table.columns.keys())
    for column in table.columns:
        got = live[column.name]
        assert got["nullable"] == column.nullable, column.name
        model_default = None if column.server_default is None else _norm(column.server_default.arg)
        live_default = None if got["default"] is None else _norm(got["default"])
        assert model_default == live_default, (column.name, model_default, live_default)
        assert type(got["type"]).__name__.upper().split("(")[0] in {type(column.type).__name__.upper(), "TIMESTAMP", "VARCHAR", "UUID", "JSONB"}
    assert table.columns["created_at"].server_default is not None and "utc" in str(table.columns["created_at"].server_default.arg)
    model_indexes = {(i.name, tuple(c.name for c in i.columns)) for i in table.indexes}
    assert model_indexes == {(i["name"], tuple(i["column_names"])) for i in insp.get_indexes("student_audit")}
    assert {c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"} == {c["name"] for c in insp.get_check_constraints("student_audit")}
    fks = {(fk["constrained_columns"][0], fk["referred_table"], fk["options"].get("ondelete")) for fk in insp.get_foreign_keys("student_audit")}
    assert fks == {(fk.parent.name, fk.column.table.name, fk.ondelete) for c in table.columns for fk in c.foreign_keys}
