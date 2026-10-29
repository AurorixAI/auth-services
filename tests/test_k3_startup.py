"""K3 (A9): the start-up checks of the public addresses.  The product (``FRONTEND_URL``) and the landing (``SITE_URL``) are two hosts; Turnstile is configured;
``REQUIRE_DIAGNOSTIC_BASE_URL`` (already in the guard, covered by test_a2_startup_guard.py) stays the hard stop for the invite links."""
import asyncio

import pytest

from src.core import startup_guard as guard

STAGING = {"APP_ENV": "staging", "FRONTEND_URL": "https://stage.algolab.me", "SITE_URL": "https://stage.algolab.me"}


def test_the_same_host_of_the_product_and_the_landing_is_an_error_in_the_log_not_a_stop_by_default(caplog):
    with caplog.at_level("ERROR"):
        guard.check_public_urls({**STAGING, "TURNSTILE_SECRET_KEY": "k"})
    assert "FRONTEND_URL и SITE_URL указывают на один хост (stage.algolab.me)" in caplog.text


def test_the_flag_makes_the_same_host_a_stop():
    with pytest.raises(RuntimeError, match="CRITICAL: FRONTEND_URL и SITE_URL"):
        guard.check_public_urls({**STAGING, "REQUIRE_DISTINCT_SITE_HOSTS": "true"})


def test_two_different_hosts_start_with_the_flag(caplog):
    env = {"APP_ENV": "production", "FRONTEND_URL": "https://app.algolab.me", "SITE_URL": "https://algolab.me", "REQUIRE_DISTINCT_SITE_HOSTS": "true",
           "TURNSTILE_SECRET_KEY": "k"}
    with caplog.at_level("WARNING"):
        guard.check_public_urls(env)
    assert caplog.text == ""


@pytest.mark.parametrize(
    "frontend, site, same",
    [
        ("https://algolab.me", "https://algolab.me", True), ("https://ALGOLAB.me/", "https://algolab.me", True), ("https://algolab.me:443", "https://algolab.me/x", True),
        ("https://app.algolab.me", "https://algolab.me", False), ("https://app.stage.algolab.me", "https://stage.algolab.me", False),
    ],
)
def test_hosts_are_compared_without_scheme_port_path_and_case(frontend, site, same):
    env = {"APP_ENV": "staging", "FRONTEND_URL": frontend, "SITE_URL": site, "REQUIRE_DISTINCT_SITE_HOSTS": "true"}
    if same:
        with pytest.raises(RuntimeError):
            guard.check_public_urls(env)
    else:
        guard.check_public_urls(env)


def test_the_defaults_count_when_the_variables_are_missing():
    guard.check_public_urls({"APP_ENV": "production", "REQUIRE_DISTINCT_SITE_HOSTS": "true"})              # localhost:3000 vs algolab.me: different


def test_development_and_tests_are_not_checked_at_all(caplog):
    with caplog.at_level("WARNING"):
        guard.check_public_urls({"APP_ENV": "testing", "FRONTEND_URL": "https://a.me", "SITE_URL": "https://a.me", "REQUIRE_DISTINCT_SITE_HOSTS": "true"})
        guard.check_public_urls({})
    assert caplog.text == ""


def test_a_missing_turnstile_key_is_a_warning_on_a_deployed_stack(caplog):
    with caplog.at_level("WARNING"):
        guard.check_public_urls({"APP_ENV": "staging", "FRONTEND_URL": "https://app.stage.algolab.me", "SITE_URL": "https://stage.algolab.me"})
    assert "TURNSTILE_SECRET_KEY не задан" in caplog.text


def run_lifespan(monkeypatch, **env):
    """The real start-up of the application, without a database: the time zone check and the migrations are replaced."""
    from alembic import command

    from src.api import main_v2
    from src.core import db_timezone

    for key in ("APP_ENV", "FRONTEND_URL", "SITE_URL", "REQUIRE_DISTINCT_SITE_HOSTS", "TURNSTILE_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(db_timezone, "verify_database_timezone", lambda engine: None)
    monkeypatch.setattr(command, "upgrade", lambda cfg, rev: None)

    async def go():
        async with main_v2.lifespan(main_v2.app):
            pass

    asyncio.run(go())


def test_the_application_refuses_to_start_on_one_host_when_the_flag_is_on(monkeypatch):
    with pytest.raises(RuntimeError, match="CRITICAL: FRONTEND_URL и SITE_URL"):
        run_lifespan(monkeypatch, **STAGING, REQUIRE_DISTINCT_SITE_HOSTS="true")


def test_the_application_starts_on_one_host_without_the_flag_and_on_two_hosts_with_it(monkeypatch):
    run_lifespan(monkeypatch, **STAGING)
    run_lifespan(monkeypatch, APP_ENV="production", FRONTEND_URL="https://app.algolab.me", SITE_URL="https://algolab.me", REQUIRE_DISTINCT_SITE_HOSTS="true")
