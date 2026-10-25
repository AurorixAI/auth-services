"""Revision ``pf1a2b3c4d5`` (PAY-F): up, down, up again; the old cancellations keep a NULL kind; the check constraint; no drift from the models."""
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

HEAD = "pf1a2b3c4d5"
BEFORE = "cn1a2b3c4d5"
DB_NAME = "a5_auth_payf_migrations"


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


def test_up_down_up_keeps_the_old_cancellations_and_drops_only_its_own(engine):
    cfg = config()
    command.upgrade(cfg, "head")
    assert version(engine) == HEAD and "org_days_off" in inspect(engine).get_table_names()
    assert {"cancellation_kind", "day_off_id"} <= columns(engine, "group_attendance")
    org, group = uuid4(), uuid4()
    from src.models.auth import Group, Organization

    with engine.begin() as c:
        c.execute(Organization.__table__.insert().values(id=org, name="Центр", status="active"))        # Core: the model's own defaults, only the columns given
        c.execute(Group.__table__.insert().values(id=group, org_id=org, name="Группа", class_level=9))
        # a cancellation as the code before the release wrote it: no kind
        c.execute(text("INSERT INTO group_attendance (id, org_id, group_id, lesson_date, lesson_id, status, cancellation_reason, version) "
                       "VALUES (gen_random_uuid(), :o, :g, '2026-10-12', 'lesson-mon', 'cancelled', 'Заболел', 1)"), {"o": org, "g": group})
    with engine.connect() as c:
        assert c.execute(text("SELECT cancellation_kind FROM group_attendance WHERE group_id=:g"), {"g": group}).scalar_one() is None
    with pytest.raises(IntegrityError):                                      # the check constraint: only centre / holiday / NULL
        with engine.begin() as c:
            c.execute(text("UPDATE group_attendance SET cancellation_kind='party' WHERE group_id=:g"), {"g": group})
    with pytest.raises(IntegrityError):                                      # a range of more than 14 days / a reason shorter than 3 characters
        with engine.begin() as c:
            c.execute(text("INSERT INTO org_days_off (id, org_id, date_from, date_to, reason) VALUES (gen_random_uuid(), :o, '2026-10-01', '2026-10-15', 'Хайит')"), {"o": org})
    with pytest.raises(IntegrityError):
        with engine.begin() as c:
            c.execute(text("INSERT INTO org_days_off (id, org_id, date_from, date_to, reason) VALUES (gen_random_uuid(), :o, '2026-10-01', '2026-10-01', 'ab')"), {"o": org})
    with engine.begin() as c:
        c.execute(text("INSERT INTO org_days_off (id, org_id, date_from, date_to, reason) VALUES (gen_random_uuid(), :o, '2026-10-01', '2026-10-14', 'Хайит')"), {"o": org})

    command.downgrade(cfg, BEFORE)
    assert version(engine) == BEFORE and "org_days_off" not in inspect(engine).get_table_names()
    assert not ({"cancellation_kind", "day_off_id"} & columns(engine, "group_attendance"))
    command.downgrade(cfg, BEFORE)                                           # repeated: harmless
    with engine.connect() as c:
        assert c.execute(text("SELECT cancellation_reason FROM group_attendance WHERE group_id=:g"), {"g": group}).scalar_one() == "Заболел"

    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")                                             # repeated: harmless
    assert version(engine) == HEAD
    with engine.connect() as c:
        assert c.execute(text("SELECT cancellation_kind FROM group_attendance WHERE group_id=:g"), {"g": group}).scalar_one() is None


def test_a_database_built_from_the_models_and_stamped_back_upgrades_without_failing(engine):
    from src.models.base import Base
    import src.models  # noqa: F401

    Base.metadata.create_all(engine)                                          # the table and the columns are already there
    cfg = config()
    command.stamp(cfg, BEFORE)
    command.upgrade(cfg, "head")
    assert version(engine) == HEAD
    assert {"cancellation_kind", "day_off_id"} <= columns(engine, "group_attendance")


def test_the_migration_and_the_models_agree_on_the_new_columns(engine):
    from src.models.auth import GroupAttendance, OrgDayOff

    command.upgrade(config(), "head")
    insp = inspect(engine)
    for model in (GroupAttendance, OrgDayOff):
        assert {c.name for c in model.__table__.columns} == columns(engine, model.__tablename__), model.__tablename__
    checks = {c["name"] for c in insp.get_check_constraints("org_days_off")} | {c["name"] for c in insp.get_check_constraints("group_attendance")}
    assert {"ck_org_days_off_range", "ck_org_days_off_reason", "ck_group_attendance_cancellation_kind"} <= checks
    with engine.connect() as c:
        # added NOT VALID and validated in a separate statement: both end up validated
        assert c.execute(text("SELECT bool_and(convalidated) FROM pg_constraint WHERE conname IN ('ck_group_attendance_cancellation_kind', 'group_attendance_day_off_id_fkey')")).scalar() is True
        assert c.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname='idx_group_attendance_day_off'")).scalar().endswith("WHERE (day_off_id IS NOT NULL)")
        assert "SET NULL" in c.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='group_attendance_day_off_id_fkey'")).scalar()
        # the users of the journal may go: the day off keeps its record
        assert c.execute(text("SELECT count(*) FROM pg_constraint WHERE conrelid='org_days_off'::regclass AND contype='f' AND confdeltype='n'")).scalar() == 2
        assert "'day_off_voided'" in c.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_group_audit_action'")).scalar()
    command.downgrade(config(), BEFORE)
    with engine.connect() as c:
        assert "'day_off_voided'" not in c.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_group_audit_action'")).scalar()
        assert c.execute(text("SELECT count(*) FROM pg_indexes WHERE indexname='idx_group_attendance_day_off'")).scalar() == 0
    command.downgrade(config(), BEFORE)                   # repeated: DROP ... IF EXISTS, harmless
