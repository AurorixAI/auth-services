"""Which address a rate limit counts, and how /auth/refresh is limited.

The service sits behind our own nginx, which is directly on the internet: it sets ``X-Real-IP $remote_addr``
(overwriting what the client sent) and APPENDS the peer to ``X-Forwarded-For``. The LEFTMOST element of
that header is therefore client input - taking it let anybody pick a new rate-limit bucket per request.
"""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import src.api.internal_router  # noqa: E402,F401  (import order: avoids a pre-existing router cycle)
from src.api import rate_limit as rl  # noqa: E402
from src.auth import router as auth_router_module  # noqa: E402
from src.auth.service import AuthService, create_refresh_token, hash_token  # noqa: E402
from src.core.database import get_db  # noqa: E402

API = "/api/v1/auth"


def request_with(headers=None, host="10.0.0.9", path="/test"):
    request = MagicMock()
    lowered = {k.lower(): v for k, v in (headers or {}).items()}
    request.headers.get = lambda name, default=None: lowered.get(name.lower(), default)
    request.client = SimpleNamespace(host=host) if host else None
    request.url.path = path
    return request


@pytest.fixture(autouse=True)
def memory_limiter(monkeypatch):
    """Deterministic: the in-memory window, an empty store, no Redis."""
    rl._memory_store.clear()
    monkeypatch.setattr(rl, "_check_redis", rl._check_memory)
    yield
    rl._memory_store.clear()


# ---- the address ------------------------------------------------------------------


def test_x_real_ip_is_the_client_address():
    assert rl.client_ip(request_with({"X-Real-IP": "203.0.113.7"})) == "203.0.113.7"


def test_x_real_ip_beats_a_spoofed_x_forwarded_for():
    request = request_with({"X-Real-IP": "203.0.113.7", "X-Forwarded-For": "1.2.3.4, 5.6.7.8, 203.0.113.7"})
    assert rl.client_ip(request) == "203.0.113.7"


def test_without_x_real_ip_the_rightmost_forwarded_element_counts_not_the_leftmost():
    # nginx appends the real peer; whatever stands before it was typed by the client
    request = request_with({"X-Forwarded-For": "9.9.9.9, 8.8.8.8, 203.0.113.50"})
    assert rl.client_ip(request) == "203.0.113.50"
    assert rl.client_ip(request_with({"X-Forwarded-For": "203.0.113.50"})) == "203.0.113.50"
    assert rl.client_ip(request_with({"X-Forwarded-For": "9.9.9.9 ,  203.0.113.51 "})) == "203.0.113.51"


def test_without_any_proxy_header_the_socket_peer_counts():
    assert rl.client_ip(request_with(host="10.1.2.3")) == "10.1.2.3"
    assert rl.client_ip(request_with(host=None)) == "unknown"


def test_garbage_in_the_headers_is_ignored_not_trusted():
    request = request_with({"X-Real-IP": "not-an-ip", "X-Forwarded-For": "1.1.1.1, <script>"}, host="10.1.2.3")
    assert rl.client_ip(request) == "10.1.2.3"
    request = request_with({"X-Real-IP": "x" * 5000, "X-Forwarded-For": "1.1.1.1, 2.2.2.2"})
    assert rl.client_ip(request) == "2.2.2.2"


def test_ipv6_is_normalised():
    assert rl.client_ip(request_with({"X-Real-IP": "2001:DB8::0001"})) == "2001:db8::1"
    assert rl.client_ip(request_with({"X-Real-IP": "::ffff:203.0.113.7"})) == "::ffff:203.0.113.7"


# ---- an IPv6 client is one bucket per /64 --------------------------------------------


def test_an_ipv4_address_is_its_own_bucket_unchanged():
    assert rl.limit_bucket("203.0.113.7") == "203.0.113.7"
    assert rl.client_bucket(request_with({"X-Real-IP": "203.0.113.7"})) == "203.0.113.7"


def test_ipv6_addresses_of_one_slash_64_share_a_bucket_and_other_prefixes_do_not():
    a = rl.limit_bucket("2001:db8:1:2:aaaa:bbbb:cccc:dddd")
    assert a == "2001:db8:1:2::/64"
    assert rl.limit_bucket("2001:db8:1:2::1") == rl.limit_bucket("2001:DB8:1:2:ffff:ffff:ffff:ffff") == a
    assert rl.limit_bucket("2001:db8:1:3::1") != a  # the next /64 is another subscriber
    assert rl.limit_bucket("2001:db8:2:2::1") != a


def test_an_ipv4_mapped_ipv6_address_is_the_ipv4_client_not_one_shared_bucket():
    # a dual-stack socket shows 203.0.113.7 as ::ffff:203.0.113.7; counting it by /64 would put every IPv4 client in "::/64"
    assert rl.limit_bucket("::ffff:203.0.113.7") == "203.0.113.7"
    assert rl.limit_bucket("::ffff:203.0.113.8") != rl.limit_bucket("::ffff:203.0.113.7")


def test_not_an_address_is_left_alone():
    assert rl.limit_bucket("unknown") == "unknown"
    assert rl.client_bucket(request_with(host=None)) == "unknown"


def test_every_source_address_of_one_slash_64_shares_the_rate_limit():
    dep = rl.rate_limit("3/minute")
    for i in range(3):  # a new source address in the same /64 for every request
        dep(request_with({"X-Real-IP": f"2001:db8:1:2:{i:x}:{i:x}:{i:x}:{i:x}"}))
    with pytest.raises(HTTPException) as exc:
        dep(request_with({"X-Real-IP": "2001:db8:1:2:9999::1"}))
    assert exc.value.status_code == 429
    dep(request_with({"X-Real-IP": "2001:db8:1:3::1"}))  # a different /64 has its own bucket


