"""«Запомнить меня»: a login without it sets BROWSER-SESSION cookies, and so does every refresh after it.

``remember`` is optional on ``/auth/login`` and true by default: an old client that never sends it keeps the
30-day cookies it always had. The mode of a session is carried by the refresh JWT (claim ``rem: false``),
so ``/auth/refresh`` re-sets the cookies in the same mode without any help from the client.
"""
import os
from http.cookies import SimpleCookie

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import src.api.internal_router  # noqa: E402,F401  (import order: avoids a pre-existing router cycle)
import src.auth.cookies as cookies_module  # noqa: E402
from src.api import rate_limit as rl  # noqa: E402
from src.auth import router as auth_router_module  # noqa: E402
from src.auth.cookies import (  # noqa: E402
    AUTH_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
    remember_from_refresh_token,
    set_auth_cookies,
)
from src.auth.service import create_refresh_token, decode_token, hash_password  # noqa: E402
from src.core.database import get_db  # noqa: E402
from src.core.exceptions import EXCEPTION_STATUS_MAP, AlgoException  # noqa: E402

from tests.pg_world import needs_pg, pg_session, world  # noqa: E402,F401  (fixtures)

API = "/api/v1/auth"
THIRTY_DAYS = str(30 * 86400)
PASSWORD = "Remember-me-1"


@pytest.fixture(autouse=True)
def memory_limiter(monkeypatch):
    rl._memory_store.clear()
    monkeypatch.setattr(rl, "_check_redis", rl._check_memory)
    yield
    rl._memory_store.clear()


def morsels(response):
    jar = SimpleCookie()
    headers = response.headers
    raws = headers.getlist("set-cookie") if hasattr(headers, "getlist") else headers.get_list("set-cookie")
    for raw in raws:
        jar.load(raw)
    return jar


def assert_long(response):
    jar = morsels(response)
    assert set(jar) == {AUTH_COOKIE_NAME, REFRESH_COOKIE_NAME}
    for morsel in jar.values():
        assert morsel["max-age"] == THIRTY_DAYS and morsel["httponly"] is True and morsel["samesite"].lower() == "lax"
    return jar


def assert_browser_session(response):
    """Both cookies exist, carry no lifetime at all, and keep every other flag of the policy."""
    jar = morsels(response)
    assert set(jar) == {AUTH_COOKIE_NAME, REFRESH_COOKIE_NAME}
    for morsel in jar.values():
        assert morsel["max-age"] == "" and morsel["expires"] == "", dict(morsel)
        assert morsel["httponly"] is True and morsel["samesite"].lower() == "lax"
        assert morsel.value
    assert jar[AUTH_COOKIE_NAME]["path"] == "/" and jar[REFRESH_COOKIE_NAME]["path"] == "/api/v1/auth"
    return jar


# ---- no database ------------------------------------------------------------------


def test_the_refresh_jwt_marks_only_a_login_without_remember_me():
    default = decode_token(create_refresh_token("user-1"))
    kept = decode_token(create_refresh_token("user-1", remember=True))
    session = decode_token(create_refresh_token("user-1", remember=False))
    assert "rem" not in default and "rem" not in kept          # tokens of old clients and releases stay as they were
    assert session["rem"] is False
    assert remember_from_refresh_token(create_refresh_token("user-1")) is True
    assert remember_from_refresh_token(create_refresh_token("user-1", remember=False)) is False
    assert remember_from_refresh_token("not-a-jwt") is True    # unreadable: the long-lived default, never a lock-out


def test_set_auth_cookies_remember_false_drops_the_lifetime_and_nothing_else(monkeypatch):
    monkeypatch.setattr(cookies_module, "_IS_PROD", True)
    monkeypatch.setattr(cookies_module, "_COOKIE_DOMAIN", ".algo.kz")
    long = morsels(set_auth_cookies(JSONResponse({}), "a.jwt", "r.jwt"))
    short = morsels(set_auth_cookies(JSONResponse({}), "a.jwt", "r.jwt", remember=False))
    for name in (AUTH_COOKIE_NAME, REFRESH_COOKIE_NAME):
        assert long[name]["max-age"] == THIRTY_DAYS
        assert short[name]["max-age"] == "" and short[name]["expires"] == ""
        for flag in ("httponly", "secure", "samesite", "path", "domain"):
            assert short[name][flag] == long[name][flag], flag
        assert short[name].value == long[name].value
    assert short[AUTH_COOKIE_NAME]["secure"] is True and short[AUTH_COOKIE_NAME]["domain"] == ".algo.kz"


