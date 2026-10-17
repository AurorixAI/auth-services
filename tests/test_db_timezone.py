"""ROADMAP #49: every database session runs in UTC, and the service refuses to start when it does not."""
import logging
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

from src.core import database  # noqa: E402
from src.core.db_timezone import (  # noqa: E402
    DatabaseTimezoneError,
    check_session_timezone,
    utc_connect_args,
    verify_database_timezone,
)

PG = "postgresql://u:p@localhost:5432/db"
DB_URL = os.getenv("A2_AUTH_TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(not DB_URL, reason="Requires disposable A2 auth PostgreSQL")


# ---- the pure check -----------------------------------------------------------


@pytest.mark.parametrize("name", ["UTC", "Etc/UTC", "utc", " Etc/UTC "])
def test_utc_names_pass(name):
    check_session_timezone(name)


@pytest.mark.parametrize("name", ["Asia/Tashkent", "Europe/Moscow", "GMT", "+05", "", None])
def test_any_other_zone_is_critical_and_refuses_to_start(name, caplog):
    with caplog.at_level(logging.CRITICAL, logger="src.core.db_timezone"):
        with pytest.raises(DatabaseTimezoneError):
            check_session_timezone(name)
    assert any(r.levelno == logging.CRITICAL and "TimeZone" in r.getMessage() for r in caplog.records)


# ---- the startup guard on an engine -------------------------------------------


def _engine(dialect, zone):
    @contextmanager
    def connect():
        yield SimpleNamespace(execute=lambda statement: SimpleNamespace(scalar=lambda: zone))

    return SimpleNamespace(dialect=SimpleNamespace(name=dialect), connect=connect)


def test_guard_accepts_a_utc_postgres_session():
    verify_database_timezone(_engine("postgresql", "Etc/UTC"))


def test_guard_rejects_a_tashkent_postgres_session():
    with pytest.raises(DatabaseTimezoneError, match="Asia/Tashkent"):
        verify_database_timezone(_engine("postgresql", "Asia/Tashkent"))


def test_guard_skips_sqlite_without_touching_the_database():
    def boom():
        raise AssertionError("SQLite has no session time zone: the guard must not connect")

    verify_database_timezone(SimpleNamespace(dialect=SimpleNamespace(name="sqlite"), connect=boom))
    verify_database_timezone(create_engine("sqlite://"))


# ---- wired into startup ---------------------------------------------------------


def test_startup_fails_before_any_migration_when_the_session_is_not_utc(monkeypatch):
    import asyncio

    from alembic import command

    from src.api import main_v2
    from src.core import db_timezone

    migrated = []
    monkeypatch.setattr(command, "upgrade", lambda *a, **k: migrated.append(a))

    def refuse(engine):
        raise DatabaseTimezoneError("session is Asia/Tashkent")

    monkeypatch.setattr(db_timezone, "verify_database_timezone", refuse)

    async def start():
        async with main_v2.lifespan(main_v2.app):
            pass

    with pytest.raises(DatabaseTimezoneError):
        asyncio.run(start())
    assert migrated == []  # nothing was written through a shifted session


# ---- the pin ------------------------------------------------------------------


def test_postgres_connect_args_carry_the_utc_option():
    assert utc_connect_args(PG) == {"options": "-c timezone=UTC"}
    assert utc_connect_args("postgresql+psycopg2://u:p@h/db") == {"options": "-c timezone=UTC"}


def test_existing_connect_args_and_options_are_kept_and_utc_comes_last():
    merged = utc_connect_args(PG, {"connect_timeout": 5, "options": "-c lock_timeout=5000"})
    assert merged == {"connect_timeout": 5, "options": "-c lock_timeout=5000 -c timezone=UTC"}
    assert utc_connect_args(PG + "?options=-c%20timezone%3DAsia/Tashkent")["options"].endswith(" -c timezone=UTC")
    assert utc_connect_args(PG, merged) == merged  # idempotent


def test_sqlite_never_gets_postgres_options():
    assert utc_connect_args("sqlite://") == {}
    assert utc_connect_args("sqlite:///x.db", {"check_same_thread": False}) == {"check_same_thread": False}


def test_the_service_engine_is_built_with_the_pin(monkeypatch):
    recorded = {}
    monkeypatch.setattr(database, "create_engine", lambda url, **kw: recorded.update(url=url, **kw))
    monkeypatch.setattr(database, "get_settings", lambda: SimpleNamespace(database_url=PG, debug=False))
    database._make_engine()
    assert recorded["connect_args"] == {"options": "-c timezone=UTC"}


def test_the_internal_router_engine_is_pinned_too(monkeypatch):
    """``src.api.database`` is a second engine (internal routes): load a private copy of it and read what it asks for."""
    import importlib.util
    import sqlalchemy

    from src.api import database as shared

    recorded = {}

    def fake_create_engine(url, **kwargs):
        recorded.update(url=url, **kwargs)
        return create_engine("sqlite://")

    monkeypatch.setattr(sqlalchemy, "create_engine", fake_create_engine)
    monkeypatch.setenv("DATABASE_URL", PG)
    spec = importlib.util.spec_from_file_location("api_database_probe", shared.__file__)
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
    assert recorded["connect_args"] == {"options": "-c timezone=UTC"}


# ---- on a real PostgreSQL ------------------------------------------------------


def _zone(url, **engine_kw):
    engine = create_engine(url, poolclass=NullPool, **engine_kw)
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT current_setting('TimeZone')")).scalar()
    finally:
        engine.dispose()


@needs_pg
def test_pinned_engine_is_utc_even_when_the_default_is_not(monkeypatch):
    drifted = {"options": "-c timezone=Asia/Tashkent"}  # what a changed server default looks like to a session
    assert _zone(DB_URL, connect_args=drifted) == "Asia/Tashkent"
    assert _zone(DB_URL, connect_args=utc_connect_args(DB_URL, drifted)) == "UTC"
    monkeypatch.setenv("PGOPTIONS", "-c timezone=Asia/Tashkent")  # a drifted container environment
    assert _zone(DB_URL) == "Asia/Tashkent"
    assert _zone(DB_URL, connect_args=utc_connect_args(DB_URL)) == "UTC"


@needs_pg
def test_guard_on_a_real_connection(monkeypatch):
    good = create_engine(DB_URL, poolclass=NullPool, connect_args=utc_connect_args(DB_URL))
    verify_database_timezone(good)
    good.dispose()
    # libpq sends PGTZ after ``options`` and it wins: the pin cannot beat it, the guard must catch it
    monkeypatch.setenv("PGTZ", "Asia/Tashkent")
    bad = create_engine(DB_URL, poolclass=NullPool, connect_args=utc_connect_args(DB_URL))
    with pytest.raises(DatabaseTimezoneError):
        verify_database_timezone(bad)
    bad.dispose()
