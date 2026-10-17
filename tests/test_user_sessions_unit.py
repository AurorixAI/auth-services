"""The parts of the per-device session logic (ROADMAP #101) that need no database."""
import json
import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

import src.api.internal_router  # noqa: E402,F401  (import order: avoids a pre-existing router cycle)
import src.auth.router as auth_router  # noqa: E402
from src.auth import user_sessions  # noqa: E402
from src.auth.service import AuthService, create_access_token, create_refresh_token  # noqa: E402
from tests.test_session_cookies import API, assert_cleared, client_for, cookie_header, expired_access_token  # noqa: E402


def request_with(headers=None, peer="198.51.100.1"):
    request = MagicMock()
    request.headers = {k.lower(): v for k, v in (headers or {}).items()}
    request.client = MagicMock(host=peer) if peer else None
    return request


def test_the_address_is_the_trusted_one_never_a_client_typed_header():
    spoofed = request_with({"x-real-ip": "203.0.113.7", "x-forwarded-for": "1.1.1.1, 2.2.2.2", "user-agent": "UA/1"})
    client = auth_router._session_client(spoofed)
    assert (client.ip, client.user_agent) == ("203.0.113.7", "UA/1")
    # no X-Real-IP: the RIGHTMOST X-Forwarded-For element (the one our proxy appended), not the leftmost typed one
    assert auth_router._session_client(request_with({"x-forwarded-for": "1.1.1.1, 2.2.2.2"})).ip == "2.2.2.2"
    # a header that is not an address is ignored, the socket peer is used
    assert auth_router._session_client(request_with({"x-real-ip": "<script>", "x-forwarded-for": "nope"})).ip == "198.51.100.1"
    assert auth_router._session_client(request_with(peer=None)).ip is None
    assert auth_router._session_client(None) is None


def test_stored_values_are_bounded_printable_and_the_address_is_an_address():
    client = user_sessions.SessionClient(ip=" 2001:db8::1 ", user_agent="Mozilla\x00/5.0\n" + "x" * 500)
    ip, agent = client.cleaned()
    assert ip == "2001:db8::1"
    assert len(agent) == user_sessions.USER_AGENT_MAX and agent.startswith("Mozilla/5.0x") and "\x00" not in agent
    assert user_sessions.SessionClient(ip="testclient", user_agent="").cleaned() == (None, None)
    assert user_sessions.SessionClient().cleaned() == (None, None)


def test_the_limits_are_the_decided_ones():
    assert user_sessions.GRACE_SECONDS == 60
    assert user_sessions.MAX_ACTIVE_SESSIONS == 20
    assert user_sessions.EXPIRED_RETENTION_DAYS == 30
    assert user_sessions.SUPERSEDED_RETENTION_DAYS == 7
    assert {
        user_sessions.REVOKE_LOGOUT, user_sessions.REVOKE_REFRESH_REUSE, user_sessions.REVOKE_PASSWORD_CHANGED,
        user_sessions.REVOKE_USER_BLOCKED, user_sessions.REVOKE_SESSION_CAP,
    } == {"logout", "refresh_reuse", "password_changed", "user_blocked", "session_cap"}
    assert all(len(r) <= 40 for r in (user_sessions.REVOKE_REFRESH_REUSE, user_sessions.REVOKE_PASSWORD_CHANGED))


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(AuthService, "logout", lambda self, user_id: seen.append(("user", user_id)))
    monkeypatch.setattr(
        AuthService, "logout_by_refresh_token", lambda self, token: seen.append(("refresh", token)) or True
    )
    return seen


def test_logout_with_a_refresh_cookie_revokes_through_the_refresh_token_even_when_the_access_cookie_is_valid(calls):
    """The refresh cookie names the device; the access token names only the user (which could be every device)."""
    refresh = create_refresh_token("u-1")
    r = client_for().post(
        f"{API}/logout", headers=cookie_header(access=create_access_token("u-1", "teacher"), refresh=refresh)
    )
    assert r.status_code == 200
    assert calls == [("refresh", refresh)]


def test_logout_with_an_access_token_alone_never_revokes_a_session():
    """AuthService.logout(user_id) identifies no device: it must not touch the database at all."""
    db = MagicMock()
    AuthService(db).logout("some-user")
    assert db.method_calls == [] and not db.commit.called


def test_logout_by_a_garbage_refresh_token_touches_no_row():
    db = MagicMock()
    assert AuthService(db).logout_by_refresh_token("garbage") is False
    assert db.method_calls == []


# ---- the body of /auth/logout is read by anybody's request: it is bounded and can never break the route ---------

HOSTILE_BODIES = [
    ("deeply nested, small enough to be read: the parser runs out of recursion", "[" * 2000 + "]" * 2000, "application/json"),
    ("deeply nested and oversized", "[" * 100_000, "application/json"),
    ("a valid token inside an oversized object", json.dumps({"refresh_token": "x" * 10, "pad": "y" * 5000}), "application/json"),
    ("not JSON", "not json at all", "application/json"),
    ("a list", "[1, 2]", "application/json"),
    ("an empty body", "", "application/json"),
    ("a token that is not a string", '{"refresh_token": 5}', "application/json"),
    ("a form", "refresh_token=x", "application/x-www-form-urlencoded"),
    ("invalid UTF-8", b'{"refresh_token": "\xff\xfe"}', "application/json"),
]


@pytest.mark.parametrize("why,body,content_type", HOSTILE_BODIES, ids=[h[0][:30] for h in HOSTILE_BODIES])
def test_logout_with_a_hostile_body_is_200_and_clears_the_cookies(calls, why, content_type, body):
    r = client_for().post(
        f"{API}/logout",
        content=body,
        headers={**cookie_header(access=expired_access_token("u-9")), "content-type": content_type},
    )
    assert r.status_code == 200, (why, r.status_code, r.text[:200])
    assert_cleared(r)
    assert calls == []  # no token came out of it


def test_a_chunked_body_without_a_length_is_cut_at_the_limit():
    r = client_for().post(
        f"{API}/logout",
        content=iter([b"[" * 1000] * 20),  # chunked, no Content-Length, 20 KB
        headers={**cookie_header(access=expired_access_token("u-9")), "content-type": "application/json"},
    )
    assert r.status_code == 200, r.text[:200]
    assert_cleared(r)


@pytest.mark.parametrize("body", ["[" * 2000 + "]" * 2000, "[" * 100_000], ids=["nested", "oversized"])
def test_a_cross_site_logout_with_a_deeply_nested_body_is_403_not_500(calls, body):
    r = client_for().post(
        f"{API}/logout",
        content=body,
        headers={**cookie_header(access=expired_access_token("u-9")), "content-type": "application/json", "sec-fetch-site": "cross-site"},
    )
    assert r.status_code == 403
    assert "set-cookie" not in r.headers and calls == []


def test_a_json_body_with_a_charset_and_a_token_is_still_read(calls):
    token = create_refresh_token("u-1")
    r = client_for().post(
        f"{API}/logout",
        content=json.dumps({"refresh_token": token}),
        headers={**cookie_header(access=expired_access_token("u-1")), "content-type": "application/json; charset=utf-8"},
    )
    assert r.status_code == 200
    assert calls == [("refresh", token)]
