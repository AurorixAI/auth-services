"""AD-2: every mutating route of the application refuses a request that another site started.

The guard test walks EVERY registered route of the real application (the routers included lazily, the hidden ones too), so a route written
tomorrow is covered, or this file fails: each POST/PUT/PATCH/DELETE must answer 403 ``bad_origin`` to a foreign Origin, to ``Sec-Fetch-Site:
cross-site`` and to a foreign Referer without Origin - except the routes of the explicit exemption list (``EXPECTED_EXEMPT``, the reasons are in
``src/core/origin_guard.py``). No database is needed: the guard answers before anything is read.
"""
import os

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

import src.api.internal_router  # noqa: E402,F401  (import order: avoids a pre-existing router cycle)
from src.api import main_v2  # noqa: E402
from src.core import origin_guard  # noqa: E402
from src.core.config import get_settings  # noqa: E402

EVIL = "https://evil.example"
CONSOLE_HOST = "console.algolab.me"
CABINET_HOST = "testserver"

# The ONLY places where a foreign Origin is not judged. Growing this set is a decision: write the reason in origin_guard.EXEMPT first.
EXPECTED_EXEMPT = {"/api/v1/internal", "/api/v1/public"}


def mutating_routes():
    """{(METHOD, full path)} of every APIRoute of the application, found by walking the included routers (not by the OpenAPI document: hidden routes count)."""
    found = set()

    def walk(routes, prefix):
        for route in routes:
            if hasattr(route, "original_router"):  # an included router (FastAPI >= 0.13x includes lazily)
                walk(route.original_router.routes, prefix + route.include_context.prefix)
            elif isinstance(route, APIRoute):
                for method in route.methods & origin_guard.MUTATING_METHODS:
                    found.add((method, prefix + route.path))

    walk(main_v2.app.routes, "")
    return found


def concrete(path):
    import re

    return re.sub(r"\{[^}]+\}", "x", path)


def client_for(path):
    host = CONSOLE_HOST if origin_guard.is_platform(path) else CABINET_HOST
    return TestClient(main_v2.app, base_url=f"http://{host}", raise_server_exceptions=False)


def own_origin():
    return get_settings().cors_origins[0]


ROUTES = sorted(mutating_routes())


def test_the_walk_finds_every_route_the_openapi_document_lists_and_the_hidden_ones():
    listed = {
        (method.upper(), path)
        for path, ops in main_v2.app.openapi()["paths"].items()
        for method in ops
        if method.upper() in origin_guard.MUTATING_METHODS
    }
    assert listed <= set(ROUTES)
    assert len(ROUTES) >= 120, "the walk found too little: a change of FastAPI's routing internals must not silently empty this test"
    assert ("POST", "/api/v1/auth/logout") in ROUTES and ("POST", "/api/v1/platform/auth/login") in ROUTES
    assert ("PUT", "/api/v1/auth/org/memberships/{membership_id}/fee") in ROUTES  # include_in_schema=False


def test_the_exemption_list_is_exactly_the_expected_one_and_every_entry_matches_a_real_route():
    assert set(origin_guard.EXEMPT) == EXPECTED_EXEMPT
    for prefix in origin_guard.EXEMPT:
        assert any(origin_guard.is_exempt(path) for _, path in ROUTES), prefix
        assert origin_guard.EXEMPT[prefix].strip()  # the reason is written down


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": EVIL},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "http://localhost:3000.evil.example"},
        {"Referer": EVIL + "/page"},
        {"Origin": "http://localhost:3000", "Sec-Fetch-Site": "cross-site"},
    ],
    ids=["foreign-origin", "null-origin", "cross-site", "suffix-attack", "foreign-referer", "own-origin-but-cross-site"],
)
def test_every_mutating_route_refuses_a_request_that_another_site_started_except_the_exempt_ones(headers):
    allowed, refused_wrongly, leaked = 0, [], []
    for method, path in ROUTES:
        response = client_for(path).request(method, concrete(path), headers=headers)
        is_bad_origin = response.status_code == 403 and response.headers.get("content-type", "").startswith("application/json") and (
            response.json().get("code") == origin_guard.BAD_ORIGIN_CODE
        )
        if origin_guard.is_exempt(path):
            allowed += 1
            if is_bad_origin:
                refused_wrongly.append((method, path))
        elif not is_bad_origin:
            leaked.append((method, path, response.status_code))
    assert not leaked, f"routes that let another site through: {leaked}"
    assert not refused_wrongly, refused_wrongly
    assert allowed >= 3  # the internal and public routes really were tried


def test_the_answer_is_403_with_a_readable_text_and_a_request_id():
    response = TestClient(main_v2.app).post("/api/v1/auth/logout", headers={"Origin": EVIL})
    assert response.status_code == 403
    assert response.json() == {"detail": origin_guard.BAD_ORIGIN_TEXT, "code": "bad_origin"}
    assert response.headers.get("x-request-id")
    assert "set-cookie" not in response.headers and "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    "headers",
    [
        {},  # curl / server to server / the Node side of the front: neither Origin nor Referer nor Sec-Fetch-Site
        {"Origin": "OWN"},
        {"Origin": "OWN/"},
        {"Origin": "FRONT"},
        {"Origin": "OWN", "Sec-Fetch-Site": "same-origin"},
        {"Origin": "OWN", "Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "same-origin"},
        {"Referer": "OWN/teacher/groups?x=1"},
        {"Origin": "OWN", "Referer": EVIL},  # Origin decides when it is there
    ],
)
def test_our_own_pages_and_clients_without_headers_pass_the_guard(monkeypatch, headers):
    monkeypatch.setenv("FRONTEND_URL", "https://app.example.test")
    headers = {k: v.replace("OWN", own_origin()).replace("FRONT", "https://app.example.test") for k, v in headers.items()}
    response = TestClient(main_v2.app).post("/api/v1/auth/logout", headers=headers)
    assert response.status_code == 200, (headers, response.text)  # no credential: nothing to clear, but the guard let it in


