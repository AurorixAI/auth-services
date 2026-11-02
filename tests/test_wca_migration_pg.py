"""Revision ``wca1b2c3d4e5`` (wave C-A, MN-48 / LD-29): three indexes.  Up / down / up on an empty database and on a filled one (rows survive, the indexes cover the
queries), repeating is harmless, a database built from the models (the indexes are already there) and stamped back upgrades."""
from datetime import date, datetime
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_k2_1_migration_pg import BASE_URL, config, engine, version  # noqa: F401  (engine is a fixture)

REVISION, BEFORE = "wca1b2c3d4e5", "wb4a1b2c3d4e5"
INDEXES = {
    "ix_student_groups_group": ("student_groups", "(group_id)"),
    "ix_attendance_org_completed_date": ("group_attendance", "(org_id, lesson_date)"),
    "ix_org_members_user": ("organization_members", "(user_id)"),
}
pytestmark = pytest.mark.skipif(not BASE_URL, reason="Requires isolated a5_auth_migrations PostgreSQL")


def definitions(engine):
    with engine.connect() as c:
        return {n: d for n, d in c.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE indexname = ANY(:n)"), {"n": list(INDEXES)}).all()}


def test_the_revision_follows_wave_b_and_is_the_head():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config())
    assert script.get_heads() == [REVISION] and script.get_revision(REVISION).down_revision == BEFORE


def test_up_down_up_on_a_filled_database(engine):
    from src.models.auth import Group, Organization, OrganizationMember, Student, StudentGroup, User
    from src.models.auth import GroupAttendance

    cfg = config()
    command.upgrade(cfg, BEFORE)                                 # (the baseline builds the tables from the models, so on a NEW database the indexes are already there)
    with engine.begin() as c:                                    # an existing database: the three indexes are not there yet
        for name in INDEXES:
            c.execute(text(f"DROP INDEX IF EXISTS {name}"))
    assert definitions(engine) == {}
    with Session(engine) as db:                                  # a filled database: a centre with a member, a group, a student, held and open lessons
        org, user = Organization(name="WC", status="active"), User(email=f"{uuid4()}@wc.test", password_hash="x", first_name="A", last_name="B", role="teacher")
        db.add_all([org, user])
        db.flush()
        group = Group(org_id=org.id, name="G", class_level=9, teacher_id=user.id)
        db.add_all([OrganizationMember(org_id=org.id, user_id=user.id, org_role="teacher", is_active=True), group])
        db.flush()
        student = Student(org_id=org.id, first_name="S", last_name="T", class_level=9, group_id=group.id)
        db.add(student)
        db.flush()
        db.add(StudentGroup(student_id=student.id, group_id=group.id, is_active=True, joined_at=datetime(2026, 1, 1)))
        for i, status in enumerate(("completed", "completed", "in_progress", "cancelled")):
            db.add(GroupAttendance(org_id=org.id, group_id=group.id, lesson_date=f"2026-10-0{i + 1}", lesson_id=f"l{i}", status=status, records={}, roster_snapshot=[]))
        db.commit()
        counts = {t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t, _ in INDEXES.values()}
    command.upgrade(cfg, "head")
    assert version(engine) == REVISION
    found = definitions(engine)
    assert set(found) == set(INDEXES)
    for name, (table, columns) in INDEXES.items():
        assert f"ON public.{table}" in found[name] and columns in found[name], found[name]
    assert "WHERE" in found["ix_attendance_org_completed_date"] and "'completed'" in found["ix_attendance_org_completed_date"]       # partial: held lessons only
    with engine.connect() as c:
        assert {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t, _ in INDEXES.values()} == counts            # no row lost
    command.downgrade(cfg, BEFORE)
    assert version(engine) == BEFORE and definitions(engine) == {}
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")                                 # repeating is harmless
    assert version(engine) == REVISION and set(definitions(engine)) == set(INDEXES)


def test_a_database_built_from_the_models_and_stamped_back_upgrades_without_failing(engine):
    from src.models.base import Base
    import src.models  # noqa: F401

    Base.metadata.create_all(engine)                             # the models carry the same three indexes
    assert set(definitions(engine)) == set(INDEXES)
    cfg = config()
    command.stamp(cfg, BEFORE)
    command.upgrade(cfg, "head")
    assert version(engine) == REVISION and set(definitions(engine)) == set(INDEXES)
