"""Brute-force protection of the password endpoints (login, change-password) and of the secret lookups.

Decided design (N13 P2.2 security): per ACCOUNT, after 5 consecutive failures the account is refused for
1, 2, 4, 8, at most 15 minutes (never locked for good); per CLIENT ADDRESS, 30 login attempts a minute and 300
an hour; 429 + Retry-After + «Слишком много попыток входа. Попробуйте через N мин.»; a refused attempt never
reaches the user lookup or the password check; unknown accounts are counted like known ones.

Most tests run on the per-process memory store with a fake clock. The tests marked ``needs_redis`` run the same
logic against a REAL Redis (Lua scripts, TTLs, parallel requests) and need ``THROTTLE_TEST_REDIS_URL`` pointing
at a throwaway instance, e.g. ``redis://localhost:55442/15`` (the database is flushed).
"""
import logging
import os
import re
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

import redis as redis_lib  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import src.api.internal_router  # noqa: E402,F401  (import order: avoids a pre-existing router cycle)
from src.api import main_v2  # noqa: E402
from src.api import rate_limit as rl  # noqa: E402
from src.auth import login_throttle as lt  # noqa: E402
from src.auth import router as auth_router_module  # noqa: E402
from src.auth import service as service_module  # noqa: E402
from src.auth.dependencies import get_current_active_user_payload  # noqa: E402
from src.auth.service import AuthService  # noqa: E402
from src.core import redis_client  # noqa: E402
from src.core.database import get_db  # noqa: E402
from src.core.exceptions import (  # noqa: E402
    AccountInactiveError,
    AccountNotFoundError,
    AlgoException,
    InvalidCredentialsError,
)

REAL_GET_REDIS = redis_client.get_redis  # conftest swaps it for "no Redis" in every test
API = "/api/v1/auth"
IP = "203.0.113.5"
GOOD = "correct-horse-battery"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(lt, "_now", c)
    monkeypatch.setattr(rl, "_monotonic", c)
    monkeypatch.setattr(redis_client, "_now", c)
    return c


class DeadRedis:
    """Every command fails like a Redis that went away."""

    def __getattr__(self, name):
        def boom(*args, **kwargs):
            raise redis_lib.exceptions.ConnectionError("Connection refused")

        return boom


def fail(identifier, scope=lt.SCOPE_LOGIN, ip=IP):
    lt.begin(scope, identifier, ip).failed()


def refused_for(identifier, scope=lt.SCOPE_LOGIN, ip=IP):
    with pytest.raises(lt.TooManyAttempts) as exc:
        lt.begin(scope, identifier, ip)
    return exc.value


def make_app(db=None):
    """The auth router with the service's own error handlers (domain errors -> status codes, CORS-aware
    HTTPException with its headers), so what the tests see is what a client sees."""
    app = FastAPI()
    app.include_router(auth_router_module.router, prefix="/api/v1")
    app.add_exception_handler(HTTPException, main_v2.http_exception_handler)
    app.add_exception_handler(AlgoException, main_v2.algo_exception_handler)
    app.dependency_overrides[get_db] = (lambda: db) if db is not None else (lambda: MagicMock())
    return app


# ---- the schedule ---------------------------------------------------------------------------


def test_backoff_schedule_is_one_two_four_eight_then_fifteen_minutes():
    assert [lt.backoff_seconds(n) for n in range(1, 5)] == [0, 0, 0, 0]
    assert [lt.backoff_seconds(n) for n in range(5, 12)] == [60, 120, 240, 480, 900, 900, 900]
    assert lt.backoff_seconds(10**6) == 900  # no overflow, still the cap


def test_the_decided_numbers():
    assert lt.FAILURE_THRESHOLD == 5
    assert lt.BACKOFF_BASE_SECONDS == 60 and lt.BACKOFF_CAP_SECONDS == 900
    assert lt.QUIET_WINDOW_SECONDS == 900
    assert lt.LOGIN_IP_LIMITS == (("minute", 30, 60), ("hour", 300, 3600))


