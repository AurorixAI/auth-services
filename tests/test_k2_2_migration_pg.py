"""Revision ``k2b1b2c3d4e5`` (K2-2): the status of an invitation's letter, ``email_changes``, the journal actions, no ``superadmin``. The new CHECKs are added
``NOT VALID`` and then validated (LESSONS 26); up / down / up; a ``superadmin`` in the database stops the upgrade with a clear text."""
import pytest
from alembic import command
from sqlalchemy import inspect, text

from tests.test_k2_1_migration_pg import BASE_URL, config, engine, version  # noqa: F401  (engine is a fixture)

REVISION, BEFORE = "k2b1b2c3d4e5", "k2a1b2c3d4e5"
pytestmark = pytest.mark.skipif(not BASE_URL, reason="Requires isolated a5_auth_migrations PostgreSQL")


def checks(engine, table):
    with engine.connect() as c:
        return {row.conname: (row.convalidated, row.definition) for row in c.execute(text(
            "SELECT conname, convalidated, pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) AND contype = 'c'"), {"t": table})}


def test_up_down_up_the_checks_are_validated_and_the_role_comes_and_goes(engine):
    cfg = config()
    command.upgrade(cfg, "head")
    assert version(engine) == REVISION
    for table, name, needle in (("users", "users_role_check", "'teacher'"), ("student_audit", "ck_student_audit_actor_role", "'teacher'"), ("group_audit", "ck_group_audit_actor_role", "'teacher'"),
                                ("invitations", "ck_invitations_mail_status", "'failed'"), ("invitations", "invitations_status_check", "'queued'"),
                                ("member_audit", "ck_member_audit_action", "'email_changed'")):
        valid, definition = checks(engine, table)[name]
        assert valid is True and needle in definition and "superadmin" not in definition, (table, name, definition)   # validated, and without the role
    assert {"mail_status", "mail_status_at"} <= {c["name"] for c in inspect(engine).get_columns("invitations")}
    assert "email_changes" in inspect(engine).get_table_names()
    command.downgrade(cfg, BEFORE)
    assert version(engine) == BEFORE
    assert "superadmin" in checks(engine, "users")["users_role_check"][1] and "email_changes" not in inspect(engine).get_table_names()
    assert "mail_status" not in {c["name"] for c in inspect(engine).get_columns("invitations")}
    assert checks(engine, "users")["users_role_check"][0] is True                                   # validated after the downgrade too
    command.upgrade(cfg, "head")
    assert version(engine) == REVISION and "superadmin" not in checks(engine, "users")["users_role_check"][1]
    command.upgrade(cfg, "head")                                                                     # repeating is harmless


def test_a_superadmin_in_the_database_stops_the_upgrade_with_a_clear_text(engine):
    cfg = config()
    command.upgrade(cfg, BEFORE)
    with engine.begin() as c:
        c.execute(text("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check"))
        c.execute(text("INSERT INTO users (id, email, password_hash, first_name, last_name, role, must_change_password) VALUES (gen_random_uuid(), 'sa@x.test', 'x', 'A', 'B', 'superadmin', false)"))
    with pytest.raises(Exception) as stopped:
        command.upgrade(cfg, "head")
    assert "superadmin" in str(stopped.value) and version(engine) == BEFORE