def test_the_mode_is_read_from_the_refresh_token_when_the_caller_does_not_say(monkeypatch):
    session_refresh = create_refresh_token("user-1", remember=False)
    assert_browser_session(set_auth_cookies(JSONResponse({}), "a.jwt", session_refresh))
    assert_long(set_auth_cookies(JSONResponse({}), "a.jwt", create_refresh_token("user-1")))


# ---- login and refresh against PostgreSQL -----------------------------------------


def client_for(db):
    app = FastAPI()

    @app.exception_handler(AlgoException)
    async def algo_exception_handler(request, exc):
        return JSONResponse(
            status_code=EXCEPTION_STATUS_MAP.get(type(exc), 400),
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(auth_router_module.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


@pytest.fixture
def account(world):
    user = world.teacher
    user.password_hash = hash_password(PASSWORD)
    user.default_org_id = world.org.id
    world.db.commit()
    return world, user


def login(client, user, **extra):
    return client.post(f"{API}/login", json={"email": user.email, "password": PASSWORD, **extra})


@needs_pg
@pytest.mark.parametrize("extra", [{}, {"remember": True}, {"remember": None}], ids=["old client", "true", "null"])
def test_login_without_the_option_or_with_it_on_sets_the_thirty_day_cookies(account, extra):
    w, user = account
    response = login(client_for(w.db), user, **extra)
    assert response.status_code == 200 and response.json()["tokens"]["refresh_token"]
    jar = assert_long(response)
    assert "rem" not in decode_token(jar[REFRESH_COOKIE_NAME].value)


@needs_pg
def test_login_with_remember_off_sets_browser_session_cookies(account):
    w, user = account
    response = login(client_for(w.db), user, remember=False)
    assert response.status_code == 200
    jar = assert_browser_session(response)
    body = response.json()
    assert body["tokens"]["access_token"] == jar[AUTH_COOKIE_NAME].value                # the body is unchanged
    assert body["tokens"]["refresh_token"] == jar[REFRESH_COOKIE_NAME].value
    assert decode_token(jar[REFRESH_COOKIE_NAME].value)["rem"] is False
    assert decode_token(jar[AUTH_COOKIE_NAME].value)["exp"] - decode_token(jar[AUTH_COOKIE_NAME].value)["iat"] == 3600


@needs_pg
def test_a_session_login_stays_a_session_login_through_every_refresh(account):
    w, user = account
    client = client_for(w.db)
    refresh = login(client, user, remember=False).json()["tokens"]["refresh_token"]
    for _ in range(3):
        response = client.post(f"{API}/refresh", json={"refresh_token": refresh})
        assert response.status_code == 200
        jar = assert_browser_session(response)
        refresh = jar[REFRESH_COOKIE_NAME].value
        assert decode_token(refresh)["rem"] is False and response.json()["refresh_token"] == refresh


@needs_pg
def test_a_remembered_login_stays_remembered_through_a_refresh_from_the_cookie(account):
    w, user = account
    client = client_for(w.db)
    refresh = login(client, user).json()["tokens"]["refresh_token"]
    # the refresh token travels in the httpOnly cookie, as in the browser
    response = client.post(f"{API}/refresh", json={}, cookies={REFRESH_COOKIE_NAME: refresh})
    assert response.status_code == 200
    jar = assert_long(response)
    assert "rem" not in decode_token(jar[REFRESH_COOKIE_NAME].value)


@needs_pg
def test_a_session_login_refreshed_from_the_cookie_stays_a_session_login(account):
    w, user = account
    client = client_for(w.db)
    refresh = login(client, user, remember=False).json()["tokens"]["refresh_token"]
    response = client.post(f"{API}/refresh", json={}, cookies={REFRESH_COOKIE_NAME: refresh})
    assert response.status_code == 200
    assert_browser_session(response)