@pytest.mark.parametrize("retry_after, minutes", [(1, 1), (59, 1), (60, 1), (61, 2), (480, 8), (900, 15)])
def test_the_message_rounds_the_wait_up_to_whole_minutes(retry_after, minutes):
    exc = lt.TooManyAttempts(retry_after)
    assert exc.status_code == 429
    assert exc.detail == f"Слишком много попыток входа. Попробуйте через {minutes} мин."
    assert exc.headers == {"Retry-After": str(retry_after)}


# ---- per account: threshold, growth, cap, reset -----------------------------------------------


def test_four_failures_do_not_block_and_the_fifth_attempt_is_still_checked(clock):
    for _ in range(4):
        fail("t@x.kz")
    attempt = lt.begin(lt.SCOPE_LOGIN, "t@x.kz", IP)  # the 5th is evaluated (it may be the right password)
    attempt.failed()
    assert refused_for("t@x.kz").retry_after == 60  # ...and only the 6th is refused


def test_backoff_doubles_up_to_the_cap_and_never_locks_for_good(clock):
    for _ in range(5):
        fail("t@x.kz")
    waits = []
    for _ in range(8):
        waits.append(refused_for("t@x.kz").retry_after)
        clock.advance(waits[-1])  # the block ends exactly now...
        fail("t@x.kz")  # ...the next attempt is evaluated again, and a failure arms the next, longer wait
    assert waits == [60, 120, 240, 480, 900, 900, 900, 900]
    clock.advance(900)
    lt.begin(lt.SCOPE_LOGIN, "t@x.kz", IP).succeeded()  # a correct password after the wait works


def test_the_block_ends_on_time(clock):
    for _ in range(5):
        fail("t@x.kz")
    clock.advance(59)
    assert refused_for("t@x.kz").retry_after == 1
    clock.advance(1)
    lt.begin(lt.SCOPE_LOGIN, "t@x.kz", IP).failed()  # allowed again


def test_refused_attempts_do_not_extend_the_block_or_the_counter(clock):
    for _ in range(5):
        fail("t@x.kz")
    for _ in range(200):  # an attacker hammering the locked account
        clock.advance(0.25)
        refused_for("t@x.kz")
    clock.advance(10.01)  # 60 s since the 5th failure
    fail("t@x.kz")  # the 6th attempt: if refused attempts had counted, this would be a much longer block
    assert refused_for("t@x.kz").retry_after == 120


def test_a_success_resets_the_counter(clock):
    for _ in range(4):
        fail("t@x.kz")
    lt.begin(lt.SCOPE_LOGIN, "t@x.kz", IP).succeeded()
    for _ in range(5):  # a full fresh set of five tries
        fail("t@x.kz")
    assert refused_for("t@x.kz").retry_after == 60  # 60, not a longer step of the earlier run


def test_a_success_after_a_block_starts_the_schedule_over(clock):
    for _ in range(5):
        fail("t@x.kz")
    for _ in range(2):  # climb to the 4-minute step
        clock.advance(refused_for("t@x.kz").retry_after)
        fail("t@x.kz")
    clock.advance(refused_for("t@x.kz").retry_after)
    lt.begin(lt.SCOPE_LOGIN, "t@x.kz", IP).succeeded()
    for _ in range(5):
        fail("t@x.kz")
    assert refused_for("t@x.kz").retry_after == 60


def test_failures_are_forgotten_after_a_quiet_quarter_of_an_hour(clock):
    for _ in range(4):
        fail("t@x.kz")
    clock.advance(lt.QUIET_WINDOW_SECONDS + 1)
    for _ in range(4):  # without the reset the first of these would already be the 5th failure
        fail("t@x.kz")
    lt.begin(lt.SCOPE_LOGIN, "t@x.kz", IP).failed()
    assert refused_for("t@x.kz").retry_after == 60


def test_accounts_are_independent(clock):
    for _ in range(5):
        fail("a@x.kz")
    refused_for("a@x.kz")
    lt.begin(lt.SCOPE_LOGIN, "b@x.kz", IP).failed()  # another account is not touched
    lt.begin(lt.SCOPE_CHANGE_PASSWORD, "a@x.kz", IP).failed()  # nor is the same text in another scope


