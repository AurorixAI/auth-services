"""LD-04 (wave E): the callbacks of diagnostic-service go through the SAME engine as the rest of the service, the one with
``pool_pre_ping`` - after a database restart / dropped connection the first callback must not answer 500 (disposable PostgreSQL)."""
from __future__ import annotations

import pytest
import sqlalchemy
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import internal_router
from src.core import database as core_database
from src.core.config import get_settings
from tests.test_diagnostic_completion_lifecycle import DB_URL, scenario  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(not DB_URL, reason="Requires disposable A2 auth PostgreSQL")


def _engine_of_the_internal_router():
    """The engine the real ``get_db`` of the internal router really talks through (no dependency override)."""
    generator = internal_router.get_db()
    try:
        return next(generator).get_bind()
    finally:
        generator.close()


def test_the_internal_router_uses_the_engine_of_the_service():
    engine = _engine_of_the_internal_router()
    assert engine is core_database.engine
    assert engine.pool._pre_ping is True


def test_the_first_callback_after_a_dropped_connection_is_not_a_500(scenario):
    _db, _factory, invite = scenario
    app = FastAPI()
    app.include_router(internal_router.router, prefix="/api/v1")
    url = f"/api/v1/internal/diagnostic/{invite.code}/start"
    headers = {"X-Internal-Service-Token": get_settings().internal_service_token}
    body = {"session_id": str(invite.cat_session_id)}

    engine = _engine_of_the_internal_router()
    engine.dispose()                                          # exactly one pooled connection from here on
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post(url, json=body, headers=headers).status_code == 200
        with engine.connect() as probe:
            pid = probe.exec_driver_sql("SELECT pg_backend_pid()").scalar()      # the pooled connection (same one)
        admin = sqlalchemy.create_engine(DB_URL, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                assert conn.exec_driver_sql("SELECT pg_terminate_backend(%s)", (pid,)).scalar() is True
        finally:
            admin.dispose()
        assert client.post(url, json=body, headers=headers).status_code == 200   # the pool notices and reconnects
