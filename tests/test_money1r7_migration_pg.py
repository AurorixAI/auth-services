"""Revision ``mg13a1b2c3d4`` (Money-1 R-7): the journal of the marks accepts ``excuse_reason_changed``; up / down / up - no record of the journal is lost by any step."""
import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from src.core.db_timezone import utc_connect_args
from tests.test_k2_1_migration_pg import BASE_URL, config, version
from tests.test_money1g_migration_pg import ATT, GROUP, ORG, USER, add_audit

DB_NAME = "a5_auth_money1r7_migrations"      # its OWN database (two files on two workers drop a shared one under each other)

REVISION, BEFORE = "mg13a1b2c3d4", "mg12a1b2c3d4"
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


def test_the_revision_is_the_head_and_follows_the_audit_key():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config())
    assert script.get_heads() == [REVISION] and script.get_revision(REVISION).down_revision == BEFORE


def actions(engine):
    with engine.connect() as c:
        return [r[0] for r in c.execute(text("SELECT action FROM group_attendance_audit ORDER BY action")).all()]


def test_up_down_up_the_new_action_is_accepted_and_no_record_is_lost(engine):
    cfg = config()
    command.upgrade(cfg, BEFORE)
    with engine.begin() as c:
        c.execute(text(f"INSERT INTO organizations (id, name, org_type, status) VALUES ('{ORG}', 'M1R7', 'center', 'active')"))
        c.execute(text(f"INSERT INTO users (id, email, password_hash, first_name, last_name, role, must_change_password) VALUES ('{USER}', 'r7@m1.invalid', 'x', 'A', 'B', 'teacher', false)"))
        c.execute(text(f"INSERT INTO groups (id, org_id, name, class_level, status, created_at, updated_at) VALUES ('{GROUP}', '{ORG}', 'G', 9, 'active', '2026-09-01 08:00', '2026-09-01 08:00')"))
        c.execute(text(f"INSERT INTO group_attendance (id, org_id, group_id, lesson_date, lesson_id, status, records) VALUES ('{ATT}', '{ORG}', '{GROUP}', '2026-10-26', 'lesson-l1', 'completed', '{{}}')"))
        add_audit(c, "corrected", ATT)
    with pytest.raises(IntegrityError):
        with engine.begin() as c:
            add_audit(c, "excuse_reason_changed", ATT)                      # the old CHECK refuses the new action
    command.upgrade(cfg, REVISION)
    assert version(engine) == REVISION and actions(engine) == ["corrected"]          # the old record is untouched
    with engine.begin() as c:
        add_audit(c, "excuse_reason_changed", ATT)
    command.downgrade(cfg, BEFORE)
    assert version(engine) == BEFORE and actions(engine) == ["corrected", "excuse_reason_changed"]      # the rollback deletes nothing
    command.upgrade(cfg, "head")
    assert version(engine) == REVISION and actions(engine) == ["corrected", "excuse_reason_changed"]