def test_a_request_that_dies_with_a_server_error_is_not_counted(clock):
    for _ in range(4):
        fail("t@x.kz")
    for _ in range(10):  # database down: every try ends with a 500, none of them says anything about the password
        lt.begin(lt.SCOPE_LOGIN, "t@x.kz", IP).aborted()
    fail("t@x.kz")  # still only the 5th attempt
    assert refused_for("t@x.kz").retry_after == 60


def test_parallel_guesses_cannot_all_slip_through_a_check_then_count_gap(clock):
    outcomes = []
    barrier = threading.Barrier(40)

    def guess():
        barrier.wait()
        try:
            lt.begin(lt.SCOPE_LOGIN, "t@x.kz", IP).failed()
            outcomes.append("evaluated")
        except lt.TooManyAttempts:
            outcomes.append("refused")

    threads = [threading.Thread(target=guess) for _ in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count("evaluated") == 5 and outcomes.count("refused") == 35


# ---- the key: normalisation, no raw identifier ------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        "Teacher@X.KZ",
        "  teacher@x.kz  ",
        "\tteacher@x.kz\n",
        "ＴＥＡＣＨＥＲ＠Ｘ．ＫＺ",  # full-width forms
        "teacher@x.kz​",  # zero-width space
        "teacher‍@x.kz",  # zero-width joiner
        "﻿teacher@x.kz",  # byte-order mark
        "teacher@x.kz ",  # no-break space
        "teacher@x.kz ",  # em space
        "TEACHER@x.kz‮",  # right-to-left override
    ],
)
def test_every_spelling_of_one_identifier_shares_one_counter(variant):
    assert lt._key_base("login", variant) == lt._key_base("login", "teacher@x.kz")


def test_unicode_case_folding_does_not_split_a_key():
    assert lt._key_base("login", "straße@x.kz") == lt._key_base("login", "STRASSE@x.kz")


def test_different_identifiers_and_scopes_get_different_keys():
    assert lt._key_base("login", "a@x.kz") != lt._key_base("login", "b@x.kz")
    assert lt._key_base("login", "a@x.kz") != lt._key_base("change-password", "a@x.kz")
    assert lt._key_base("login", "a+1@x.kz") != lt._key_base("login", "a@x.kz")  # separate real mailboxes


def test_the_key_never_contains_the_identifier_and_depends_on_the_secret(monkeypatch):
    key = lt._key_base("login", "teacher@x.kz")
    assert "teacher" not in key and "x.kz" not in key
    assert re.fullmatch(r"lt:login:[0-9a-f]{32}", key)
    monkeypatch.setattr(lt, "get_settings", lambda: SimpleNamespace(secret_key="another-secret-key-of-32-chars-or-more"))
    assert lt._key_base("login", "teacher@x.kz") != key  # an offline dictionary needs the secret too


def test_the_memory_store_holds_only_hashed_keys(clock):
    fail("teacher@x.kz")
    assert list(lt._memory) and all("teacher" not in k for k in lt._memory)


# ---- logging ---------------------------------------------------------------------------------


def test_one_warning_when_an_account_enters_backoff_and_no_secret_in_it(clock, caplog):
    caplog.set_level(logging.WARNING, logger="src.auth.login_throttle")
    for _ in range(4):
        fail("teacher@x.kz")
    assert caplog.records == []
    fail("teacher@x.kz")
    for _ in range(20):
        refused_for("teacher@x.kz")  # refused attempts are silent
    assert len(caplog.records) == 1
    line = caplog.records[0].getMessage()
    assert re.search(r"key=[0-9a-f]{8} ", line) and f"ip={IP}" in line and "attempts=5" in line
    assert "block_seconds=60" in line
    assert "teacher" not in line and "x.kz" not in line


# ---- the endpoint: /auth/login ----------------------------------------------------------------