def test_the_forwarded_for_fallback_is_bucketed_too():
    dep = rl.rate_limit("2/minute")
    for i in range(2):
        dep(request_with({"X-Forwarded-For": f"1.1.1.1, 2001:db8:5:6:{i}::1"}))
    with pytest.raises(HTTPException):
        dep(request_with({"X-Forwarded-For": "9.9.9.9, 2001:db8:5:6:77::1"}))


# ---- a spoofed header cannot pick a fresh bucket ----------------------------------


def test_spoofing_the_leftmost_forwarded_element_does_not_escape_the_limit():
    dep = rl.rate_limit("3/minute")
    for i in range(3):  # a "new client" in the leftmost slot every time, the same peer on the right
        dep(request_with({"X-Forwarded-For": f"10.99.0.{i}, 203.0.113.9"}))
    with pytest.raises(HTTPException) as exc:
        dep(request_with({"X-Forwarded-For": "10.99.0.77, 203.0.113.9"}))
    assert exc.value.status_code == 429


def test_spoofing_x_forwarded_for_next_to_x_real_ip_does_not_escape_the_limit():
    dep = rl.rate_limit("3/minute")
    for i in range(3):
        dep(request_with({"X-Real-IP": "203.0.113.9", "X-Forwarded-For": f"10.99.0.{i}"}))
    with pytest.raises(HTTPException):
        dep(request_with({"X-Real-IP": "203.0.113.9", "X-Forwarded-For": "10.99.1.1"}))


def test_different_real_clients_have_their_own_buckets():
    dep = rl.rate_limit("2/minute")
    for ip in ("203.0.113.1", "203.0.113.2", "203.0.113.3"):
        dep(request_with({"X-Real-IP": ip}))
        dep(request_with({"X-Real-IP": ip}))
    with pytest.raises(HTTPException):
        dep(request_with({"X-Real-IP": "203.0.113.1"}))


# ---- /auth/refresh: per token (tight) and per address (generous) ------------------


def refresh_client(monkeypatch):
    seen = []

    def refuse(self, token):
        seen.append(token)
        raise ValueError("Refresh token отозван")

    monkeypatch.setattr(AuthService, "refresh_tokens", refuse)
    app = FastAPI()
    app.include_router(auth_router_module.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: MagicMock()
    return TestClient(app), seen


def refresh(client, token, real_ip="203.0.113.5", forwarded=None, via_cookie=False):
    headers = {"x-real-ip": real_ip}
    if forwarded:
        headers["x-forwarded-for"] = forwarded
    # K2-1 (A2): the refresh token is read from the cookie only (``via_cookie`` stays for the callers; both mean the cookie)
    headers["cookie"] = f"algo_refresh_token={token}"
    return client.post(f"{API}/refresh", json={}, headers=headers)


def test_the_limits_are_what_the_task_decided():
    assert auth_router_module.REFRESH_TOKEN_LIMIT == "10/minute"
    assert rl._parse_rate(auth_router_module.REFRESH_IP_LIMIT) == (120, 60)


def test_one_refresh_token_is_limited_to_ten_a_minute_whatever_the_address(monkeypatch):
    client, seen = refresh_client(monkeypatch)
    token = create_refresh_token("u-1")
    codes = [refresh(client, token, real_ip=f"203.0.113.{i}", forwarded=f"6.6.6.{i}").status_code for i in range(12)]
    assert codes == [401] * 10 + [429] * 2  # the handler answered 10 times, then the token limiter closed
    assert len(seen) == 10
    # the same token in the cookie is the same bucket
    assert refresh(client, token, via_cookie=True).status_code == 429
    # another token of the same client is untouched
    assert refresh(client, create_refresh_token("u-2")).status_code == 401


def test_the_token_bucket_never_holds_the_token_itself(monkeypatch):
    client, _ = refresh_client(monkeypatch)
    token = create_refresh_token("u-3")
    refresh(client, token)
    keys = list(rl._memory_store)
    assert any(hash_token(token) in key for key in keys)
    assert not any(token in key for key in keys)


def test_a_request_without_a_token_is_401_and_not_counted_against_a_token(monkeypatch):
    client, _ = refresh_client(monkeypatch)
    for _ in range(15):
        r = client.post(f"{API}/refresh", json={}, headers={"x-real-ip": "203.0.113.5"})
        assert r.status_code == 401 and r.json()["detail"] == "refresh_token required"


def test_a_school_behind_one_address_is_not_throttled_by_the_ip_limit(monkeypatch):
    """Everybody returns at 08:00: 100 different browsers, one NAT address -> all answered."""
    client, _ = refresh_client(monkeypatch)
    codes = {refresh(client, create_refresh_token(f"teacher-{i}")).status_code for i in range(100)}
    assert codes == {401}


def test_the_ip_limit_is_still_there_and_cannot_be_spoofed_around(monkeypatch):
    client, _ = refresh_client(monkeypatch)
    codes = [
        refresh(client, create_refresh_token(f"bot-{i}"), forwarded=f"10.0.{i // 250}.{i % 250}").status_code
        for i in range(125)
    ]
    assert codes[:120] == [401] * 120
    assert codes[120:] == [429] * 5
    # a different real client is not affected
    assert refresh(client, create_refresh_token("other"), real_ip="203.0.113.99").status_code == 401
