"""Revision ``mn1b2b3c4d5e`` (Money-1 pack B): ``student_group_periods.billing_from`` and the journal ``org_money_audit``; up / down / up with the data of the column kept across the up and cleaned by the down."""
import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from src.core.db_timezone import utc_connect_args
from tests.test_k2_1_migration_pg import BASE_URL, config, version

DB_NAME = "a5_auth_money1b_migrations"      # its OWN database: the fixture of the K2-1 file names one database for every file that imports it, and two files on two workers drop it under each other

REVISION, BEFORE = "mn1b2b3c4d5e", "mn1a2b3c4d5"
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


def test_the_revision_is_the_head_and_follows_pack_a():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config())
    assert script.get_heads() == ["mv5a1b2c3d4"] and script.get_revision("mv1a1b2c3d4").down_revision == REVISION and script.get_revision(REVISION).down_revision == BEFORE


def period_columns(engine):
    return {c["name"] for c in inspect(engine).get_columns("student_group_periods")}


def test_up_down_up_the_column_and_the_journal_and_the_data_of_a_period_survive_the_cycle(engine):
    cfg = config()
    command.upgrade(cfg, BEFORE)
    assert "billing_from" not in period_columns(engine) and "org_money_audit" not in inspect(engine).get_table_names()
    with engine.begin() as c:                      # a stay that exists BEFORE the revision: it keeps its day (the column is empty = the day he joined)
        c.execute(text("INSERT INTO organizations (id, name, org_type, status) VALUES ('00000000-0000-0000-0000-0000000000b2', 'M1B', 'center', 'active')"))
        c.execute(text("INSERT INTO users (id, email, password_hash, first_name, last_name, role, must_change_password) VALUES ('00000000-0000-0000-0000-0000000000a2', 'b@m1b.invalid', 'x', 'A', 'B', 'teacher', false)"))
        c.execute(text("INSERT INTO groups (id, org_id, name, class_level, status, created_at, updated_at) VALUES ('00000000-0000-0000-0000-0000000000d2', '00000000-0000-0000-0000-0000000000b2', 'G', 9, 'active', "
                       "'2026-09-01 08:00', '2026-09-01 08:00')"))
        c.execute(text("INSERT INTO students (id, org_id, first_name, last_name, class_level, status) VALUES ('00000000-0000-0000-0000-0000000000e2', '00000000-0000-0000-0000-0000000000b2', 'S', 'T', 9, 'new')"))
        c.execute(text("INSERT INTO student_groups (id, student_id, group_id, is_active, joined_at) VALUES ('00000000-0000-0000-0000-0000000000f2', '00000000-0000-0000-0000-0000000000e2', "
                       "'00000000-0000-0000-0000-0000000000d2', true, '2026-10-20 06:00')"))
        c.execute(text("INSERT INTO student_group_periods (id, membership_id, joined_at) VALUES (gen_random_uuid(), '00000000-0000-0000-0000-0000000000f2', '2026-10-20 06:00')"))
    command.upgrade(cfg, REVISION)
    assert version(engine) == REVISION and "billing_from" in period_columns(engine) and "org_money_audit" in inspect(engine).get_table_names()
    with engine.begin() as c:
        assert c.execute(text("SELECT billing_from FROM student_group_periods")).scalar() is None                       # the old stay is untouched
        c.execute(text("UPDATE student_group_periods SET billing_from = '2026-11-05'"))
        c.execute(text("INSERT INTO org_money_audit (id, org_id, action, details) VALUES (gen_random_uuid(), '00000000-0000-0000-0000-0000000000b2', 'billing_regime_changed', '{\"a\": 1}')"))
        assert c.execute(text("SELECT count(*) FROM org_money_audit")).scalar() == 1
    command.downgrade(cfg, BEFORE)
    assert version(engine) == BEFORE and "billing_from" not in period_columns(engine) and "org_money_audit" not in inspect(engine).get_table_names()
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM student_group_periods")).scalar() == 1                             # the period itself stays: only its extra day is gone
    command.upgrade(cfg, "head")
    assert version(engine) == "mv5a1b2c3d4" and "billing_from" in period_columns(engine)