def login_app(monkeypatch):
    calls = []

    def fake_login(self, request):
        calls.append((request.email, request.password))
        if request.email.startswith("ghost"):
            raise AccountNotFoundError()
        if request.password != GOOD:
            raise InvalidCredentialsError()
        return SimpleNamespace(
            model_dump=lambda mode="json": {"ok": True},
            tokens=SimpleNamespace(access_token="a.b.c", refresh_token="d.e.f"),
        )

    monkeypatch.setattr(AuthService, "login", fake_login)
    return TestClient(make_app()), calls


def post_login(client, email, password="wrong-password", ip=IP, headers=None):
    return client.post(
        f"{API}/login",
        json={"email": email, "password": password},
        headers={"x-real-ip": ip, **(headers or {})},
    )


def test_login_answers_429_with_the_russian_message_and_retry_after(monkeypatch, clock):
    client, calls = login_app(monkeypatch)
    codes = [post_login(client, "teacher@x.kz").status_code for _ in range(5)]
    assert codes == [401] * 5
    r = post_login(client, "teacher@x.kz")
    assert r.status_code == 429
    assert r.json() == {"detail": "Слишком много попыток входа. Попробуйте через 1 мин."}
    assert r.headers["retry-after"] == "60"
    assert len(calls) == 5  # the sixth never reached the login service


def test_even_the_right_password_is_refused_while_the_account_is_in_backoff(monkeypatch, clock):
    client, calls = login_app(monkeypatch)
    for _ in range(5):
        post_login(client, "teacher@x.kz")
    assert post_login(client, "teacher@x.kz", GOOD).status_code == 429
    assert len(calls) == 5
    clock.advance(61)
    ok = post_login(client, "teacher@x.kz", GOOD)
    assert ok.status_code == 200 and "algo_auth_token" in ok.headers["set-cookie"]


def test_a_refused_attempt_never_looks_up_the_user_or_checks_the_password(monkeypatch, clock):
    """The real AuthService.login on a mocked session: while blocked, neither the database nor bcrypt is touched."""
    verified = []
    monkeypatch.setattr(service_module, "verify_password", lambda plain, hashed: verified.append(plain) or False)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
        is_active=True, password_hash="hash", email="teacher@x.kz"
    )
    client = TestClient(make_app(db))
    for _ in range(5):
        assert post_login(client, "teacher@x.kz", "guess").status_code == 401
    assert len(verified) == 5 and db.query.call_count == 5
    monkeypatch.setattr(service_module, "verify_password", lambda plain, hashed: verified.append(plain) or True)
    assert post_login(client, "teacher@x.kz", "the-right-one").status_code == 429
    assert len(verified) == 5 and db.query.call_count == 5  # untouched


def test_a_success_resets_the_account_through_the_endpoint(monkeypatch, clock):
    client, _ = login_app(monkeypatch)
    for _ in range(4):
        post_login(client, "teacher@x.kz")
    assert post_login(client, "teacher@x.kz", GOOD).status_code == 200
    codes = [post_login(client, "teacher@x.kz").status_code for _ in range(5)]
    assert codes == [401] * 5  # five fresh tries, not one
    assert post_login(client, "teacher@x.kz").status_code == 429


def test_an_unknown_account_is_counted_and_answered_like_a_known_one(monkeypatch, clock):
    client, _ = login_app(monkeypatch)
    known = [post_login(client, "teacher@x.kz") for _ in range(6)]
    ghost = [post_login(client, "ghost@x.kz") for _ in range(6)]
    # (the 401 "wrong password" vs 404 "no such account" answers are the service's existing, deliberate contract;
    # the throttle adds nothing to them: same threshold, same 429 body, same wait)
    assert [r.status_code for r in known] == [401] * 5 + [429]
    assert [r.status_code for r in ghost] == [404] * 5 + [429]
    assert known[5].json() == ghost[5].json()
    assert known[5].headers["retry-after"] == ghost[5].headers["retry-after"]


