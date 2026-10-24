"""Start-up guards for the secrets and public URLs the service signs and links with (SV-01, SV-27).

The whole project names its environment ``APP_ENV`` (compose files, Dockerfile, cookies, main_v2). On the servers it
is ``staging`` for both the stage and the prod stack, ``production`` in docker-compose.prod.yml, ``testing`` in CI.

* ``APP_ENV`` in {production, staging} (and the aliases prod/stage/preview) is *strict*: an empty, default, placeholder
  or shorter than 32 characters signing key stops the start with a clear message.
* Any other value (development, testing, unset) only warns, so local runs and the test suites keep starting.
"""
from __future__ import annotations

import logging
import os
from typing import Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

STRICT_ENVS = frozenset({"production", "prod", "staging", "stage", "preview"})
MIN_SECRET_LENGTH = 32
DEFAULT_SECRET = "algo-v1-dev-secret-key-change-in-prod"
_PLACEHOLDER_PREFIXES = ("change-me", "changeme", "your-secret", "replace-me", "replace-with")  # replace-with…: infra compose/.env.example
MIN_DISTINCT_CHARS = 8
DEFAULT_DIAGNOSTIC_URL = "http://localhost:3000/d"


def app_env(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    return (env.get("APP_ENV") or "").strip().lower()


def is_strict_env(environ: Optional[Mapping[str, str]] = None) -> bool:
    return app_env(environ) in STRICT_ENVS


def secret_problem(value: str) -> Optional[str]:
    """Why *value* cannot sign tokens in a deployed environment (None when it can)."""
    if not value or not value.strip():
        return "пустой"
    if value == DEFAULT_SECRET or value.lower().startswith(_PLACEHOLDER_PREFIXES):
        return "значение по умолчанию / пример из документации"
    if len(value) < MIN_SECRET_LENGTH:
        return f"короче {MIN_SECRET_LENGTH} символов"
    if len(set(value)) < MIN_DISTINCT_CHARS:
        return f"слишком простой (меньше {MIN_DISTINCT_CHARS} разных символов)"
    return None


def load_signing_secrets(environ: Optional[Mapping[str, str]] = None) -> Tuple[str, str]:
    """``(SECRET_KEY, JWT_SECRET)`` from the environment; raises RuntimeError in production/staging when one is unfit.

    ``JWT_SECRET_KEY`` falls back to ``SECRET_KEY`` when it is unset OR EMPTY (compose passes ``${JWT_SECRET_KEY}`` as
    an empty string when the host variable is missing - an empty HS256 key would be forgeable).
    """
    env = os.environ if environ is None else environ
    secret_key = env.get("SECRET_KEY") or ""
    jwt_secret = env.get("JWT_SECRET_KEY") or secret_key
    strict = is_strict_env(env)
    for name, value in (("SECRET_KEY", secret_key), ("JWT_SECRET_KEY", jwt_secret)):
        problem = secret_problem(value)
        if problem is None:
            continue
        if strict:
            raise RuntimeError(
                f"CRITICAL: {name} непригоден для APP_ENV={app_env(env)}: {problem}. "
                f"Задайте случайную строку не короче {MIN_SECRET_LENGTH} символов (openssl rand -hex 32)."
            )
        logger.warning("%s: %s - допустимо только для разработки и тестов (APP_ENV=%r).", name, problem, app_env(env))
    return secret_key or DEFAULT_SECRET, jwt_secret or DEFAULT_SECRET


def diagnostic_base_url(environ: Optional[Mapping[str, str]] = None) -> str:
    """Base of the invite links handed to students (``DIAGNOSTIC_BASE_URL``).

    A localhost link is useless to a student. In production/staging it is logged as an ERROR; the start is refused only
    when ``REQUIRE_DIAGNOSTIC_BASE_URL=true`` (the infra compose files do not pass this variable yet - a hard stop
    would take the stack down on the next deploy; switch it on once they do).
    """
    env = os.environ if environ is None else environ
    configured = (env.get("DIAGNOSTIC_BASE_URL") or "").strip()
    local = any(host in configured for host in ("localhost", "127.0.0.1", "[::1]"))
    if configured and not (local and is_strict_env(env)):
        return configured
    if is_strict_env(env):
        message = (
            f"DIAGNOSTIC_BASE_URL не задан (или указывает на localhost) при APP_ENV={app_env(env)}: ссылки-приглашения "
            f"ученикам будут вести на {configured or DEFAULT_DIAGNOSTIC_URL}."
        )
        if (env.get("REQUIRE_DIAGNOSTIC_BASE_URL") or "").strip().lower() in ("1", "true", "yes"):
            raise RuntimeError("CRITICAL: " + message)
        logger.error(message)
    return configured or DEFAULT_DIAGNOSTIC_URL
