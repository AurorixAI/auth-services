"""The browser session survives an idle hour: the access COOKIE lives 30 days, the JWT inside 60 minutes.

Defect (prod): the access cookie `algo_auth_token` had max-age 3600, so after an hour away the browser
sent no credential at all, the Next.js middleware redirected to /login before any client code could call
/auth/refresh, although the refresh cookie was valid for 30 days. The access cookie now lives as long as
the refresh cookie; a stale JWT inside it is still rejected with a plain 401, which makes the client
refresh and retry.

The first half needs no database; the second half (``needs_pg``) drives login -> idle -> refresh -> logout
against the disposable ``a2_auth`` PostgreSQL.
"""
import os
from datetime import timedelta
from http.cookies import SimpleCookie
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import src.api.internal_router  # noqa: E402,F401  (import order: avoids a pre-existing router cycle)
import src.auth.router as auth_router_module  # noqa: E402
from src.auth.dependencies import (  # noqa: E402
    ACCESS_COOKIE_MAX_AGE,
    AUTH_COOKIE_NAME,
    REFRESH_COOKIE_MAX_AGE,
    REFRESH_COOKIE_NAME,
    get_current_user_payload,
    get_optional_user,
)
from src.auth.payments_router import router as payments_router  # noqa: E402
from src.auth.service import (  # noqa: E402
    ACCESS_TOKEN_EXPIRE_MINUTES,
    AuthService,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
)
from src.core.database import get_db  # noqa: E402

from tests.pg_world import needs_pg, pg_session, world  # noqa: E402,F401  (fixtures)

THIRTY_DAYS = 30 * 86400
API = "/api/v1/auth"


# ---- helpers ----------------------------------------------------------------------


def expired_access_token(user_id="user-1", role="teacher"):
    """A correctly signed access JWT that expired five minutes ago."""
    return create_access_token(user_id, role, expires_delta=timedelta(minutes=-5))


