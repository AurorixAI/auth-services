"""Revision ``mn1a2b3c4d5`` (Money-1 pack A): the journal of reopened months, the nightly reconciliation tables, two new actions of the attendance audit; up / down / up."""
import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from src.core.db_timezone import utc_connect_args
from tests.test_k2_1_migration_pg import BASE_URL, config, version

DB_NAME = "a5_auth_money1_migrations"      # its OWN database: the fixture of the K2-1 file names one database for every file that imports it, and two files on two workers drop it under each other

REVISION, BEFORE = "mn1a2b3c4d5", "wca1b2c3d4e5"
TABLES = {"payroll_period_reopenings", "money_reconcile_runs", "money_reconcile_findings", "money_reconcile_snapshots", "money_reconcile_centres"}
pytestmark = pytest.mark.skipif(not BASE_URL, reason="Requires isolated a5_auth_migrations PostgreSQL")

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


def audit_check(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_group_attendance_audit_action'")).scalar()


def columns(engine):
    return {c["name"] for c in inspect(engine).get_columns("payroll_sheets")}


def test_the_revision_follows_wca_and_pack_b_sits_on_top_of_it():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config())
    assert script.get_heads() == ["mg12a1b2c3d4"] and script.get_revision(REVISION).down_revision == BEFORE and script.get_revision("mn1b2b3c4d5e").down_revision == REVISION


def test_up_down_up_the_tables_the_checks_and_the_audit_actions(engine):
    cfg = config()
    command.upgrade(cfg, REVISION)
    assert version(engine) == REVISION and TABLES <= set(inspect(engine).get_table_names())
    assert "confirmed_open_lessons" in columns(engine)                  # I-4: the lessons the owner confirmed on saving a sheet
    with engine.connect() as c:
        assert c.execute(text("SELECT convalidated FROM pg_constraint WHERE conname = 'ck_group_attendance_audit_action'")).scalar() is True        # R3 #9: added NOT VALID, then VALIDATED
    assert "'completed_reverted'" in audit_check(engine) and "'teacher_changed'" in audit_check(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO organizations (id, name, org_type, status) VALUES ('00000000-0000-0000-0000-0000000000b1', 'M1', 'center', 'active')"))
        c.execute(text("INSERT INTO money_reconcile_runs (id, status) VALUES ('00000000-0000-0000-0000-0000000000c1', 'findings')"))
        c.execute(text("INSERT INTO money_reconcile_findings (id, run_id, org_id, invariant, subject_key) VALUES (gen_random_uuid(), '00000000-0000-0000-0000-0000000000c1', "
                       "'00000000-0000-0000-0000-0000000000b1', 'I-1', 'm:1')"))
    with engine.begin() as c, pytest.raises(Exception):         # an invariant that does not exist
        c.execute(text("INSERT INTO money_reconcile_findings (id, run_id, org_id, invariant, subject_key) VALUES (gen_random_uuid(), '00000000-0000-0000-0000-0000000000c1', "
                       "'00000000-0000-0000-0000-0000000000b1', 'I-9', 'm:2')"))
    with engine.begin() as c, pytest.raises(Exception):         # a run in a state that does not exist
        c.execute(text("INSERT INTO money_reconcile_runs (id, status) VALUES (gen_random_uuid(), 'bogus')"))
    command.downgrade(cfg, BEFORE)
    assert version(engine) == BEFORE and not (TABLES & set(inspect(engine).get_table_names())) and "confirmed_open_lessons" not in columns(engine)
    assert "completed_reverted" not in audit_check(engine) and "'teacher_changed'" in audit_check(engine)
    command.upgrade(cfg, REVISION)
    command.upgrade(cfg, REVISION)                                # repeating is harmless
    assert version(engine) == REVISION and TABLES <= set(inspect(engine).get_table_names()) and "confirmed_open_lessons" in columns(engine)
