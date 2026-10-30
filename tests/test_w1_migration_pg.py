"""Revision ``w1a2b3c4d5e`` (W1): up, down, up again; the live duplicates of a day off are merged before the unique index; the audit action ``lesson_restored``; no drift from the models."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from src.core.db_timezone import utc_connect_args

BASE_URL = os.environ.get("A5_AUTH_MIGRATION_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not BASE_URL, reason="Requires isolated a5_auth_migrations PostgreSQL")

REVISION = "w1a2b3c4d5e"
HEAD = "k2a1b2c3d4e5"          # the revision after it (#157: two new actions of the events); this file is about REVISION, ``upgrade head`` runs through it
BEFORE = "pf1a2b3c4d5"
DB_NAME = "a5_auth_w1_migrations"


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


def config():
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    return cfg


def version(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def columns(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def indexes(engine, table):
    return {i["name"] for i in inspect(engine).get_indexes(table)}


def world(engine):
    """An organisation, a user, a group (Core inserts: only the columns given)."""
    from src.models.auth import Group, Organization, User

    org, group, user = uuid4(), uuid4(), uuid4()
    with engine.begin() as c:
        c.execute(Organization.__table__.insert().values(id=org, name="Центр", status="active"))
        c.execute(User.__table__.insert().values(id=user, email=f"{user}@w1.invalid", password_hash="x", first_name="А", last_name="Б", role="owner"))
        c.execute(Group.__table__.insert().values(id=group, org_id=org, name="Группа", class_level=9))
    return org, group, user


def test_up_down_up_adds_and_drops_only_its_own(engine):
    cfg = config()
    command.upgrade(cfg, "head")
    assert version(engine) == HEAD
    assert "incomplete" in columns(engine, "org_days_off") and "uq_org_days_off_live_range" in indexes(engine, "org_days_off")
    assert "converted_from_kind" in columns(engine, "group_attendance")
    org, group, user = world(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO org_days_off (id, org_id, date_from, date_to, reason) VALUES (gen_random_uuid(), :o, '2026-10-28', '2026-10-28', 'Хайит')"), {"o": org})
        # the new audit action is accepted
        for action in ("lesson_restored", "lesson_cancelled"):                          # the new audit actions are accepted
            c.execute(text("INSERT INTO group_audit (id, org_id, group_id, actor_id, actor_role, action, snapshot) VALUES (gen_random_uuid(), :o, :g, :u, 'admin', :a, '{}')"),
                      {"o": org, "g": group, "u": user, "a": action})
        assert c.execute(text("SELECT incomplete FROM org_days_off WHERE org_id=:o"), {"o": org}).scalar_one() is False
    with pytest.raises(IntegrityError):                                      # one LIVE day off per range
        with engine.begin() as c:
            c.execute(text("INSERT INTO org_days_off (id, org_id, date_from, date_to, reason) VALUES (gen_random_uuid(), :o, '2026-10-28', '2026-10-28', 'Дубль')"), {"o": org})
    with engine.begin() as c:                                                # a voided one does not count, another range is another range
        c.execute(text("INSERT INTO org_days_off (id, org_id, date_from, date_to, reason, voided_at) VALUES (gen_random_uuid(), :o, '2026-10-28', '2026-10-28', 'Снят', now())"), {"o": org})
        c.execute(text("INSERT INTO org_days_off (id, org_id, date_from, date_to, reason) VALUES (gen_random_uuid(), :o, '2026-10-28', '2026-10-29', 'Другой')"), {"o": org})

    command.downgrade(cfg, BEFORE)
    assert version(engine) == BEFORE
    assert "incomplete" not in columns(engine, "org_days_off") and "uq_org_days_off_live_range" not in indexes(engine, "org_days_off")
    assert "converted_from_kind" not in columns(engine, "group_attendance")
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM group_audit WHERE action IN ('lesson_restored', 'lesson_cancelled')")).scalar_one() == 0      # the rows of the new actions go with it
        assert c.execute(text("SELECT count(*) FROM org_days_off WHERE org_id=:o"), {"o": org}).scalar_one() == 3          # the days off stay
    command.downgrade(cfg, BEFORE)                                           # repeated: harmless
    with pytest.raises(IntegrityError):                                      # the old CHECK is back
        with engine.begin() as c:
            c.execute(text("INSERT INTO group_audit (id, org_id, group_id, actor_id, actor_role, action, snapshot) VALUES (gen_random_uuid(), :o, :g, :u, 'admin', 'lesson_restored', '{}')"),
                      {"o": org, "g": group, "u": user})
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")                                             # repeated: harmless
    assert version(engine) == HEAD


def test_live_duplicates_of_a_range_are_merged_before_the_index(engine, capfd):
    cfg = config()
    command.upgrade(cfg, BEFORE)
    org, group, user = world(engine)
    first, second, third, other = uuid4(), uuid4(), uuid4(), uuid4()
    with engine.begin() as c:
        for day_off, reason, created, voided in ((first, "Первый", "2026-10-20 08:00", None), (second, "Второй", "2026-10-20 09:00", None), (third, "Третий", "2026-10-20 10:00", None),
                                                 (other, "Другие даты", "2026-10-20 11:00", None)):
            dates = ("2026-10-29", "2026-10-29") if day_off == other else ("2026-10-28", "2026-10-28")
            c.execute(text("INSERT INTO org_days_off (id, org_id, date_from, date_to, reason, created_at) VALUES (:i, :o, :a, :b, :r, :t)"),
                      {"i": day_off, "o": org, "a": dates[0], "b": dates[1], "r": reason, "t": created})
        # a voided duplicate of the same range stays as it is
        gone = uuid4()
        c.execute(text("INSERT INTO org_days_off (id, org_id, date_from, date_to, reason, created_at, voided_at) VALUES (:i, :o, '2026-10-28', '2026-10-28', 'Снят', '2026-10-19 08:00', now())"),
                  {"i": gone, "o": org})
        for day_off, lesson in ((second, "lesson-a"), (third, "lesson-b"), (first, "lesson-c"), (other, "lesson-d"), (gone, "lesson-e")):
            c.execute(text("INSERT INTO group_attendance (id, org_id, group_id, lesson_date, lesson_id, status, cancellation_kind, day_off_id, version) "
                           "VALUES (gen_random_uuid(), :o, :g, '2026-10-28', :l, 'cancelled', 'holiday', :d, 1)"), {"o": org, "g": group, "l": lesson, "d": day_off})
    command.upgrade(cfg, "head")
    out = capfd.readouterr()
    assert "merged 2 live duplicate day off(s)" in out.out + out.err and "2 cancelled lesson(s) re-pointed" in out.out + out.err           # the stage / prod output says how many
    with engine.connect() as c:
        live = {str(r[0]) for r in c.execute(text("SELECT id FROM org_days_off WHERE org_id=:o AND voided_at IS NULL"), {"o": org})}
        assert live == {str(first), str(other)}                              # the oldest of the range stays; the other range is untouched
        assert c.execute(text("SELECT voided_by FROM org_days_off WHERE id=:i"), {"i": second}).scalar_one() is None
        owner_of = dict(c.execute(text("SELECT lesson_id, day_off_id FROM group_attendance WHERE group_id=:g"), {"g": group}).all())
        assert owner_of == {"lesson-a": first, "lesson-b": first, "lesson-c": first, "lesson-d": other, "lesson-e": gone}      # the lessons follow the survivor, nothing is deleted


def test_a_database_built_from_the_models_and_stamped_back_upgrades_without_failing(engine):
    from src.models.base import Base
    import src.models  # noqa: F401

    Base.metadata.create_all(engine)                                          # the column and the index are already there
    cfg = config()
    command.stamp(cfg, BEFORE)
    command.upgrade(cfg, "head")
    assert version(engine) == HEAD and "incomplete" in columns(engine, "org_days_off")


def test_the_migration_and_the_models_agree(engine):
    from src.models.auth import OrgDayOff

    command.upgrade(config(), "head")
    assert {c.name for c in OrgDayOff.__table__.columns} == columns(engine, "org_days_off")
    with engine.connect() as c:
        definition = c.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname='uq_org_days_off_live_range'")).scalar_one()
        assert "UNIQUE" in definition and "(org_id, date_from, date_to)" in definition and "voided_at IS NULL" in definition
        assert c.execute(text("SELECT convalidated FROM pg_constraint WHERE conname='ck_group_audit_action'")).scalar_one() is True          # NOT VALID, then VALIDATEd
        assert "'lesson_cancelled'" in c.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_group_audit_action'")).scalar_one()
        assert "'lesson_restored'" in c.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_group_audit_action'")).scalar_one()
        assert c.execute(text("SELECT column_default FROM information_schema.columns WHERE table_name='org_days_off' AND column_name='incomplete'")).scalar_one() == "false"
