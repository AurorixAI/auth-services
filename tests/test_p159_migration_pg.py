"""Revision ``p159a1b2c3d4`` (#159): a make-up is an official lesson - its ``money`` is always NULL.  The empty make-ups of #154 that were decided (``paid`` / ``none``) go back to NULL by one
update; the CHECK refuses a make-up with money; the downgrade brings the old CHECK back (the NULLs stay: the decisions are in the audit).  Also: the read-only SQL the owner runs on stage / prod
BEFORE the release (how many such rows, in which centres) answers on the very rows of this test."""
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

REVISION, BEFORE = "p159a1b2c3d4", "p154a1b2c3d4"
DB_NAME = "a5_auth_p159_migrations"

# The question before the release (read only): empty held make-ups that carry a decision, per centre.
SQL_EMPTY_DECIDED = """
SELECT o.org_id, count(*) FROM group_schedule_one_off_lessons o
JOIN group_attendance a ON a.group_id = o.group_id AND a.lesson_id = o.id AND a.lesson_date = o.lesson_date
WHERE o.purpose = 'makeup' AND o.money IS NOT NULL AND a.status = 'completed'
  AND NOT EXISTS (SELECT 1 FROM jsonb_each_text(a.records) r WHERE r.value IN ('present', 'late'))
GROUP BY o.org_id ORDER BY o.org_id"""
SQL_ANY_MAKEUP_MONEY = "SELECT org_id, count(*) FROM group_schedule_one_off_lessons WHERE purpose = 'makeup' AND money IS NOT NULL GROUP BY org_id ORDER BY org_id"


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


def centre(engine, name):
    from src.models.auth import Group, Organization, User

    org, group, user = uuid4(), uuid4(), uuid4()
    with engine.begin() as c:
        c.execute(Organization.__table__.insert().values(id=org, name=name, status="active"))
        c.execute(User.__table__.insert().values(id=user, email=f"{user}@p159.invalid", password_hash="x", first_name="А", last_name="Б", role="owner"))
        c.execute(Group.__table__.insert().values(id=group, org_id=org, name="Группа", class_level=9))
    return org, group, user


def one_off(engine, org, group, user, lid, purpose, money, *, day="2026-11-09", origin=None, records=None, status="completed"):
    with engine.begin() as c:
        c.execute(text("INSERT INTO group_schedule_one_off_lessons (id, org_id, group_id, lesson_date, payload, created_by, created_at, updated_at, purpose, money, made_up_lesson_id, made_up_date) "
                       "VALUES (:i, :o, :g, :d, '{}', :u, now(), now(), :p, :m, :l, :od)"),
                  {"i": lid, "o": org, "g": group, "u": user, "d": day, "p": purpose, "m": money, "l": origin, "od": "2026-11-02" if origin else None})
        if records is not None:
            c.execute(text("INSERT INTO group_attendance (id, org_id, group_id, lesson_date, lesson_id, status, records, version) VALUES (gen_random_uuid(), :o, :g, :d, :i, :s, CAST(:r AS jsonb), 1)"),
                      {"o": org, "g": group, "d": day, "i": lid, "s": status, "r": records})


def test_empty_decided_make_ups_go_back_to_null_the_rest_is_untouched_and_the_check_holds(engine):
    cfg = config()
    command.upgrade(cfg, BEFORE)
    a, ga, ua = centre(engine, "Центр А")
    b, gb, ub = centre(engine, "Центр Б")
    one_off(engine, a, ga, ua, "mk-empty-paid", "makeup", "paid", origin="lesson-1", records='{"s1": "absent", "s2": "excused"}')            # empty, decided «Оплатить»
    one_off(engine, a, ga, ua, "mk-empty-none", "makeup", "none", origin="lesson-2", records='{}')                                           # empty, decided «Не оплачивать»
    one_off(engine, b, gb, ub, "mk-empty-b", "makeup", "paid", origin="lesson-3", records='{"s1": "absent"}')
    one_off(engine, b, gb, ub, "mk-full-paid", "makeup", "paid", origin="lesson-4", records='{"s1": "present"}')                              # not empty (marked later): also NULL by the new CHECK
    one_off(engine, a, ga, ua, "mk-free", "makeup", None, origin="lesson-5", records='{"s1": "absent"}')                                     # nothing to change
    one_off(engine, a, ga, ua, "ex-paid", "extra", "paid", records='{"s1": "present"}')
    one_off(engine, a, ga, ua, "ex-wait", "extra", "wait", records='{"s1": "present"}')
    one_off(engine, a, ga, ua, "moved", "moved", None)
    with engine.connect() as c:                                                                              # the question before the release
        assert [(str(o), n) for o, n in c.execute(text(SQL_EMPTY_DECIDED))] == sorted([(str(a), 2), (str(b), 1)])
        assert sorted((str(o), n) for o, n in c.execute(text(SQL_ANY_MAKEUP_MONEY))) == sorted([(str(a), 2), (str(b), 2)])
    command.upgrade(cfg, REVISION)
    assert version(engine) == REVISION
    with engine.connect() as c:
        got = {r[0]: (r[1], r[2]) for r in c.execute(text("SELECT id, purpose, money FROM group_schedule_one_off_lessons"))}
        assert got == {"mk-empty-paid": ("makeup", None), "mk-empty-none": ("makeup", None), "mk-empty-b": ("makeup", None), "mk-full-paid": ("makeup", None), "mk-free": ("makeup", None),
                       "ex-paid": ("extra", "paid"), "ex-wait": ("extra", "wait"), "moved": ("moved", None)}
        assert c.execute(text(SQL_ANY_MAKEUP_MONEY)).fetchall() == []
        assert c.execute(text("SELECT convalidated FROM pg_constraint WHERE conname='ck_one_off_money'")).scalar_one() is True
    with pytest.raises(IntegrityError):                                                                      # a make-up never carries money again
        one_off(engine, a, ga, ua, "mk-bad", "makeup", "paid", origin="lesson-9")
    command.downgrade(cfg, BEFORE)
    assert version(engine) == BEFORE
    one_off(engine, a, ga, ua, "mk-old-rule", "makeup", "paid", origin="lesson-9")                           # the old CHECK is back
    with engine.connect() as c:
        assert c.execute(text("SELECT money FROM group_schedule_one_off_lessons WHERE id='mk-empty-paid'")).scalar_one() is None      # the NULLs stay
    command.upgrade(cfg, REVISION)                                                                           # up again: the row written under the old rule is cleaned by the same update
    with engine.connect() as c:
        assert c.execute(text("SELECT money FROM group_schedule_one_off_lessons WHERE id='mk-old-rule'")).scalar_one() is None
    command.upgrade(cfg, REVISION)                                                                           # repeated: harmless


def test_the_model_agrees_with_the_check(engine):
    from src.models.auth import GroupScheduleOneOffLesson

    command.upgrade(config(), REVISION)
    with engine.connect() as c:
        db_def = c.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_one_off_money'")).scalar_one()
    model = next(str(c.sqltext) for c in GroupScheduleOneOffLesson.__table__.constraints if getattr(c, "name", None) == "ck_one_off_money")
    assert "'makeup', 'moved'" in model and "makeup" in db_def and "'none', 'paid'" not in db_def.split("makeup")[1]
