"""Revision ``wb4a1b2c3d4e5`` (wave B P4, B8): ``group_attendance.exam_release_pending`` and its partial index.  Up / down / up, repeating is harmless, a database built from the
models and stamped back upgrades, and the downgrade leaves the table itself."""
import pytest
from alembic import command
from sqlalchemy import inspect, text

from tests.test_k2_1_migration_pg import BASE_URL, config, engine, version  # noqa: F401  (engine is a fixture)

REVISION, BEFORE = "wb4a1b2c3d4e5", "k4a1b2c3d4e5"
INDEX = "ix_attendance_exam_release_pending"
pytestmark = pytest.mark.skipif(not BASE_URL, reason="Requires isolated a5_auth_migrations PostgreSQL")


def columns(engine):
    return {c["name"]: c for c in inspect(engine).get_columns("group_attendance")}


def index_definition(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"), {"n": INDEX}).scalar()


def test_the_revision_follows_k4a_and_is_the_head():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config())
    assert script.get_heads() == [REVISION] and script.get_revision(REVISION).down_revision == BEFORE


def test_up_down_up_the_column_and_the_partial_index(engine):
    cfg = config()
    command.upgrade(cfg, "head")
    assert version(engine) == REVISION
    column = columns(engine)["exam_release_pending"]
    assert column["nullable"] is False and "false" in str(column["default"]).lower()
    assert "WHERE exam_release_pending" in index_definition(engine) or "WHERE (exam_release_pending)" in index_definition(engine)
    command.downgrade(cfg, BEFORE)
    assert version(engine) == BEFORE and "exam_release_pending" not in columns(engine) and index_definition(engine) is None
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")                                # repeating is harmless
    assert version(engine) == REVISION and "exam_release_pending" in columns(engine)


def test_a_database_built_from_the_models_and_stamped_back_upgrades_without_failing(engine):
    from src.models.base import Base
    import src.models  # noqa: F401

    Base.metadata.create_all(engine)                            # the column and the index are already there
    cfg = config()
    command.stamp(cfg, BEFORE)
    command.upgrade(cfg, "head")
    assert version(engine) == REVISION and "exam_release_pending" in columns(engine)
