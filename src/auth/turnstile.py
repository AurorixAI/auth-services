"""Cloudflare Turnstile for the public demo request (A5): the token of the form is checked on the server.

* ``TURNSTILE_SECRET_KEY`` not set -> the check is skipped (local runs, tests, a stack without the key).
* ``TURNSTILE_HOSTNAMES`` (comma separated, e.g. ``algolab.me,www.algolab.me``): the ``hostname`` of Cloudflare's answer must be one of them, so that a token earned on
  another site that uses the same key cannot be spent here.  Empty = not compared (a warning at start-up).  The address of the client goes to Cloudflare (``remoteip``).
* Set: no token or a token Cloudflare refuses -> False.  Cloudflare UNREACHABLE (network error, DNS, timeout) -> True and an ERROR in the log at most once in
  10 minutes (with the number of unchecked requests): a real application must not be lost because of someone else's outage; the limits behind this check still
  hold.  Cloudflare answering with an error status -> False.  ``action`` (when the caller names one) must match the widget that made the token.
"""
from __future__ import annotations

import logging
import os
import threading
import time
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


# An outage of the check is logged as ERROR at most once in 10 minutes, with the number of checks that went through unchecked since the previous line (K2-2 R3, 5a).
OUTAGE_LOG_EVERY_SECONDS = 600
_outage_lock = threading.Lock()
_outage_state = {"last_log": None, "skipped": 0}


def _note_outage(reason: str) -> None:
    now = time.monotonic()
    with _outage_lock:
        _outage_state["skipped"] += 1
        last = _outage_state["last_log"]
        if last is not None and now - last < OUTAGE_LOG_EVERY_SECONDS:
            return
        skipped, _outage_state["skipped"], _outage_state["last_log"] = _outage_state["skipped"], 0, now
    logger.error("turnstile: Cloudflare is unreachable (%s) - requests are accepted WITHOUT the check; %s unchecked since the last line", reason, skipped)


def _action_matches(answer: dict, expected: Optional[str]) -> bool:
    """The token must have been made by the widget of THIS form (``action``): a token of the landing's form is not a token of «Забыли пароль». The testing keys
    of Cloudflare never carry an action (``metadata.result_with_testing_key``): they are exempt, a real key is not."""
    if expected is None:
        return True
    if isinstance(answer.get("metadata"), dict) and answer["metadata"].get("result_with_testing_key") is True:
        return True
    return str(answer.get("action") or "") == expected


def verify(token: Any, remote_ip: Optional[str] = None, action: Optional[str] = None) -> bool:
    key = secret()
    if not key:
        return True
    if not isinstance(token, str) or not token.strip() or len(token) > MAX_TOKEN_LENGTH:
        return False
    try:
        answer = _siteverify(key, token.strip(), remote_ip)
    except (httpx.TransportError, TimeoutError, OSError) as exc:  # no network, DNS, a timeout: the ONLY case that lets a request through unchecked
        _note_outage(type(exc).__name__)
        return True
    except Exception as exc:  # noqa: BLE001 - Cloudflare answered, but with an error status or something that is not JSON: not the same as «unreachable»
        logger.warning("turnstile: Cloudflare answered with an error (%s) - the request is refused", type(exc).__name__)
        return False
    if not (isinstance(answer, dict) and answer.get("success") is True):
        return False
    if not _action_matches(answer, action):
        return False
    allowed = hostnames()
    return not allowed or str(answer.get("hostname") or "").strip().lower() in allowed
