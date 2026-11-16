"""Revision ``mm21a1b2c3d4`` (Money-2): the baseline of the money rows (I-5), the counters of what the night did not check, three wider CHECKs; up / down / up."""
import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from src.core.db_timezone import utc_connect_args
from tests.test_k2_1_migration_pg import BASE_URL, config, version

DB_NAME = "a5_auth_money2_migrations"      # its OWN database (see ``test_money1_migration_pg``)

REVISION, BEFORE = "mm21a1b2c3d4", "mg13a1b2c3d4"
pytestmark = pytest.mark.skipif(not BASE_URL, reason="Requires isolated a5_auth_migrations PostgreSQL")
ORG, RUN = "00000000-0000-0000-0000-0000000000d1", "00000000-0000-0000-0000-0000000000e1"


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


def check(engine, name):
    with engine.connect() as c:
        return c.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"), {"n": name}).scalar()


def columns(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_the_revision_sits_on_top_of_the_one_of_the_production():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config())
    assert script.get_heads() == [REVISION] and script.get_revision(REVISION).down_revision == BEFORE


def test_up_down_up_the_baseline_the_counters_the_wider_checks_and_the_cascade(engine):
    cfg = config()
    command.upgrade(cfg, BEFORE)
    assert "money_row_baseline" not in inspect(engine).get_table_names() and "'student_transferred'" not in check(engine, "ck_group_audit_action")
    command.upgrade(cfg, REVISION)
    assert version(engine) == REVISION and "money_row_baseline" in inspect(engine).get_table_names()
    assert {"kind", "row_id", "org_id", "fingerprint", "amount", "voided", "void_fingerprint", "accepted", "first_run_id", "last_run_id", "first_seen_at", "last_confirmed_at"} <= columns(engine, "money_row_baseline")
    assert {"not_checked", "note"} <= columns(engine, "money_reconcile_runs") and "rows_checked" in columns(engine, "money_reconcile_centres")
    assert "'student_transferred'" in check(engine, "ck_group_audit_action") and "'payment_void'" in check(engine, "ck_group_audit_action")          # widened, nothing lost
    assert "'I-5'" in check(engine, "ck_money_reconcile_findings_invariant") and "'partial'" in check(engine, "ck_money_reconcile_runs_status") and "'findings'" in check(engine, "ck_money_reconcile_runs_status")
    with engine.begin() as c:
        c.execute(text(f"INSERT INTO organizations (id, name, org_type, status) VALUES ('{ORG}', 'M2', 'center', 'active')"))
        c.execute(text(f"INSERT INTO money_reconcile_runs (id, status, not_checked, note) VALUES ('{RUN}', 'partial', '{{\"payroll_no_fresh\": 1}}', 'x')"))
        c.execute(text(f"INSERT INTO money_reconcile_findings (id, run_id, org_id, invariant, subject_key) VALUES (gen_random_uuid(), '{RUN}', '{ORG}', 'I-5', 'payment:1:changed')"))
        c.execute(text(f"INSERT INTO money_row_baseline (kind, row_id, org_id, fingerprint, first_run_id) VALUES ('payment', gen_random_uuid(), '{ORG}', md5('x'), '{RUN}')"))
    with engine.begin() as c, pytest.raises(Exception):         # a row with no centre
        c.execute(text("INSERT INTO money_row_baseline (kind, row_id, org_id, fingerprint) VALUES ('payment', gen_random_uuid(), gen_random_uuid(), md5('x'))"))
    with engine.begin() as c:
        c.execute(text(f"DELETE FROM organizations WHERE id = '{ORG}'"))
        assert c.execute(text("SELECT count(*) FROM money_row_baseline")).scalar() == 0          # the baseline goes with the centre, like the rows
    command.downgrade(cfg, BEFORE)
    assert version(engine) == BEFORE and "money_row_baseline" not in inspect(engine).get_table_names()
    assert {"not_checked", "note"} <= columns(engine, "money_reconcile_runs")          # the columns and the wider checks stay: a row with a new value may be there
    command.upgrade(cfg, REVISION)
    command.upgrade(cfg, REVISION)                                # repeating is harmless
    assert version(engine) == REVISION and "money_row_baseline" in inspect(engine).get_table_names()
