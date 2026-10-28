"""Revision ``p154a1b2c3d4`` (#154): the purpose / money / make-up columns of the one-off lessons, ``made_up_by`` of the attendance row, the two new audit actions;
the old rows are brought to the new rule (a copy of a move -> ``moved``, every other one -> ``extra`` / ``paid``); up, down, up again; the models agree."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from src.core.db_timezone import utc_connect_args

BASE_URL = os.environ.get("A5_AUTH_MIGRATION_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not BASE_URL, reason="Requires isolated a5_auth_migrations PostgreSQL")

REVISION = "p154a1b2c3d4"
BEFORE = "p157a1b2c3d4"
DB_NAME = "a5_auth_p154_migrations"


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


def seed_old(engine):
    """The state BEFORE the revision: three one-off lessons (a plain one, the copy of a move, and a lesson nobody knows), the request that made the copy."""
    from src.models.auth import Group, Organization, User

    org, group, user, request = uuid4(), uuid4(), uuid4(), uuid4()
    with engine.begin() as c:
        c.execute(Organization.__table__.insert().values(id=org, name="Центр", status="active"))
        c.execute(User.__table__.insert().values(id=user, email=f"{user}@p154.invalid", password_hash="x", first_name="А", last_name="Б", role="owner"))
        c.execute(Group.__table__.insert().values(id=group, org_id=org, name="Группа", class_level=9))
        for lid in ("one-off-plain", "one-off-copy", "one-off-other"):
            c.execute(text("INSERT INTO group_schedule_one_off_lessons (id, org_id, group_id, lesson_date, payload, created_by, created_at, updated_at) "
                           "VALUES (:i, :o, :g, '2026-11-09', '{}', :u, now(), now())"), {"i": lid, "o": org, "g": group, "u": user})
        c.execute(text("INSERT INTO lesson_substitution_requests (id, org_id, group_id, lesson_id, lesson_date, start_time, end_time, starts_at, requester_id, origin, batch_id, reason_code, "
                       "status, moved_to_lesson_id, moved_to_date, decided_at, created_at, updated_at) VALUES (:i, :o, :g, 'lesson-l1', '2026-11-02', '10:00', '11:00', '2026-11-02 05:00', :u, "
                       "'teacher', :b, 'sick', 'rescheduled', 'one-off-copy', '2026-11-09', now(), now(), now())"), {"i": request, "o": org, "g": group, "u": user, "b": uuid4()})
        c.execute(text("INSERT INTO group_audit (id, org_id, group_id, actor_id, actor_role, action, snapshot, created_at) VALUES (gen_random_uuid(), :o, :g, :u, 'owner', 'created', '{}', now())"),
                  {"o": org, "g": group, "u": user})
    return org, group, user


def audit(engine, org, group, user, action):
    with engine.begin() as c:
        c.execute(text("INSERT INTO group_audit (id, org_id, group_id, actor_id, actor_role, action, snapshot, created_at) VALUES (gen_random_uuid(), :o, :g, :u, 'owner', :a, '{}', now())"),
                  {"o": org, "g": group, "u": user, "a": action})


def test_old_rows_are_brought_to_the_rule_and_down_up_round_trips(engine):
    cfg = config()
    command.upgrade(cfg, BEFORE)
    org, group, user = seed_old(engine)
    command.upgrade(cfg, REVISION)
    assert version(engine) == REVISION
    with engine.connect() as c:
        got = {r[0]: (r[1], r[2], r[3], r[4]) for r in c.execute(text("SELECT id, purpose, money, made_up_lesson_id, made_up_date FROM group_schedule_one_off_lessons"))}
        assert got == {"one-off-plain": ("extra", "paid", None, None), "one-off-copy": ("moved", None, None, None), "one-off-other": ("extra", "paid", None, None)}
        assert c.execute(text("SELECT is_nullable FROM information_schema.columns WHERE table_name='group_schedule_one_off_lessons' AND column_name='purpose'")).scalar_one() == "NO"
        for name in ("ck_one_off_purpose", "ck_one_off_money", "ck_one_off_made_up", "ck_group_audit_action"):
            assert c.execute(text("SELECT convalidated FROM pg_constraint WHERE conname=:n"), {"n": name}).scalar_one() is True, name
    for action in ("one_off_created", "one_off_money_decided"):
        audit(engine, org, group, user, action)
    command.downgrade(cfg, BEFORE)
    assert version(engine) == BEFORE
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM group_audit WHERE action IN ('one_off_created', 'one_off_money_decided')")).scalar_one() == 0
        assert c.execute(text("SELECT count(*) FROM information_schema.columns WHERE table_name='group_schedule_one_off_lessons' AND column_name IN ('purpose','money','made_up_lesson_id','made_up_date')")).scalar_one() == 0
        assert c.execute(text("SELECT count(*) FROM information_schema.columns WHERE table_name='group_attendance' AND column_name='made_up_by'")).scalar_one() == 0
        assert c.execute(text("SELECT count(*) FROM group_schedule_one_off_lessons")).scalar_one() == 3          # the lessons stay, only their purpose is gone
    with pytest.raises(IntegrityError):
        audit(engine, org, group, user, "one_off_created")                           # the old CHECK is back
    command.downgrade(cfg, BEFORE)                                                   # repeated: harmless
    command.upgrade(cfg, REVISION)
    command.upgrade(cfg, REVISION)                                                   # repeated: harmless
    audit(engine, org, group, user, "one_off_money_decided")
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM group_schedule_one_off_lessons WHERE purpose = 'extra' AND money = 'paid'")).scalar_one() == 2


def test_the_checks_refuse_a_wrong_combination_and_the_unique_index_works(engine):
    cfg = config()
    command.upgrade(cfg, REVISION)
    org, group, user = seed_old_after(engine)
    bad = [("extra", None, None, None), ("moved", "paid", None, None), ("makeup", "wait", "lesson-l1", "2026-11-02"), ("makeup", None, None, None), ("extra", "paid", "lesson-l1", "2026-11-02"),
           ("other", None, None, None), ("makeup", None, "lesson-l1", None)]
    for purpose, money, lesson, day in bad:
        with pytest.raises(IntegrityError):
            insert_one_off(engine, org, group, user, purpose, money, lesson, day)
    insert_one_off(engine, org, group, user, "makeup", None, "lesson-l1", "2026-11-02")
    insert_one_off(engine, org, group, user, "makeup", "none", "lesson-l2", "2026-11-04")
    insert_one_off(engine, org, group, user, "extra", "wait", None, None)
    insert_one_off(engine, org, group, user, "moved", None, None, None)
    with engine.begin() as c:
        c.execute(text("INSERT INTO group_attendance (id, org_id, group_id, lesson_date, lesson_id, status, made_up_by, version) VALUES (gen_random_uuid(), :o, :g, '2026-11-02', 'lesson-l1', 'cancelled', 'one-off-x', 1)"), {"o": org, "g": group})
    with pytest.raises(IntegrityError):                                              # one make-up lesson cannot make up for two cancelled lessons
        with engine.begin() as c:
            c.execute(text("INSERT INTO group_attendance (id, org_id, group_id, lesson_date, lesson_id, status, made_up_by, version) VALUES (gen_random_uuid(), :o, :g, '2026-11-04', 'lesson-l2', 'cancelled', 'one-off-x', 1)"), {"o": org, "g": group})


def seed_old_after(engine):
    from src.models.auth import Group, Organization, User

    org, group, user = uuid4(), uuid4(), uuid4()
    with engine.begin() as c:
        c.execute(Organization.__table__.insert().values(id=org, name="Центр", status="active"))
        c.execute(User.__table__.insert().values(id=user, email=f"{user}@p154.invalid", password_hash="x", first_name="А", last_name="Б", role="owner"))
        c.execute(Group.__table__.insert().values(id=group, org_id=org, name="Группа", class_level=9))
    return org, group, user


def insert_one_off(engine, org, group, user, purpose, money, lesson, day):
    with engine.begin() as c:
        c.execute(text("INSERT INTO group_schedule_one_off_lessons (id, org_id, group_id, lesson_date, payload, created_by, created_at, updated_at, purpose, money, made_up_lesson_id, made_up_date) "
                       "VALUES (:i, :o, :g, '2026-11-09', '{}', :u, now(), now(), :p, :m, :l, :d)"),
                  {"i": f"one-off-{uuid4().hex[:8]}", "o": org, "g": group, "u": user, "p": purpose, "m": money, "l": lesson, "d": day})


def test_the_models_agree_with_the_checks(engine):
    from src.models.auth import GroupAudit, GroupScheduleOneOffLesson

    command.upgrade(config(), REVISION)
    with engine.connect() as c:
        for name in ("ck_one_off_purpose", "ck_one_off_money", "ck_one_off_made_up", "ck_group_audit_action"):
            definition = c.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname=:n"), {"n": name}).scalar_one()
            assert definition
    model = " ".join(str(c.sqltext) for t in (GroupScheduleOneOffLesson.__table__, GroupAudit.__table__) for c in t.constraints if getattr(c, "name", None))
    for needle in ("'makeup'", "'extra'", "'moved'", "'wait'", "one_off_created", "one_off_money_decided", "made_up_lesson_id IS NOT NULL"):
        assert needle in model, needle