def test_the_wait_grows_through_the_endpoint_up_to_fifteen_minutes(monkeypatch, clock):
    client, _ = login_app(monkeypatch)
    for _ in range(5):
        post_login(client, "teacher@x.kz")
    seen = []
    for _ in range(5):
        r = post_login(client, "teacher@x.kz")
        seen.append((r.headers["retry-after"], r.json()["detail"].split("через ")[1]))
        clock.advance(int(r.headers["retry-after"]) + 1)
        assert post_login(client, "teacher@x.kz").status_code == 401
    assert seen == [("60", "1 мин."), ("120", "2 мин."), ("240", "4 мин."), ("480", "8 мин."), ("900", "15 мин.")]


def test_case_whitespace_and_width_variants_of_the_email_share_one_counter(monkeypatch, clock):
    client, _ = login_app(monkeypatch)
    spellings = ["Teacher@X.kz", " teacher@x.kz ", "TEACHER@X.KZ", "ＴＥＡＣＨＥＲ＠Ｘ．ＫＺ", "teacher@x.kz​"]
    assert [post_login(client, s).status_code for s in spellings] == [401] * 5
    assert post_login(client, "teacher@x.kz").status_code == 429


def test_changing_the_address_does_not_reset_the_account_counter(monkeypatch, clock):
    client, calls = login_app(monkeypatch)
    codes = [post_login(client, "teacher@x.kz", ip=f"198.51.100.{i}").status_code for i in range(7)]
    assert codes == [401] * 5 + [429] * 2
    assert len(calls) == 5


def test_other_accounts_on_the_same_address_are_not_blocked(monkeypatch, clock):
    client, _ = login_app(monkeypatch)
    for _ in range(6):
        post_login(client, "victim@x.kz")
    assert post_login(client, "colleague@x.kz", GOOD).status_code == 200


def test_an_inactive_account_is_counted_like_any_other_failure(monkeypatch, clock):
    def inactive(self, request):
        raise AccountInactiveError()

    monkeypatch.setattr(AuthService, "login", inactive)
    client = TestClient(make_app())
    assert [post_login(client, "off@x.kz").status_code for _ in range(6)] == [403] * 5 + [429]


def test_a_server_error_is_not_counted_against_the_account(monkeypatch, clock):
    def broken(self, request):
        raise RuntimeError("database is down")

    monkeypatch.setattr(AuthService, "login", broken)
    client = TestClient(make_app(), raise_server_exceptions=False)
    assert [post_login(client, "teacher@x.kz").status_code for _ in range(8)] == [500] * 8


# ---- per client address ----------------------------------------------------------------------


def test_thirty_attempts_a_minute_per_address_then_429_with_retry_after(monkeypatch, clock):
    client, _ = login_app(monkeypatch)
    codes = [post_login(client, f"user{i}@x.kz").status_code for i in range(30)]
    assert codes == [401] * 30
    r = post_login(client, "user30@x.kz")
    assert r.status_code == 429
    assert r.json()["detail"] == "Слишком много попыток входа. Попробуйте через 1 мин."
    assert 1 <= int(r.headers["retry-after"]) <= 60
    assert post_login(client, "user31@x.kz", ip="198.51.100.7").status_code == 401  # another address is unaffected
    clock.advance(61)
    assert post_login(client, "user32@x.kz").status_code == 401  # the window is over


def test_three_hundred_attempts_an_hour_per_address(monkeypatch, clock):
    client, _ = login_app(monkeypatch)
    total = 0
    for minute in range(10):  # 30 a minute for ten minutes: all answered
        for _ in range(30):
            assert post_login(client, f"user{total}@x.kz").status_code == 401
            total += 1
        clock.advance(61)
    r = post_login(client, "one-too-many@x.kz")
    assert r.status_code == 429
    assert 2900 <= int(r.headers["retry-after"]) <= 3000
    assert r.json()["detail"] == "Слишком много попыток входа. Попробуйте через 50 мин."


def test_a_school_behind_one_address_logs_in_at_eight_without_being_blocked(monkeypatch, clock):
    client, _ = login_app(monkeypatch)
    assert {post_login(client, f"teacher{i}@school.kz", GOOD).status_code for i in range(25)} == {200}


