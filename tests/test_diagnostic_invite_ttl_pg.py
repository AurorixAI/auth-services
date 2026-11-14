"""A diagnostic link lives 7 days (the screens «Новая диагностика» / «Новый ученик» promise it; LESSONS_LEARNED 125)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth import service as service_module
from src.core.database import get_db
from src.models.auth import DiagnosticInvite

from tests.pg_world import make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)

pytestmark = needs_pg

API = "/api/v1/auth"


def _public_client(w):
    from src.auth.router import router as auth_router

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: w.db
    return TestClient(app)


def _aware(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _move_clock(monkeypatch, delta: timedelta):
    real = service_module.datetime

    class Shifted(real):
        @classmethod
        def now(cls, tz=None):
            return real.now(tz) + delta

    monkeypatch.setattr(service_module, "datetime", Shifted)


def test_a_created_invite_lives_seven_days_and_the_answers_say_so(world, monkeypatch):
    student = world.students[0]
    client = make_client(world, user=world.admin)
    before = datetime.now(timezone.utc)
    r = client.post(f"{API}/org/diagnostics", json={"student_id": str(student.id), "class_level": 9, "diagnostic_type": "initial"})
    assert r.status_code == 201, r.text
    body = r.json()
    expires = _aware(body["expires_at"])
    assert abs((expires - before) - timedelta(days=7)) < timedelta(minutes=1)

    listed = client.get(f"{API}/org/diagnostics")
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"] if isinstance(listed.json(), dict) else listed.json()
    row = next(i for i in items if i["id"] == body["id"])
    assert _aware(row["expires_at"]) == expires

    public = _public_client(world)
    code = body["code"]
    _move_clock(monkeypatch, timedelta(days=6, hours=23))
    assert public.get(f"{API}/diagnostic/{code}").json()["status"] == "pending"
    _move_clock(monkeypatch, timedelta(days=7, minutes=1))
    info = public.get(f"{API}/diagnostic/{code}")
    assert info.json()["status"] == "expired", info.text
    assert world.db.query(DiagnosticInvite).filter_by(code=code).one().status == "expired"


def test_g3m_c6_the_expiry_is_told_in_utc_with_the_zone_everywhere(world):
    student = world.students[0]
    client = make_client(world, user=world.admin)
    made = client.post(f"{API}/org/diagnostics", json={"student_id": str(student.id), "class_level": 9, "diagnostic_type": "initial"}).json()
    listed = client.get(f"{API}/org/diagnostics").json()
    row = next(i for i in (listed["items"] if isinstance(listed, dict) else listed) if i["id"] == made["id"])
    public = _public_client(world).get(f"{API}/diagnostic/{made['code']}").json()
    for value in (made["expires_at"], row["expires_at"], public["expires_at"]):
        assert value.endswith("+00:00"), value                  # before: the list said '2026-11-21T07:00:00' - a time without a zone
    assert made["expires_at"] == row["expires_at"] == public["expires_at"]
