"""#77 (auth side): the public diagnostic code lookups are throttled by FAILED lookups per browser address.

``GET /auth/diagnostic/{code}`` answers with the student's name, and the front calls it straight from the browser. The code
space (8 characters, 31 letters) must not be probed: 20 failed lookups a minute and 200 an hour per client address, then
429 + Retry-After. A valid lookup is not counted (a class behind one NAT opens ~30 codes in a minute), and a call without
X-Real-IP (diagnostic-service asks server to server from our own address) is not a browser and is not counted here.
"""
import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import src.api.internal_router  # noqa: E402,F401  (import order: avoids a pre-existing router cycle)
from src.api import rate_limit as rl  # noqa: E402
from src.auth import router as auth_router_module  # noqa: E402
from src.auth.service import AuthService  # noqa: E402
from src.core.database import get_db  # noqa: E402

API = "/api/v1/auth"
KNOWN = {f"CLS{i:05d}" for i in range(60)}
LIMIT_MINUTE = auth_router_module.DIAGNOSTIC_CODE_FAIL_LIMITS[0][0]
LIMIT_HOUR = auth_router_module.DIAGNOSTIC_CODE_FAIL_LIMITS[1][0]


@pytest.fixture
def client(monkeypatch):
    # the invites that exist in some state (started / completed / expired): the confirm service refuses them like an unknown code
    monkeypatch.setattr(auth_router_module, "_diagnostic_invite_exists", lambda db, code: code in KNOWN or code.startswith("STATE"))
    def info(self, code):
        return {"id": "i", "student_first_name": "Али", "student_last_name": "Каримов", "status": "confirmed"} if code in KNOWN else None

    def confirm(self, code):
        if code in KNOWN:
            return {"status": "confirmed"}
        raise ValueError("Диагностика не найдена или уже начата")

    monkeypatch.setattr(AuthService, "get_diagnostic_by_code", info)
    monkeypatch.setattr(AuthService, "confirm_diagnostic", confirm)
    app = FastAPI()
    app.include_router(auth_router_module.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: MagicMock()
    return TestClient(app)


def lookup(client, code, ip="203.0.113.7", **headers):
    h = {"x-real-ip": ip} if ip else {}
    h.update(headers)
    return client.get(f"{API}/diagnostic/{code}", headers=h)


def confirm(client, code, ip="203.0.113.7"):
    return client.post(f"{API}/diagnostic/{code}/confirm", headers={"x-real-ip": ip} if ip else {})


def test_the_limits_are_what_the_task_decided():
    assert auth_router_module.DIAGNOSTIC_CODE_FAIL_LIMITS == ((100, 60), (600, 3600))


def test_a_class_behind_one_nat_opens_thirty_codes_in_a_minute(client):
    for i in range(30):
        assert lookup(client, f"CLS{i:05d}").status_code == 200
        assert confirm(client, f"CLS{i:05d}").status_code == 200
    for _ in range(3):  # reloads
        for i in range(30):
            assert lookup(client, f"CLS{i:05d}").status_code == 200


def test_the_hundred_and_first_wrong_code_in_a_minute_is_refused_with_retry_after(client):
    for n in range(LIMIT_MINUTE):
        assert lookup(client, f"NOPE{n:05d}").status_code == 404
    refused = lookup(client, "CLS00001")  # even a valid code waits: the budget of the address is spent
    assert refused.status_code == 429
    assert refused.json()["detail"] == "Слишком много попыток. Подождите минуту и попробуйте снова."
    assert 1 <= int(refused.headers["Retry-After"]) <= 60
    assert "Али" not in refused.text and "CLS00001" not in refused.text


def test_confirm_shares_the_budget(client):
    for n in range(LIMIT_MINUTE // 2):
        assert lookup(client, f"NOPE{n:05d}").status_code == 404
        assert confirm(client, f"NOPE{n:05d}").status_code == 400
    assert confirm(client, "CLS00001").status_code == 429
    assert lookup(client, "CLS00001").status_code == 429


def test_a_used_or_completed_code_is_not_a_guess_only_an_unknown_one_is(client, monkeypatch):
    """confirm answers «не найдена или уже начата» for BOTH; only a code that exists nowhere spends the budget."""

    def refuse(self, code):
        raise ValueError("Диагностика не найдена или уже начата")

    monkeypatch.setattr(AuthService, "confirm_diagnostic", refuse)
    for n in range(LIMIT_MINUTE + 20):
        assert confirm(client, f"STATE{n:05d}").status_code == 400  # exists, in_progress / completed / expired
    assert lookup(client, "CLS00001").status_code == 200  # the budget is untouched
    for n in range(LIMIT_MINUTE):
        assert confirm(client, f"NOPE{n:05d}").status_code == 400  # unknown: each one spends
    assert lookup(client, "CLS00001").status_code == 429


def test_retry_after_is_the_longest_of_the_spent_windows(client, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rl, "_monotonic", lambda: now[0])
    spent = 0
    while spent < LIMIT_HOUR:
        for _ in range(LIMIT_MINUTE):
            if spent == LIMIT_HOUR:
                break
            lookup(client, f"NOPE{spent:05d}")
            spent += 1
        if spent < LIMIT_HOUR:
            now[0] += 61
    refused = lookup(client, "CLS00001")  # the last minute is full AND the hour is full
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) > 3000 and "мин" in refused.json()["detail"]


def test_an_expired_link_is_a_state_not_a_guess(client, monkeypatch):
    def expired(self, code):
        raise ValueError("Ссылка на диагностику истекла")

    monkeypatch.setattr(AuthService, "confirm_diagnostic", expired)
    for _ in range(LIMIT_MINUTE + 5):
        assert confirm(client, "CLS00001").status_code == 400
    assert lookup(client, "CLS00001").status_code == 200


def test_another_address_is_not_affected(client):
    for n in range(LIMIT_MINUTE):
        lookup(client, f"NOPE{n:05d}")
    assert lookup(client, "CLS00001").status_code == 429
    assert lookup(client, "CLS00001", ip="198.51.100.9").status_code == 200


def test_the_hour_budget_holds_when_the_minute_budget_is_respected(client, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rl, "_monotonic", lambda: now[0])
    spent = 0
    while spent < LIMIT_HOUR:
        for _ in range(LIMIT_MINUTE):
            if spent == LIMIT_HOUR:
                break
            assert lookup(client, f"NOPE{spent:05d}").status_code == 404
            spent += 1
        now[0] += 61
    refused = lookup(client, "CLS00001")
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) > 60 and "мин" in refused.json()["detail"]