def test_a_spoofed_leftmost_forwarded_element_cannot_escape_the_address_limit(monkeypatch, clock):
    client, _ = login_app(monkeypatch)
    codes = [
        client.post(
            f"{API}/login",
            json={"email": f"user{i}@x.kz", "password": "x-wrong"},
            headers={"x-forwarded-for": f"10.99.0.{i}, 203.0.113.9"},  # a "new client" every time, same real peer
        ).status_code
        for i in range(31)
    ]
    assert codes == [401] * 30 + [429]


def test_a_spoofed_forwarded_for_next_to_x_real_ip_cannot_escape_the_address_limit(monkeypatch, clock):
    client, _ = login_app(monkeypatch)
    codes = [
        post_login(client, f"user{i}@x.kz", headers={"x-forwarded-for": f"10.99.0.{i}"}).status_code
        for i in range(31)
    ]
    assert codes == [401] * 30 + [429]


def test_retry_after_survives_the_cors_error_handler(clock):
    from src.api import main_v2

    app = FastAPI()
    app.add_exception_handler(HTTPException, main_v2.http_exception_handler)

    @app.get("/limited")
    def limited():
        raise lt.TooManyAttempts(120)

    origin = main_v2._allowed_origins[0]
    r = TestClient(app).get("/limited", headers={"Origin": origin})
    assert r.status_code == 429 and r.headers["retry-after"] == "120"
    assert r.headers.get("access-control-allow-origin") == origin


# ---- the other endpoints that check a secret -------------------------------------------------


def password_app(monkeypatch, user_id="user-1"):
    def wrong_old_password(self, uid, old, new):
        if old != GOOD:
            raise ValueError("Неверный текущий пароль")
        return True

    monkeypatch.setattr(AuthService, "change_password", wrong_old_password)
    app = make_app()
    who = {"sub": user_id}
    app.dependency_overrides[get_current_active_user_payload] = lambda: who
    return TestClient(app), who


def change(client, old="wrong-old-pw"):
    return client.post(f"{API}/change-password", json={"old_password": old, "new_password": "brand-new-pw"})


def test_change_password_is_throttled_per_user_like_login(monkeypatch, clock):
    client, who = password_app(monkeypatch)
    codes = [change(client).status_code for _ in range(5)]
    assert codes == [400] * 5
    r = change(client, GOOD)  # the right old password does not help while the user is in backoff
    assert r.status_code == 429 and r.headers["retry-after"] == "60"
    assert "Попробуйте через 1 мин." in r.json()["detail"]
    who["sub"] = "user-2"  # somebody else is untouched
    assert change(client, GOOD).status_code == 200
    who["sub"] = "user-1"
    clock.advance(61)
    assert change(client, GOOD).status_code == 200  # after the wait the right password works
    assert [change(client).status_code for _ in range(5)] == [400] * 5  # and the counter started over


def test_change_password_and_login_have_separate_counters(monkeypatch, clock):
    client, _ = password_app(monkeypatch, user_id="teacher@x.kz")
    for _ in range(5):
        change(client)
    assert change(client).status_code == 429
    fail("teacher@x.kz", scope=lt.SCOPE_LOGIN)  # login for the same text is a different bucket


def test_invite_and_activation_lookups_share_a_per_address_limit(monkeypatch, clock):
    def bad_invite(self, request):
        raise ValueError("Приглашение не найдено или уже использовано")

    def no_invite(self, code):
        raise ValueError("Приглашение не найдено")

    monkeypatch.setattr(AuthService, "accept_invitation", bad_invite)
    monkeypatch.setattr(AuthService, "get_invitation_info", no_invite)
    client = TestClient(make_app())
    body = {"code": "ABCD1234", "email": "n@x.kz", "password": "secret-pw", "first_name": "А", "last_name": "Б"}
    headers = {"x-real-ip": IP}
    codes = []
    for i in range(30):
        if i % 3:
            codes.append(client.post(f"{API}/invite/accept", json=body, headers=headers).status_code)
        else:
            codes.append(client.get(f"{API}/invite/ABCD{i:04d}", headers=headers).status_code)
    assert set(codes) == {400, 404}
    assert client.post(f"{API}/invite/accept", json=body, headers=headers).status_code == 429
    assert client.post(f"{API}/activate", json={"token": "t" * 20}, headers=headers).status_code == 429
    assert client.get(f"{API}/invite/ABCD0000", headers={"x-real-ip": "198.51.100.1"}).status_code == 404


