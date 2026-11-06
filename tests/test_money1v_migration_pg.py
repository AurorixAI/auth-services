"""Revision ``mv1a1b2c3d4`` (Money-1 pack V1): the key of a repeated request of a forgiveness / a refund and the new actions of ``group_audit``; up / down / up.

The rollback is an emergency path on production and the journal of money decisions must not be lost: the downgrade keeps the rows of the new actions and only puts the OLD check back as ``NOT VALID``."""
import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from src.core.db_timezone import utc_connect_args
from tests.test_k2_1_migration_pg import BASE_URL, config, version

DB_NAME = "a5_auth_money1v_migrations"

REVISION, BEFORE = "mv1a1b2c3d4", "mn1b2b3c4d5e"
pytestmark = pytest.mark.skipif(not BASE_URL, reason="Requires isolated a5_auth_migrations PostgreSQL")
ORG, USER, GROUP = "00000000-0000-0000-0000-0000000000c1", "00000000-0000-0000-0000-0000000000c2", "00000000-0000-0000-0000-0000000000c3"


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


def columns(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def audit(c, action):
    c.execute(text("INSERT INTO group_audit (id, org_id, group_id, actor_id, actor_role, action, snapshot) "
                   f"VALUES (gen_random_uuid(), '{ORG}', '{GROUP}', '{USER}', 'owner', :a, '{{}}')"), {"a": action})


def test_the_revision_sits_on_pack_b_and_below_the_rest_of_pack_v():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config())
    assert script.get_revision(REVISION).down_revision == BEFORE and script.get_revision("mv6a1b2c3d4").down_revision == REVISION


def test_up_down_up_the_journal_of_money_decisions_is_never_lost(engine):
    cfg = config()
    command.upgrade(cfg, BEFORE)
    assert "idempotency_key" not in columns(engine, "student_writeoffs")
    with engine.begin() as c:
        c.execute(text(f"INSERT INTO organizations (id, name, org_type, status) VALUES ('{ORG}', 'M1V', 'center', 'active')"))
        c.execute(text(f"INSERT INTO users (id, email, password_hash, first_name, last_name, role, must_change_password) VALUES ('{USER}', 'v@m1v.invalid', 'x', 'A', 'B', 'owner', false)"))
        c.execute(text(f"INSERT INTO groups (id, org_id, name, class_level, status, created_at, updated_at) VALUES ('{GROUP}', '{ORG}', 'G', 9, 'active', '2026-09-01 08:00', '2026-09-01 08:00')"))
    with pytest.raises(IntegrityError):                                    # before the revision the action does not exist
        with engine.begin() as c:
            audit(c, "payment_void")
    command.upgrade(cfg, REVISION)
    assert version(engine) == REVISION and {"idempotency_key"} <= columns(engine, "student_writeoffs") and {"idempotency_key"} <= columns(engine, "student_refunds")
    with engine.begin() as c:
        audit(c, "payment_void")                                          # the new action is accepted
        audit(c, "payment_replace")
        audit(c, "payment_file_removed")
        audit(c, "payment_writeoff")                                      # and the old ones still are
    command.downgrade(cfg, BEFORE)
    assert version(engine) == BEFORE and "idempotency_key" not in columns(engine, "student_writeoffs")
    with engine.connect() as c:                                            # NOTHING IS DELETED: the three rows of the new actions stay
        assert c.execute(text("SELECT count(*) FROM group_audit WHERE action IN ('payment_void', 'payment_replace', 'payment_file_removed')")).scalar() == 3
        assert c.execute(text("SELECT convalidated FROM pg_constraint WHERE conname = 'ck_group_audit_action'")).scalar() is False       # the old check is NOT VALID
    with pytest.raises(IntegrityError):                                    # a new row of the new action is refused by the old list
        with engine.begin() as c:
            audit(c, "payment_void")
    with engine.begin() as c:
        audit(c, "payment_writeoff")                                      # an old action is fine
    command.upgrade(cfg, REVISION)
    with engine.begin() as c:
        audit(c, "payment_void")                                          # the new action is accepted again
        assert c.execute(text("SELECT count(*) FROM group_audit WHERE action = 'payment_void'")).scalar() == 2
        assert c.execute(text("SELECT convalidated FROM pg_constraint WHERE conname = 'ck_group_audit_action'")).scalar() is True
