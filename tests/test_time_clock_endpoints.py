"""TIME-TESTS (server): ``/api/v1/auth/dev/clock`` - GET / PUT only with both signs (else 404), forward only, a clear 503 when Redis is down."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth import business_clock as bc
from src.auth.dev_router import router as dev_router
from tests.time_clock_helpers import no_stand_clock, stand_clock  # noqa: F401  (fixtures)

URL = "/api/v1/auth/dev/clock"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(dev_router, prefix="/api/v1")
    return TestClient(app)


def test_without_the_signs_every_call_is_404_and_redis_is_not_touched(client, no_stand_clock):
    assert client.get(URL).status_code == 404
    assert client.put(URL, json={"offset_seconds": 60}).status_code == 404
    assert client.put(URL, json={"business_now": "2030-01-01T10:00"}).status_code == 404
    assert client.delete(URL).status_code in (404, 405)
    assert no_stand_clock.calls == 0


def test_a_development_app_without_the_variable_answers_404(client, monkeypatch, no_stand_clock):
    monkeypatch.setenv("APP_ENV", "development")                       # the first sign only
    assert client.get(URL).status_code == 404 and no_stand_clock.calls == 0


def test_get_shows_the_stand_time(client, stand_clock):
    body = client.get(URL).json()
    assert set(body) == {"now_business", "now_utc", "offset_seconds"} and body["offset_seconds"] == 0
    assert body["now_business"].endswith("+05:00") and body["now_utc"].endswith("+00:00")


def _in_days(days: int) -> datetime:
    return (bc.real_utc_now().astimezone(bc.BUSINESS_TIMEZONE) + timedelta(days=days)).replace(hour=10, minute=0, second=0, microsecond=0)


def test_put_by_business_time_moves_the_clock_and_get_follows(client, stand_clock):
    target = _in_days(40)
    r = client.put(URL, json={"business_now": target.strftime("%Y-%m-%dT%H:%M")})
    assert r.status_code == 200, r.text
    assert r.json()["now_business"].startswith(target.strftime("%Y-%m-%dT10:00:0"))
    assert client.get(URL).json()["now_business"].startswith(target.strftime("%Y-%m-%dT10:00:"))
    assert bc.business_today() == target.date()


def test_put_by_offset_and_the_clock_runs_on(client, stand_clock):
    r = client.put(URL, json={"offset_seconds": 2 * 86400})
    assert r.status_code == 200 and r.json()["offset_seconds"] == 2 * 86400
    real = bc.real_utc_now().astimezone(bc.BUSINESS_TIMEZONE).date()
    assert bc.business_today() == real + timedelta(days=2)


def test_put_refuses_to_go_back_and_the_state_is_unchanged(client, stand_clock):
    client.put(URL, json={"business_now": _in_days(40).strftime("%Y-%m-%dT%H:%M")})
    before = client.get(URL).json()["offset_seconds"]
    r = client.put(URL, json={"business_now": _in_days(39).strftime("%Y-%m-%dT%H:%M")})
    assert r.status_code == 409 and "вперёд" in r.json()["detail"]
    assert client.get(URL).json()["offset_seconds"] == before
    assert client.put(URL, json={"offset_seconds": before - 1}).status_code == 409


@pytest.mark.parametrize("body", [{}, {"business_now": "2030-03-04T10:00", "offset_seconds": 5}, {"business_now": "tomorrow"}, {"business_now": "2030-13-04T10:00"},
                                  {"offset_seconds": "soon"}, {"offset_seconds": 10 ** 12}, {"other": 1}])
def test_put_rejects_a_malformed_body(client, stand_clock, body):
    assert client.put(URL, json=body).status_code == 422
    assert stand_clock.store == {}


def test_a_dead_redis_is_a_503_with_the_reason_not_a_silent_zero(client, stand_clock):
    stand_clock.down = True
    for r in (client.get(URL), client.put(URL, json={"offset_seconds": 60})):
        assert r.status_code in (500, 503)