# ---- Redis: fallback and the shared client ---------------------------------------------------


def test_with_redis_down_the_limits_still_hold_in_process_and_the_outage_is_logged_once(monkeypatch, clock, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(redis_client, "get_redis", REAL_GET_REDIS)
    monkeypatch.setattr(redis_lib.Redis, "from_url", staticmethod(lambda *a, **k: DeadRedis()))
    client, calls = login_app(monkeypatch)
    codes = [post_login(client, "teacher@x.kz").status_code for _ in range(7)]
    assert codes == [401] * 5 + [429] * 2  # fail open for availability, but still limited per process
    assert len(calls) == 5
    other = [post_login(client, f"u{i}@x.kz", ip="198.51.100.9").status_code for i in range(31)]
    assert other == [401] * 30 + [429]  # the address limit as well
    outage = [r for r in caplog.records if "Redis unavailable" in r.getMessage()]
    assert len(outage) == 1  # one line per outage, not one per request


def test_the_redis_client_is_retried_after_the_pause_not_on_every_request(monkeypatch, clock):
    monkeypatch.setattr(redis_client, "get_redis", REAL_GET_REDIS)
    dead = DeadRedis()
    monkeypatch.setattr(redis_lib.Redis, "from_url", staticmethod(lambda *a, **k: dead))
    assert redis_client.get_redis() is dead
    redis_client.mark_down(ConnectionError("x"))
    assert redis_client.get_redis() is None
    clock.advance(redis_client.RETRY_AFTER_FAILURE_SECONDS - 1)
    assert redis_client.get_redis() is None
    clock.advance(2)
    assert redis_client.get_redis() is dead


def test_a_missing_redis_url_or_package_degrades_to_memory_not_to_an_error(monkeypatch, clock):
    monkeypatch.setattr(redis_client, "get_redis", REAL_GET_REDIS)

    def explode(*args, **kwargs):
        raise ValueError("bad url")

    monkeypatch.setattr(redis_lib.Redis, "from_url", staticmethod(explode))
    assert redis_client.get_redis() is None
    assert lt.begin(lt.SCOPE_LOGIN, "t@x.kz", IP) is not None


def test_the_refresh_limiter_falls_back_to_memory_when_redis_errors(monkeypatch, clock):
    monkeypatch.setattr(redis_client, "get_redis", lambda: DeadRedis())
    dep = rl.rate_limit("2/minute")
    request = MagicMock()
    request.headers.get = lambda name, default=None: {"x-real-ip": IP}.get(name.lower(), default)
    request.url.path = "/p"
    dep(request)
    dep(request)
    with pytest.raises(HTTPException) as exc:
        dep(request)
    assert exc.value.status_code == 429


def test_hit_window_counts_inside_a_fixed_window_and_reports_the_time_left(clock):
    assert rl.hit_window("k", 60) == (1, 60)
    clock.advance(10)
    assert rl.hit_window("k", 60) == (2, 50)
    clock.advance(50.5)
    assert rl.hit_window("k", 60)[0] == 1  # a new window


# ---- real Redis ------------------------------------------------------------------------------

LIVE_URL = os.getenv("THROTTLE_TEST_REDIS_URL")
needs_redis = pytest.mark.skipif(not LIVE_URL, reason="needs THROTTLE_TEST_REDIS_URL (a throwaway Redis)")


@pytest.fixture
def live(monkeypatch):
    assert LIVE_URL.split("//")[1].split(":")[0] in ("localhost", "127.0.0.1"), "flushes the database: local only"
    client = redis_lib.Redis.from_url(LIVE_URL, decode_responses=True)
    client.flushdb()
    monkeypatch.setattr(redis_client, "get_redis", lambda: client)
    yield client
    client.flushdb()
    client.close()


@needs_redis
def test_live_redis_blocks_after_five_with_real_ttls(live):
    base = lt._key_base("login", "t@x.kz")
    for _ in range(5):
        fail("t@x.kz")
    assert 1 <= refused_for("t@x.kz").retry_after <= 60
    assert 0 < live.ttl(f"{base}:b") <= 60
    assert 900 < live.ttl(f"{base}:n") <= 960  # forgotten a quiet quarter hour after the block ends
    assert not lt._memory  # nothing was kept in process memory: the state is in Redis


@needs_redis
def test_live_redis_backoff_grows_and_a_success_clears_both_keys(live):
    base = lt._key_base("login", "t@x.kz")
    for _ in range(5):
        fail("t@x.kz")
    live.pexpire(f"{base}:b", 1)  # let the first block run out
    time.sleep(0.05)
    fail("t@x.kz")  # allowed again; the failure arms the next step
    assert 119 <= refused_for("t@x.kz").retry_after <= 120
    live.delete(f"{base}:b")
    lt.begin(lt.SCOPE_LOGIN, "t@x.kz", IP).succeeded()
    assert live.exists(f"{base}:n", f"{base}:b") == 0
    lt.begin(lt.SCOPE_LOGIN, "t@x.kz", IP).succeeded()  # and a stale reset is harmless


@needs_redis
def test_live_redis_sixty_parallel_guesses_evaluate_exactly_five(live):
    outcomes = []
    barrier = threading.Barrier(60)

    def guess():
        barrier.wait()
        try:
            lt.begin(lt.SCOPE_LOGIN, "t@x.kz", IP).failed()
            outcomes.append("evaluated")
        except lt.TooManyAttempts:
            outcomes.append("refused")

    threads = [threading.Thread(target=guess) for _ in range(60)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count("evaluated") == 5 and outcomes.count("refused") == 55


@needs_redis
def test_live_redis_state_is_shared_between_workers(live, monkeypatch):
    for _ in range(5):
        fail("t@x.kz")
    lt._memory.clear()  # "another worker process" has no memory of it, only Redis does
    other_worker = redis_lib.Redis.from_url(LIVE_URL, decode_responses=True)
    monkeypatch.setattr(redis_client, "get_redis", lambda: other_worker)
    try:
        assert refused_for("t@x.kz").retry_after >= 1
    finally:
        other_worker.close()


@needs_redis
def test_live_redis_hit_window_counts_expires_and_heals_a_key_without_ttl(live):
    assert rl.hit_window("rl:t", 60)[0] == 1
    count, left = rl.hit_window("rl:t", 60)
    assert count == 2 and 1 <= left <= 60
    live.set("rl:orphan", 5)  # what a crash between INCR and EXPIRE used to leave: a counter that never expires
    count, left = rl.hit_window("rl:orphan", 60)
    assert count == 6 and live.ttl("rl:orphan") > 0


@needs_redis
def test_live_redis_the_endpoint_limits_hold(monkeypatch, live):
    client, calls = login_app(monkeypatch)
    codes = [post_login(client, "teacher@x.kz").status_code for _ in range(7)]
    assert codes == [401] * 5 + [429] * 2 and len(calls) == 5
    other = [post_login(client, f"u{i}@x.kz", ip="198.51.100.9").status_code for i in range(31)]
    assert other == [401] * 30 + [429]
    assert live.get("lt:ip:login:minute:198.51.100.9") == "31"


@needs_redis
def test_live_redis_the_refresh_limiter_now_uses_redis(live):
    dep = rl.rate_limit("2/minute", endpoint="live-check")
    request = MagicMock()
    request.headers.get = lambda name, default=None: {"x-real-ip": IP}.get(name.lower(), default)
    request.url.path = "/p"
    dep(request)
    dep(request)
    assert live.get(f"rl:live-check:{IP}") == "2" and live.ttl(f"rl:live-check:{IP}") > 0
    with pytest.raises(HTTPException):
        dep(request)
    assert not rl._memory_store  # the process memory was not used
