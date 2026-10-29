"""Cloudflare Turnstile for the public demo request (A5): the token of the form is checked on the server.

* ``TURNSTILE_SECRET_KEY`` not set -> the check is skipped (local runs, tests, a stack without the key).
* ``TURNSTILE_HOSTNAMES`` (comma separated, e.g. ``algolab.me,www.algolab.me``): the ``hostname`` of Cloudflare's answer must be one of them, so that a token earned on
  another site that uses the same key cannot be spent here.  Empty = not compared (a warning at start-up).  The address of the client goes to Cloudflare (``remoteip``).
* Set: no token or a token Cloudflare refuses -> False.  Cloudflare unreachable / answers with an error -> True and a warning in the log:
  a real application must not be lost because of someone else's outage; the limits behind this check still hold.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TIMEOUT_SECONDS = 5.0
MAX_TOKEN_LENGTH = 2048  # the documented maximum of a Turnstile response


def secret() -> str:
    return (os.getenv("TURNSTILE_SECRET_KEY") or "").strip()


def hostnames() -> frozenset:
    return frozenset(h.strip().lower() for h in (os.getenv("TURNSTILE_HOSTNAMES") or "").split(",") if h.strip())


def _siteverify(secret_key: str, token: str, remote_ip: Optional[str] = None) -> Any:
    """The answer of Cloudflare as a dict; raises on a network error or a non-2xx status (the only function that touches the network)."""
    data = {"secret": secret_key, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    response = httpx.post(SITEVERIFY_URL, data=data, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def verify(token: Any, remote_ip: Optional[str] = None) -> bool:
    key = secret()
    if not key:
        return True
    if not isinstance(token, str) or not token.strip() or len(token) > MAX_TOKEN_LENGTH:
        return False
    try:
        answer = _siteverify(key, token.strip(), remote_ip)
    except Exception:  # noqa: BLE001 - an outage of the check must not drop an application
        logger.warning("turnstile: Cloudflare did not answer - the request is accepted without the check", exc_info=True)
        return True
    if not (isinstance(answer, dict) and answer.get("success") is True):
        return False
    allowed = hostnames()
    return not allowed or str(answer.get("hostname") or "").strip().lower() in allowed
