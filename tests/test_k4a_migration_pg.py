"""Revision ``k4a1b2c3d4e5`` (K4a «Поддержка»): the three tables, the sequence of the numbers, the checks; up / down / up; the roles with the right; a database built from the models upgrades."""
import pytest
from alembic import command
from sqlalchemy import inspect, text

from tests.test_k2_1_migration_pg import BASE_URL, config, engine, version  # noqa: F401  (engine is a fixture)

REVISION, BEFORE = "k4a1b2c3d4e5", "k2b1b2c3d4e5"
TABLES = {"support_tickets", "support_messages", "support_attachments"}
pytestmark = pytest.mark.skipif(not BASE_URL, reason="Requires isolated a5_auth_migrations PostgreSQL")


def checks(engine, table):
    with engine.connect() as c:
        return {row.conname: row.definition for row in c.execute(text(
            "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) AND contype = 'c'"), {"t": table})}


def test_the_revision_follows_k2_2_and_is_the_head():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config())
    assert script.get_heads() == [REVISION] and script.get_revision(REVISION).down_revision == BEFORE


def test_up_down_up_the_tables_the_checks_and_the_sequence(engine):
    cfg = config()
    command.upgrade(cfg, "head")
    assert version(engine) == REVISION and TABLES <= set(inspect(engine).get_table_names())
    assert {"ck_support_tickets_status", "ck_support_tickets_author_role", "ck_support_tickets_mail_status"} <= set(checks(engine, "support_tickets"))
    assert "'resolved'" in checks(engine, "support_tickets")["ck_support_tickets_status"]
    assert {"ck_support_messages_author_kind", "ck_support_messages_mail_status"} <= set(checks(engine, "support_messages"))
    assert {"ck_support_attachments_size", "ck_support_attachments_type"} <= set(checks(engine, "support_attachments"))
    with engine.begin() as c:                                   # the numbers run on from the sequence even for a raw insert
        c.execute(text("INSERT INTO organizations (id, name, org_type, status) VALUES ('00000000-0000-0000-0000-0000000000a1', 'K4a', 'center', 'active')"))
        for i in range(2):
            c.execute(text("INSERT INTO support_tickets (id, org_id, author_role, text) VALUES (gen_random_uuid(), '00000000-0000-0000-0000-0000000000a1', 'teacher', 'text of the ticket')"))
        numbers = [r[0] for r in c.execute(text("SELECT number FROM support_tickets ORDER BY number"))]
        assert numbers == [numbers[0], numbers[0] + 1]
    with engine.begin() as c, pytest.raises(Exception):         # the CHECKs hold
        c.execute(text("INSERT INTO support_tickets (id, org_id, author_role, status, text) VALUES (gen_random_uuid(), '00000000-0000-0000-0000-0000000000a1', 'teacher', 'bogus', 'x')"))
    with engine.begin() as c, pytest.raises(Exception):
        c.execute(text("INSERT INTO support_attachments (id, ticket_id, content_type, size, sha256, data) SELECT gen_random_uuid(), id, 'text/html', 3, 'x', 'abc' FROM support_tickets LIMIT 1"))
    with engine.begin() as c, pytest.raises(Exception):
        c.execute(text("INSERT INTO support_attachments (id, ticket_id, content_type, size, sha256, data) SELECT gen_random_uuid(), id, 'image/png', 2097153, 'x', 'abc' FROM support_tickets LIMIT 1"))
    command.downgrade(cfg, BEFORE)
    assert version(engine) == BEFORE and not (TABLES & set(inspect(engine).get_table_names()))
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM pg_class WHERE relname = 'support_ticket_number_seq'")).scalar() == 0
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")                                # repeating is harmless
    assert version(engine) == REVISION and TABLES <= set(inspect(engine).get_table_names())


def test_the_template_roles_carry_the_right_and_the_migration_adds_it_where_it_is_missing(engine):
    cfg = config()
    command.upgrade(cfg, BEFORE)
    with engine.begin() as c:
        c.execute(text("""UPDATE platform_roles SET permissions = '["requests.view"]'::jsonb WHERE name IN ('Владелец платформы', 'Поддержка')"""))
    command.upgrade(cfg, "head")
    with engine.connect() as c:
        rows = {name: perms for name, perms in c.execute(text("SELECT name, permissions FROM platform_roles WHERE is_template"))}
    assert "support.manage" in rows["Владелец платформы"] and "support.manage" in rows["Поддержка"]
    assert "requests.view" in rows["Поддержка"]                                                 # what was there stays
    assert "support.manage" not in rows["Продажи"] and "support.manage" not in rows["Финансы"] and "support.manage" not in rows["Контент"]
    command.downgrade(cfg, BEFORE)
    command.upgrade(cfg, "head")                                                                 # a second time: no duplicate of the key
    with engine.connect() as c:
        perms = c.execute(text("SELECT permissions FROM platform_roles WHERE name = 'Поддержка'")).scalar_one()
    assert perms.count("support.manage") == 1


def test_a_database_built_from_the_models_and_stamped_back_upgrades_without_failing(engine):
    from src.models.base import Base
    import src.models  # noqa: F401

    Base.metadata.create_all(engine)                                                            # the tables are already there
    cfg = config()
    command.stamp(cfg, BEFORE)
    command.upgrade(cfg, "head")
    assert version(engine) == REVISION and TABLES <= set(inspect(engine).get_table_names())
