"""LD-27 (wave E): the schema of a deployed database comes from Alembic only. Start-up no longer calls ``Base.metadata.create_all`` after the
migrations (it swallowed its own errors and quietly added a table that a migration forgot - in production but not in the CI database), and
``alembic upgrade head`` on an empty database already gives every table and column of the models."""
import os

import pytest
import sqlalchemy
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from src.core.db_timezone import utc_connect_args
from tests.test_k3_startup import STAGING, run_lifespan

BASE_URL = os.environ.get("A5_AUTH_MIGRATION_TEST_DATABASE_URL")
DB_NAME = "a5_auth_head_vs_models"      # its OWN database (two files on two workers must not drop one database under each other)

# Columns that the models have and the migrations never added. All of them belong to models nothing in the service reads or writes
# (AnswerEvent / SessionSummary: diagnostic-service keeps its own copy of these tables); a new name here needs a migration instead.
KNOWN_MODEL_ONLY_COLUMNS = {
    ("answer_events", "org_id"),
    ("session_summaries", "org_id"),
    ("session_summaries", "integrity_report"),
    ("session_summaries", "error_patterns"),
}


def test_start_up_does_not_create_tables_after_the_migrations(monkeypatch):
    created = []
    monkeypatch.setattr(sqlalchemy.MetaData, "create_all", lambda self, *a, **k: created.append(a or k))
    run_lifespan(monkeypatch, **STAGING)
    assert created == []


@pytest.mark.skipif(not BASE_URL, reason="Requires isolated a5_auth_migrations PostgreSQL")
def test_head_on_an_empty_database_has_every_table_and_column_of_the_models(monkeypatch):
    from pathlib import Path

    from src.models.auth import Base
    import src.models  # noqa: F401  (all the model modules on one metadata)

    base = make_url(BASE_URL)
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT", connect_args=utc_connect_args(BASE_URL))
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {DB_NAME}"))
    url = base.set(database=DB_NAME).render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url, connect_args=utc_connect_args(url))
    try:
        root = Path(__file__).resolve().parents[1]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "alembic"))
        command.upgrade(config, "head")
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert set(Base.metadata.tables) - tables == set(), "a table of the models has no migration"
        missing = {(t, c.name) for t, table in Base.metadata.tables.items() for c in table.columns
                   if c.name not in {col["name"] for col in inspector.get_columns(t)}}
        assert missing == KNOWN_MODEL_ONLY_COLUMNS
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)"))
        admin.dispose()
