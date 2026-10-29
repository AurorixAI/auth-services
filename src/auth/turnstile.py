"""Cloudflare Turnstile for the public demo request (A5): the token of the form is checked on the server.

* ``TURNSTILE_SECRET_KEY`` not set -> the check is skipped (local runs, tests, a stack without the key).
* Set: no token or a token Cloudflare refuses -> False.  Cloudflare unreachable / answers with an error -> True and a warning in the log:
  a real application must not be lost because of someone else's outage; the limits behind this check still hold.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TIMEOUT_SECONDS = 5.0
MAX_TOKEN_LENGTH = 2048  # the documented maximum of a Turnstile response


def secret() -> str:
    return (os.getenv("TURNSTILE_SECRET_KEY") or "").strip()


def _siteverify(secret_key: str, token: str) -> Any:
    """The answer of Cloudflare as a dict; raises on a network error or a non-2xx status (the only function that touches the network)."""
    response = httpx.post(SITEVERIFY_URL, data={"secret": secret_key, "response": token}, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def verify(token: Any) -> bool:
    key = secret()
    if not key:
        return True
    if not isinstance(token, str) or not token.strip() or len(token) > MAX_TOKEN_LENGTH:
        return False
    try:
        answer = _siteverify(key, token.strip())
    except Exception:  # noqa: BLE001 - an outage of the check must not drop an application
        logger.warning("turnstile: Cloudflare did not answer - the request is accepted without the check", exc_info=True)
        return True
    return isinstance(answer, dict) and answer.get("success") is True