def test_the_frontend_url_is_allowed_even_when_the_cors_list_does_not_name_it(monkeypatch):
    """LESSONS 68: the address the product is served from must not depend on a CORS list that is one step behind."""
    monkeypatch.setattr(get_settings(), "cors_origins", ["https://other.example.test"])
    monkeypatch.setenv("FRONTEND_URL", "https://app.example.test/")
    client = TestClient(main_v2.app)
    assert client.post("/api/v1/auth/logout", headers={"Origin": "https://app.example.test"}).status_code == 200
    assert client.post("/api/v1/auth/logout", headers={"Origin": "https://other.example.test"}).status_code == 200
    assert client.post("/api/v1/auth/logout", headers={"Origin": "https://app.example.test.evil.example"}).status_code == 403
    assert client.post("/api/v1/auth/logout", headers={"Origin": "http://app.example.test"}).status_code == 403  # another scheme is another origin


def test_a_wildcard_cors_list_lets_every_origin_in_but_never_a_cross_site_header(monkeypatch):
    monkeypatch.setattr(get_settings(), "cors_origins", ["*"])
    client = TestClient(main_v2.app)
    assert client.post("/api/v1/auth/logout", headers={"Origin": EVIL}).status_code == 200
    assert client.post("/api/v1/auth/logout", headers={"Origin": EVIL, "Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_reads_and_preflights_are_not_judged_by_the_middleware():
    client = TestClient(main_v2.app)
    assert client.get("/health/live", headers={"Origin": EVIL}).status_code == 200
    preflight = client.options(
        "/api/v1/auth/logout", headers={"Origin": own_origin(), "Access-Control-Request-Method": "POST"}
    )
    assert preflight.status_code == 200 and preflight.headers["access-control-allow-origin"] == own_origin()


# ===================================================================================================== the console section


def console(**headers):
    return TestClient(main_v2.app, base_url=f"http://{CONSOLE_HOST}", headers=headers)


@pytest.mark.parametrize(
    "headers",
    [{"Origin": EVIL}, {"Sec-Fetch-Site": "cross-site"}, {"Referer": EVIL + "/x"}, {"Origin": "https://console.algolab.me.evil.example"}],
)
def test_the_console_refuses_a_foreign_request_on_its_own_host(headers):
    response = console().post("/api/v1/platform/auth/logout", headers=headers)
    assert response.status_code == 403 and response.json()["code"] == "bad_origin", response.text


def test_the_console_does_not_reveal_itself_on_another_host_even_to_a_foreign_origin():
    response = TestClient(main_v2.app, base_url="http://app.algolab.me").post("/api/v1/platform/auth/logout", headers={"Origin": EVIL})
    assert response.status_code == 404 and response.json()["code"] == "not_found"


@pytest.mark.parametrize(
    "headers",
    [{}, {"Origin": "http://console.algolab.me"}, {"Origin": "https://console.stage.algolab.me", "Sec-Fetch-Site": "same-origin"}, {"Referer": "https://console.algolab.me/requests"}],
)
def test_the_console_accepts_its_own_pages_and_headerless_clients(headers):
    # logout answers 200 for a request without a session; the point is that the guard let it in
    response = console().post("/api/v1/platform/auth/logout", headers=headers)
    assert response.status_code == 200, (headers, response.text)


def test_a_console_read_with_a_foreign_origin_is_refused_as_before():
    assert console().get("/api/v1/platform/auth/me", headers={"Origin": EVIL}).status_code == 403


# ===================================================================================================== the pure decision


@pytest.mark.parametrize(
    "value, expected",
    [
        ("https://App.AlgoLab.me:443/", "https://app.algolab.me"),
        ("http://localhost:3000", "http://localhost:3000"),
        ("http://localhost:80", "http://localhost"),
        ("https://[::1]:8443", "https://[::1]:8443"),
        ("null", None),
        ("", None),
        (None, None),
        ("https://user@app.algolab.me", None),
        ("https://app.algolab.me/path", None),
        ("https://app.algolab.me?x=1", None),
        ("ftp://app.algolab.me", None),
        ("app.algolab.me", None),
        ("https://app.algolab.me:notaport", None),
    ],
)
def test_normalize_origin(value, expected):
    assert origin_guard.normalize_origin(value) == expected


@pytest.mark.parametrize(
    "headers, foreign",
    [
        ({}, False),
        ({"origin": "https://ok.test"}, False),
        ({"origin": "https://evil.test"}, True),
        ({"origin": "https://ok.test", "sec-fetch-site": "cross-site"}, True),
        ({"origin": "https://ok.test", "sec-fetch-site": "same-site"}, False),
        ({"referer": "https://ok.test/a/b?c=d"}, False),
        ({"referer": "https://evil.test/a"}, True),
        ({"referer": "not a url"}, True),
        ({"origin": "https://ok.test", "referer": "https://evil.test/a"}, False),
        ({"origin": ""}, True),  # an empty Origin is not an origin
    ],
)
def test_request_is_foreign(headers, foreign):
    assert origin_guard.request_is_foreign(headers, lambda o: origin_guard.normalize_origin(o) == "https://ok.test") is foreign