def client_for(db=None):
    app = FastAPI()
    app.include_router(auth_router_module.router, prefix="/api/v1")
    app.include_router(payments_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db if db is not None else MagicMock()
    return TestClient(app)


def cookie_header(access=None, refresh=None):
    parts = []
    if access is not None:
        parts.append(f"{AUTH_COOKIE_NAME}={access}")
    if refresh is not None:
        parts.append(f"{REFRESH_COOKIE_NAME}={refresh}")
    return {"cookie": "; ".join(parts)}


def set_cookies(response):
    """{name: morsel} of the Set-Cookie headers of a response."""
    jar = {}
    headers = response.headers
    # a starlette response (getlist) or an httpx response from the TestClient (get_list)
    raws = headers.getlist("set-cookie") if hasattr(headers, "getlist") else headers.get_list("set-cookie")
    for raw in raws:
        parsed = SimpleCookie()
        parsed.load(raw)
        jar.update(parsed)
    return jar


def assert_cleared(response):
    """Both cookies are deleted, with the very path the browser stored them under."""
    jar = set_cookies(response)
    assert set(jar) == {AUTH_COOKIE_NAME, REFRESH_COOKIE_NAME}
    for morsel in jar.values():
        assert morsel.value == "" and morsel["max-age"] == "0"
    assert jar[AUTH_COOKIE_NAME]["path"] == "/"
    assert jar[REFRESH_COOKIE_NAME]["path"] == "/api/v1/auth"


def assert_session_cookies(response, access_token=None):
    jar = set_cookies(response)
    assert set(jar) == {AUTH_COOKIE_NAME, REFRESH_COOKIE_NAME}
    access, refresh = jar[AUTH_COOKIE_NAME], jar[REFRESH_COOKIE_NAME]
    # the access COOKIE is as long-lived as the refresh cookie ...
    assert access["max-age"] == str(THIRTY_DAYS) == refresh["max-age"]
    # ... and every flag of the policy is untouched
    for morsel in (access, refresh):
        assert morsel["httponly"] is True
        assert morsel["samesite"].lower() == "lax"
    assert access["path"] == "/" and refresh["path"] == "/api/v1/auth"
    if access_token is not None:
        assert access.value == access_token
    return access, refresh


# ---- cookie policy (no database) --------------------------------------------------


def test_access_cookie_lives_as_long_as_the_refresh_cookie_but_the_jwt_stays_short():
    assert ACCESS_COOKIE_MAX_AGE == REFRESH_COOKIE_MAX_AGE == THIRTY_DAYS
    token = create_access_token("user-1", "teacher")
    payload = decode_token(token)
    assert payload["exp"] - payload["iat"] == ACCESS_TOKEN_EXPIRE_MINUTES * 60 == 3600
    assert ACCESS_COOKIE_MAX_AGE > ACCESS_TOKEN_EXPIRE_MINUTES * 60


def test_set_auth_cookies_flags_and_lifetimes(monkeypatch):
    monkeypatch.setattr(auth_router_module, "_IS_PROD", True)
    monkeypatch.setattr(auth_router_module, "_COOKIE_DOMAIN", ".algo.kz")
    response = auth_router_module._set_auth_cookies(JSONResponse({}), "access.jwt", "refresh.jwt")
    jar = set_cookies(response)
    assert jar[AUTH_COOKIE_NAME].value == "access.jwt" and jar[REFRESH_COOKIE_NAME].value == "refresh.jwt"
    for morsel in jar.values():
        assert morsel["max-age"] == str(THIRTY_DAYS)
        assert morsel["httponly"] is True and morsel["secure"] is True
        assert morsel["samesite"].lower() == "lax"
        assert morsel["domain"] == ".algo.kz"
    assert jar[AUTH_COOKIE_NAME]["path"] == "/" and jar[REFRESH_COOKIE_NAME]["path"] == "/api/v1/auth"


def test_cookies_are_not_secure_outside_production(monkeypatch):
    monkeypatch.setattr(auth_router_module, "_IS_PROD", False)
    monkeypatch.setattr(auth_router_module, "_COOKIE_DOMAIN", None)
    jar = set_cookies(auth_router_module._set_auth_cookies(JSONResponse({}), "a", "r"))
    for morsel in jar.values():
        assert not morsel["secure"] and not morsel["domain"] and morsel["httponly"] is True


def test_dev_login_uses_the_same_cookie_policy():
    """The dev quick-login used to carry its own copy of the cookie policy (and its 3600)."""
    from src.auth import dev_router

    assert dev_router._set_auth_cookies is auth_router_module._set_auth_cookies
    source = open(dev_router.__file__, encoding="utf-8").read()
    assert "set_cookie(" not in source and "max_age=3600" not in source


# ---- an expired access JWT in the (now long-lived) cookie -------------------------

PROTECTED = [
    ("GET", f"{API}/me"),
    ("GET", f"{API}/org"),
    ("GET", f"{API}/org/stats"),
    ("GET", f"{API}/org/payments/summary"),
    ("GET", f"{API}/org/settings/payments"),
]


@pytest.mark.parametrize("method,url", PROTECTED)
def test_expired_access_cookie_is_a_plain_401_on_protected_endpoints(method, url):
    r = client_for().request(method, url, headers=cookie_header(access=expired_access_token()))
    assert r.status_code == 401, (r.status_code, r.text)
    assert r.headers["www-authenticate"] == "Bearer"
    assert "истёкший" in r.json()["detail"]


@pytest.mark.parametrize("method,url", PROTECTED)
def test_expired_cookie_answers_exactly_like_a_missing_cookie(method, url):
    """Services behind /auth/me (exam, diagnostic) map 401 -> 401: stale equals absent."""
    client = client_for()
    stale = client.request(method, url, headers=cookie_header(access=expired_access_token()))
    absent = client.request(method, url)
    assert stale.status_code == absent.status_code == 401


def test_garbage_and_wrong_type_cookies_are_401_too():
    client = client_for()
    for value in ("garbage", create_refresh_token("user-1")):  # a refresh JWT is not an access JWT
        r = client.get(f"{API}/me", headers=cookie_header(access=value))
        assert r.status_code == 401


def test_expired_access_cookie_does_not_break_public_endpoints(monkeypatch):
    monkeypatch.setattr(AuthService, "get_invitation_info", lambda self, code: {"code": code, "role": "teacher"})
    client = client_for()
    headers = cookie_header(access=expired_access_token())
    r = client.get(f"{API}/invite/ABC123", headers=headers)
    assert r.status_code == 200 and r.json()["code"] == "ABC123"


def test_expired_access_cookie_does_not_break_login(monkeypatch):
    """The stale cookie must not shadow the login: the handler runs and answers on its merits."""
    def refuse(self, request):
        raise ValueError("Неверный пароль (из обработчика)")

    monkeypatch.setattr(AuthService, "login", refuse)
    r = client_for().post(
        f"{API}/login",
        json={"email": "a@b.co", "password": "x"},
        headers=cookie_header(access=expired_access_token()),
    )
    assert r.status_code == 401 and r.json()["detail"] == "Неверный пароль (из обработчика)"


def test_refresh_ignores_the_stale_access_cookie(monkeypatch):
    from src.api import rate_limit

    monkeypatch.setattr(rate_limit, "_check_redis", lambda *a, **k: True)
    seen = {}

    def fake_refresh(self, token):
        seen["refresh"] = token
        from src.auth.schemas import TokenPair

        return TokenPair(access_token="new.access", refresh_token="new.refresh", expires_in=3600)

    monkeypatch.setattr(AuthService, "refresh_tokens", fake_refresh)
    r = client_for().post(
        f"{API}/refresh",
        json={},
        headers=cookie_header(access=expired_access_token(), refresh="old.refresh"),
    )
    assert r.status_code == 200, r.text
    assert seen["refresh"] == "old.refresh"
    assert_session_cookies(r, access_token="new.access")


def test_valid_bearer_wins_over_a_stale_cookie_but_no_header_stays_401():
    """After a refresh the client retries with the new token: a stale cookie must not shadow it."""
    stale, fresh = expired_access_token("old-user"), create_access_token("fresh-user", "teacher")
    request = MagicMock()
    request.cookies.get = lambda name, default=None: stale if name == AUTH_COOKIE_NAME else default
    creds = MagicMock()
    creds.credentials = fresh

    assert get_current_user_payload(request, creds)["sub"] == "fresh-user"
    assert get_optional_user(request, creds)["sub"] == "fresh-user"
    assert get_optional_user(request, None) is None

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        get_current_user_payload(request, None)
    assert exc.value.status_code == 401


def test_a_valid_cookie_still_beats_the_header():
    cookie_token, header_token = create_access_token("cookie-user", "teacher"), create_access_token("header-user", "admin")
    request = MagicMock()
    request.cookies.get = lambda name, default=None: cookie_token if name == AUTH_COOKIE_NAME else default
    creds = MagicMock()
    creds.credentials = header_token
    assert get_current_user_payload(request, creds)["sub"] == "cookie-user"


# ---- logout always clears both cookies ---------------------------------------------


def revoked_users(monkeypatch):
    revoked = []
    monkeypatch.setattr(AuthService, "logout", lambda self, user_id: revoked.append(user_id))
    return revoked


def test_logout_with_a_valid_access_cookie_revokes_and_clears(monkeypatch):
    revoked = revoked_users(monkeypatch)
    r = client_for().post(f"{API}/logout", headers=cookie_header(access=create_access_token("u-1", "teacher")))
    assert r.status_code == 200
    assert revoked == ["u-1"]
    assert_cleared(r)


def test_logout_with_an_expired_access_cookie_still_revokes_via_the_refresh_cookie_and_clears(monkeypatch):
    revoked = revoked_users(monkeypatch)
    r = client_for().post(
        f"{API}/logout",
        headers=cookie_header(access=expired_access_token("u-2"), refresh=create_refresh_token("u-2")),
    )
    assert r.status_code == 200, r.text
    assert revoked == ["u-2"]
    assert_cleared(r)


def test_logout_with_nothing_valid_is_200_and_still_clears_both_cookies(monkeypatch):
    revoked = revoked_users(monkeypatch)
    client = client_for()
    for headers in (
        {},
        cookie_header(access=expired_access_token("u-3")),
        cookie_header(access="garbage", refresh="garbage"),
        # an access JWT in the refresh slot is not a refresh token
        cookie_header(refresh=create_access_token("u-3", "teacher")),
    ):
        r = client.post(f"{API}/logout", headers=headers)
        assert r.status_code == 200, (headers, r.text)
        assert_cleared(r)
    assert revoked == []


def test_logout_accepts_a_bearer_header_too(monkeypatch):
    revoked = revoked_users(monkeypatch)
    r = client_for().post(
        f"{API}/logout",
        headers={"authorization": f"Bearer {create_access_token('u-4', 'owner')}", **cookie_header(access=expired_access_token("u-4"))},
    )
    assert r.status_code == 200 and revoked == ["u-4"]
    assert_cleared(r)


# ---- end to end against PostgreSQL --------------------------------------------------

PASSWORD = "Sess1on-Cookie!"


@pytest.fixture
def session_world(world, monkeypatch):
    """The shared world with a real, loggable owner (password + default organisation)."""
    from src.api import rate_limit

    monkeypatch.setattr(rate_limit, "_check_redis", lambda *a, **k: True)
    owner = world.owner
    owner.password_hash = hash_password(PASSWORD)
    owner.default_org_id = world.org.id
    owner.is_active = True
    world.db.commit()
    world.client = client_for(world.db)
    return world


def login(w):
    r = w.client.post(f"{API}/login", json={"email": w.owner.email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r


def stored_refresh_hash(w):
    w.db.expire_all()
    return w.owner.refresh_token_hash


@needs_pg
def test_login_sets_a_30_day_access_cookie_holding_a_60_minute_jwt(session_world):
    r = login(session_world)
    access, refresh = assert_session_cookies(r)
    payload = decode_token(access.value)
    assert payload["type"] == "access" and payload["exp"] - payload["iat"] == 3600
    assert r.json()["tokens"]["expires_in"] == 3600
    assert decode_token(refresh.value)["type"] == "refresh"


@needs_pg
def test_idle_hour_flow_expired_cookie_401_then_refresh_then_retry_works(session_world):
    w = session_world
    refresh_cookie = set_cookies(login(w))[REFRESH_COOKIE_NAME].value
    stale = expired_access_token(str(w.owner.id), "owner")

    # an hour later the browser still sends both cookies; the server answers a plain 401 ...
    r = w.client.get(f"{API}/me", headers=cookie_header(access=stale, refresh=refresh_cookie))
    assert r.status_code == 401

    # ... the client refreshes (the stale access cookie travels along and does not matter) ...
    r = w.client.post(f"{API}/refresh", json={}, headers=cookie_header(access=stale, refresh=refresh_cookie))
    assert r.status_code == 200, r.text
    access, refresh = assert_session_cookies(r)
    assert decode_token(access.value)["exp"] - decode_token(access.value)["iat"] == 3600
    assert refresh.value != refresh_cookie  # rotated

    # ... and the retry with the new cookies succeeds
    r = w.client.get(f"{API}/me", headers=cookie_header(access=access.value, refresh=refresh.value))
    assert r.status_code == 200 and r.json()["id"] == str(w.owner.id)

    # the rotated-away refresh token is dead
    r = w.client.post(f"{API}/refresh", json={}, headers=cookie_header(refresh=refresh_cookie))
    assert r.status_code == 401


@needs_pg
def test_retry_with_a_bearer_token_works_even_when_the_stale_cookie_is_still_in_the_jar(session_world):
    w = session_world
    refresh_cookie = set_cookies(login(w))[REFRESH_COOKIE_NAME].value
    stale = expired_access_token(str(w.owner.id), "owner")
    r = w.client.post(f"{API}/refresh", json={}, headers=cookie_header(access=stale, refresh=refresh_cookie))
    fresh = r.json()["access_token"]
    r = w.client.get(f"{API}/me", headers={"authorization": f"Bearer {fresh}", **cookie_header(access=stale)})
    assert r.status_code == 200


@needs_pg
def test_expired_cookie_with_a_revoked_refresh_token_is_401_everywhere(session_world):
    w = session_world
    refresh_cookie = set_cookies(login(w))[REFRESH_COOKIE_NAME].value
    w.owner.refresh_token_hash = None  # password change / logout elsewhere
    w.db.commit()
    stale = expired_access_token(str(w.owner.id), "owner")
    assert w.client.get(f"{API}/me", headers=cookie_header(access=stale, refresh=refresh_cookie)).status_code == 401
    assert w.client.post(f"{API}/refresh", json={}, headers=cookie_header(refresh=refresh_cookie)).status_code == 401


@needs_pg
def test_login_works_while_a_stale_access_cookie_is_present(session_world):
    w = session_world
    stale = expired_access_token(str(w.owner.id), "owner")
    r = w.client.post(
        f"{API}/login", json={"email": w.owner.email, "password": PASSWORD}, headers=cookie_header(access=stale)
    )
    assert r.status_code == 200
    assert_session_cookies(r)


@needs_pg
def test_logout_with_an_expired_access_cookie_revokes_the_session_and_clears_both_cookies(session_world):
    w = session_world
    refresh_cookie = set_cookies(login(w))[REFRESH_COOKIE_NAME].value
    assert stored_refresh_hash(w) is not None
    stale = expired_access_token(str(w.owner.id), "owner")

    r = w.client.post(f"{API}/logout", headers=cookie_header(access=stale, refresh=refresh_cookie))
    assert r.status_code == 200
    assert_cleared(r)
    assert stored_refresh_hash(w) is None
    assert w.client.post(f"{API}/refresh", json={}, headers=cookie_header(refresh=refresh_cookie)).status_code == 401


@needs_pg
def test_logout_with_a_valid_session_revokes_it_and_clears_both_cookies(session_world):
    w = session_world
    jar = set_cookies(login(w))
    r = w.client.post(
        f"{API}/logout", headers=cookie_header(access=jar[AUTH_COOKIE_NAME].value, refresh=jar[REFRESH_COOKIE_NAME].value)
    )
    assert r.status_code == 200
    assert_cleared(r)
    assert stored_refresh_hash(session_world) is None


@needs_pg
def test_logout_never_touches_another_users_session(session_world):
    w = session_world
    login(w)
    other = w.admin
    other.refresh_token_hash = "someone-elses"
    w.db.commit()
    jar = set_cookies(login(w))
    w.client.post(f"{API}/logout", headers=cookie_header(access=jar[AUTH_COOKIE_NAME].value))
    w.db.expire_all()
    assert other.refresh_token_hash == "someone-elses"
