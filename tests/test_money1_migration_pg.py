"""Revision ``mn1a2b3c4d5`` (Money-1 pack A): the journal of reopened months, the nightly reconciliation tables, two new actions of the attendance audit; up / down / up."""
import pytest
from alembic import command
from sqlalchemy import inspect, text

from tests.test_k2_1_migration_pg import BASE_URL, config, engine, version  # noqa: F401  (engine is a fixture)

REVISION, BEFORE = "mn1a2b3c4d5", "wca1b2c3d4e5"
TABLES = {"payroll_period_reopenings", "money_reconcile_runs", "money_reconcile_findings", "money_reconcile_snapshots", "money_reconcile_centres"}
pytestmark = pytest.mark.skipif(not BASE_URL, reason="Requires isolated a5_auth_migrations PostgreSQL")


def audit_check(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_group_attendance_audit_action'")).scalar()


def columns(engine):
    return {c["name"] for c in inspect(engine).get_columns("payroll_sheets")}


def test_the_revision_follows_wca_and_is_the_head():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config())
    assert script.get_heads() == [REVISION] and script.get_revision(REVISION).down_revision == BEFORE


def test_up_down_up_the_tables_the_checks_and_the_audit_actions(engine):
    cfg = config()
    command.upgrade(cfg, "head")
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
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")                                # repeating is harmless
    assert version(engine) == REVISION and TABLES <= set(inspect(engine).get_table_names()) and "confirmed_open_lessons" in columns(engine)
