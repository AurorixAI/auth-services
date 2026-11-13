"""Revision ``mg12a1b2c3d4`` (Money-1 G-12): the journal of the marks outlives the row of its lesson; up / down / up - no record of the journal is lost by any step."""
import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from src.core.db_timezone import utc_connect_args
from tests.test_k2_1_migration_pg import BASE_URL, config, version

DB_NAME = "a5_auth_money1g_migrations"      # its OWN database: the fixture of the K2-1 file names one database for every file that imports it, and two files on two workers drop it under each other

REVISION, BEFORE = "mg12a1b2c3d4", "mv5a1b2c3d4"
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


ORG, USER, GROUP, ATT = ("00000000-0000-0000-0000-0000000000%s" % x for x in ("b7", "a7", "d7", "c7"))


def test_the_revision_is_the_head_and_follows_the_receipt_prints():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config())
    assert script.get_heads() == [REVISION] and script.get_revision(REVISION).down_revision == BEFORE


def audit_state(engine):
    with engine.connect() as c:
        rows = c.execute(text("SELECT action, attendance_id::text, group_id::text FROM group_attendance_audit ORDER BY action")).all()
        rule = c.execute(text("SELECT confdeltype FROM pg_constraint WHERE conrelid = 'group_attendance_audit'::regclass AND contype = 'f' AND conkey = "
                              "ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid = 'group_attendance_audit'::regclass AND attname = 'attendance_id')]")).scalar()
        nullable = c.execute(text("SELECT is_nullable FROM information_schema.columns WHERE table_name = 'group_attendance_audit' AND column_name = 'attendance_id'")).scalar()
    return rows, rule, nullable


def add_audit(c, action, attendance_id, **key):
    c.execute(text("INSERT INTO group_attendance_audit (id, attendance_id, org_id, group_id, actor_id, action, after_status, after_records" + "".join(f", {k}" for k in key) + ") "
                   "VALUES (gen_random_uuid(), :att, :org, :grp, :usr, :action, 'cancelled', '{}'" + "".join(f", :{k}" for k in key) + ")"),
              {"att": attendance_id, "org": ORG, "grp": GROUP, "usr": USER, "action": action, **key})


def test_up_down_up_the_journal_of_a_deleted_lesson_stays_on_every_step(engine):
    cfg = config()
    command.upgrade(cfg, BEFORE)
    with engine.begin() as c:
        c.execute(text(f"INSERT INTO organizations (id, name, org_type, status) VALUES ('{ORG}', 'M1G', 'center', 'active')"))
        c.execute(text(f"INSERT INTO users (id, email, password_hash, first_name, last_name, role, must_change_password) VALUES ('{USER}', 'g@m1g.invalid', 'x', 'A', 'B', 'teacher', false)"))
        c.execute(text(f"INSERT INTO groups (id, org_id, name, class_level, status, created_at, updated_at) VALUES ('{GROUP}', '{ORG}', 'G', 9, 'active', '2026-09-01 08:00', '2026-09-01 08:00')"))
        c.execute(text(f"INSERT INTO group_attendance (id, org_id, group_id, lesson_date, lesson_id, status, records) VALUES ('{ATT}', '{ORG}', '{GROUP}', '2026-10-26', 'lesson-l1', 'cancelled', '{{}}')"))
        add_audit(c, "cancelled", ATT)                                          # a record written BEFORE the revision
    assert audit_state(engine)[1:] == ("c", "NO")
    command.upgrade(cfg, REVISION)
    assert version(engine) == REVISION
    rows, rule, nullable = audit_state(engine)
    assert (rule, nullable, rows) == ("n", "YES", [("cancelled", ATT, GROUP)])        # the old record is untouched
    with engine.begin() as c:
        add_audit(c, "corrected", ATT, lesson_date="2026-10-26", lesson_id="lesson-l1")
        c.execute(text(f"DELETE FROM group_attendance WHERE id = '{ATT}'"))             # the lesson goes back to the schedule
    rows, _, _ = audit_state(engine)
    assert rows == [("cancelled", None, GROUP), ("corrected", None, GROUP)]               # both records are on their place
    command.downgrade(cfg, BEFORE)
    assert version(engine) == BEFORE
    rows, rule, _ = audit_state(engine)
    assert rule == "c" and rows == [("cancelled", None, GROUP), ("corrected", None, GROUP)]    # the rollback deletes nothing and brings the old rule back
    with engine.connect() as c:
        assert c.execute(text("SELECT lesson_date FROM group_attendance_audit WHERE action = 'corrected'")).scalar() == "2026-10-26"      # nor does it forget the key
    command.upgrade(cfg, "head")
    assert version(engine) == REVISION
    rows, rule, nullable = audit_state(engine)
    assert (rule, nullable, len(rows)) == ("n", "YES", 2)