def test_the_budget_comes_back_after_the_window(client, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rl, "_monotonic", lambda: now[0])
    for n in range(LIMIT_MINUTE):
        lookup(client, f"NOPE{n:05d}")
    assert lookup(client, "CLS00001").status_code == 429
    now[0] += 61
    assert lookup(client, "CLS00001").status_code == 200


def test_a_server_to_server_call_without_x_real_ip_is_not_counted(client):
    """diagnostic-service asks from our own address: every student would share ONE budget."""
    for n in range(LIMIT_MINUTE * 3):
        assert lookup(client, f"NOPE{n:05d}", ip=None).status_code == 404
    assert lookup(client, "CLS00001", ip=None).status_code == 200
    assert lookup(client, "CLS00001").status_code == 200  # and no browser budget was spent by those calls


def test_spoofed_forwarded_for_next_to_the_real_ip_does_not_open_a_new_bucket(client):
    for n in range(LIMIT_MINUTE):
        lookup(client, f"NOPE{n:05d}", **{"x-forwarded-for": f"10.0.{n}.1"})
    assert lookup(client, "CLS00001", **{"x-forwarded-for": "192.0.2.5"}).status_code == 429


def test_ipv6_addresses_of_one_subscriber_share_a_bucket(client):
    for n in range(LIMIT_MINUTE):
        lookup(client, f"NOPE{n:05d}", ip=f"2001:db8:5:6:{n:x}::1")
    assert lookup(client, "CLS00001", ip="2001:db8:5:6:ffff::9").status_code == 429
    assert lookup(client, "CLS00001", ip="2001:db8:5:7::9").status_code == 200


def test_peek_window_reads_without_counting(monkeypatch):
    now = [500.0]
    monkeypatch.setattr(rl, "_monotonic", lambda: now[0])
    assert rl.peek_window("k") == (0, 0)
    assert rl.peek_window("k") == (0, 0)
    assert rl.hit_window("k", 60)[0] == 1
    assert rl.peek_window("k") == (1, 60)
    now[0] += 59
    assert rl.peek_window("k") == (1, 1)
    now[0] += 2
    assert rl.peek_window("k") == (0, 0)


def test_peek_window_reads_redis_when_there_is_one(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.values = {"k": "3"}

        def get(self, key):
            return self.values.get(key)

        def ttl(self, key):
            return 42 if key in self.values else -2

    monkeypatch.setattr(rl.redis_client, "get_redis", lambda: FakeRedis())
    assert rl.peek_window("k") == (3, 42)
    assert rl.peek_window("absent") == (0, 0)


def test_a_redis_failure_falls_back_to_memory(monkeypatch):
    class Broken:
        def get(self, key):
            raise ConnectionError("down")

    marked = []
    monkeypatch.setattr(rl.redis_client, "get_redis", lambda: Broken())
    monkeypatch.setattr(rl.redis_client, "mark_down", lambda exc: marked.append(exc))
    assert rl.peek_window("nothing-here") == (0, 0)
    assert marked
