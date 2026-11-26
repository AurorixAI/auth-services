"""Revisions ``lr1`` / ``lr2`` / ``lr3`` (LESSON-RULES): the kind ``unconfirmed`` of a cancellation, the event ``confirmed``, the group journal line ``lesson_added`` and the partial index of the audit;
up / down / up - nothing but what the revisions own is lost: a row with ``unconfirmed`` becomes a cancellation without a kind on the way down (neutral for the money), the events ``confirmed`` and the
``lesson_added`` lines go."""
import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from src.core.db_timezone import utc_connect_args
from tests.test_k2_1_migration_pg import BASE_URL, config, version
from tests.test_money1g_migration_pg import ATT, GROUP, ORG, USER

DB_NAME = "a5_auth_lesson_rules_migrations"
BEFORE, HEAD = "mm22a1b2c3d4", "lr3a2b3c4d5e"
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


def seed(c):
    c.execute(text(f"INSERT INTO organizations (id, name, org_type, status) VALUES ('{ORG}', 'LR', 'center', 'active')"))
    c.execute(text(f"INSERT INTO users (id, email, password_hash, first_name, last_name, role, must_change_password) VALUES ('{USER}', 'lr@m1.invalid', 'x', 'A', 'B', 'teacher', false)"))
    c.execute(text(f"INSERT INTO groups (id, org_id, name, class_level, status, created_at, updated_at) VALUES ('{GROUP}', '{ORG}', 'G', 9, 'active', '2026-09-01 08:00', '2026-09-01 08:00')"))
    c.execute(text(f"INSERT INTO group_attendance (id, org_id, group_id, lesson_date, lesson_id, status, records, cancellation_kind) VALUES ('{ATT}', '{ORG}', '{GROUP}', '2026-10-26', 'lesson-l1', 'cancelled', '{{}}', 'centre')"))


def set_kind(c, kind):
    c.execute(text("UPDATE group_attendance SET cancellation_kind = :k WHERE id = :a"), {"k": kind, "a": ATT})


def test_the_chain_ends_at_lr3():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config())
    assert script.get_heads() == [HEAD] and script.get_revision("lr1a2b3c4d5e").down_revision == BEFORE and script.get_revision("lr2a2b3c4d5e").down_revision == "lr1a2b3c4d5e"
    assert script.get_revision(HEAD).down_revision == "lr2a2b3c4d5e"


def test_up_down_up_the_kind_the_event_and_the_line_are_accepted_and_the_way_down_loses_only_its_own(engine):
    cfg = config()
    command.upgrade(cfg, BEFORE)
    with engine.begin() as c:
        seed(c)
    with pytest.raises(IntegrityError):
        with engine.begin() as c:
            set_kind(c, "unconfirmed")                                              # the old CHECK refuses the new kind
    command.upgrade(cfg, HEAD)
    assert version(engine) == HEAD
    with engine.begin() as c:
        set_kind(c, "unconfirmed")
        c.execute(text("INSERT INTO group_audit (id, org_id, group_id, actor_id, actor_role, action, snapshot, created_at) VALUES (gen_random_uuid(), :o, :g, :u, 'teacher', 'lesson_added', '{}', now())"),
                  {"o": ORG, "g": GROUP, "u": USER})
        c.execute(text("SELECT 1 FROM pg_indexes WHERE indexname = 'ix_gaa_org_done_corrected'")).scalar_one()
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_gaa_org_done_corrected'")).scalar() == 1
    command.downgrade(cfg, BEFORE)
    with engine.connect() as c:
        assert c.execute(text("SELECT cancellation_kind FROM group_attendance WHERE id = :a"), {"a": ATT}).scalar() is None          # unconfirmed -> no kind: neutral for the money
        assert c.execute(text("SELECT count(*) FROM group_audit WHERE action = 'lesson_added'")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_gaa_org_done_corrected'")).scalar() == 0
    command.upgrade(cfg, HEAD)
    assert version(engine) == HEAD
