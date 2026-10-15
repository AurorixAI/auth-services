"""Password recovery for staff accounts (N13): «Забыли пароль?» and the link an owner / admin hands over.

The first half needs no database (token shape, masking, the shared password policy, how the link's site
address is chosen, the access-token revocation rule); the second half (``needs_pg``) drives the real
routes against the disposable ``a2_auth`` PostgreSQL: enumeration-neutral answers, rate limits, expiry,
single use, scope rules, revocation of the old logins.
"""
import hashlib
import logging
import os
import re
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import src.api.internal_router  # noqa: E402,F401  (import order: avoids a pre-existing router cycle)
from src.api import rate_limit as rl  # noqa: E402
from src.auth import password_reset as pr  # noqa: E402
from src.auth import password_reset_router as pr_router  # noqa: E402
from src.auth import router as auth_router_module  # noqa: E402
from src.auth.dependencies import get_current_active_user_payload  # noqa: E402
from src.auth.password_policy import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH  # noqa: E402
from src.auth.schemas import (  # noqa: E402
    AcceptInviteRequest,
    ChangePasswordRequest,
    OrgApplicationRequest,
)
from src.auth.service import create_access_token, hash_password, verify_password  # noqa: E402
from src.core import email as email_module  # noqa: E402
from src.core.config import get_settings  # noqa: E402
from src.core.database import get_db  # noqa: E402
from src.core.exceptions import EXCEPTION_STATUS_MAP, AlgoException  # noqa: E402
from src.models.auth import PasswordResetToken  # noqa: E402

from tests.pg_world import make_member, make_user, needs_pg, pg_session, world  # noqa: E402,F401

API = "/api/v1/auth"
SITE = "https://algolab.me"
OLD_PASSWORD = "OldPass-123"
NEW_PASSWORD = "NewPass-456"
NEUTRAL = {"ok": True, "email_delivery": True}
INVALID = "Ссылка недействительна или устарела. Попросите новую."


# ---- fixtures ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def memory_limiter(monkeypatch):
    """Deterministic: the in-memory window, an empty store, no Redis."""
    rl._memory_store.clear()
    monkeypatch.setattr(rl, "_check_redis", rl._check_memory)
    yield
    rl._memory_store.clear()


@pytest.fixture
def mailbox(monkeypatch):
    """SMTP configured, FRONTEND_URL set; every e-mail is captured instead of being sent."""
    sent = []
    monkeypatch.setattr(email_module, "SMTP_HOST", "smtp.test.invalid")
    monkeypatch.setattr(email_module, "SMTP_USER", "mailer")
    monkeypatch.setattr(email_module, "SMTP_PASSWORD", "x")
    monkeypatch.setenv("FRONTEND_URL", SITE)

    def capture(to, subject, body):
        sent.append(SimpleNamespace(to=to, subject=subject, body=body))
        return True

    monkeypatch.setattr(email_module, "_send_raw", capture)
    return sent


def token_in(message) -> str:
    found = re.findall(r"/reset-password\?token=([A-Za-z0-9_-]+)", message.body)
    assert found and len(set(found)) == 1, "the e-mail carries exactly one reset link"
    return found[0]


def token_in_url(url: str) -> str:
    assert url.startswith(f"{SITE}/reset-password?token=")
    return url.split("token=", 1)[1]


def client_for(db=None, **headers):
    app = FastAPI()

    @app.exception_handler(AlgoException)
    async def algo_exception_handler(request, exc):  # the same mapping as main_v2: login errors are 401, not a crash
        return JSONResponse(
            status_code=EXCEPTION_STATUS_MAP.get(type(exc), 400),
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(auth_router_module.router, prefix="/api/v1")
    app.include_router(pr_router.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db if db is not None else MagicMock()
    return TestClient(app, headers=headers or None)


def bearer(user, org=None, org_role=None):
    """A real access JWT; role and membership are re-read live by the dependency."""
    token = create_access_token(
        str(user.id), user.role, str(org.id) if org is not None else None, org_role
    )
    return {"Authorization": f"Bearer {token}"}


def rows(db, user):
    db.expire_all()
    return db.query(PasswordResetToken).filter(PasswordResetToken.user_id == user.id).all()


def live_rows(db, user):
    return [r for r in rows(db, user) if r.used_at is None and r.expires_at > pr.utcnow()]


def with_password(db, user, password=OLD_PASSWORD, org=None):
    user.password_hash = hash_password(password)
    if org is not None:
        user.default_org_id = org.id
    db.commit()
    return user


def login(client, user, password=OLD_PASSWORD, **extra):
    return client.post(f"{API}/login", json={"email": user.email, "password": password, **extra})


# ---- no database: token, masking, policy, base URL, revocation rule -------------------


def test_raw_tokens_are_long_random_and_only_their_sha256_is_the_digest():
    tokens = {pr.new_raw_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(pr.looks_like_token(t) and len(t) >= 43 for t in tokens)  # 32 bytes = 43 urlsafe characters
    sample = next(iter(tokens))
    digest = pr.token_digest(sample)
    assert digest == hashlib.sha256(sample.encode()).hexdigest() and len(digest) == 64 and digest != sample


@pytest.mark.parametrize("value", [None, 5, "", "short", "a" * 15, "a" * 129, "has space " * 5, "тест" * 10, "../" * 10])
def test_a_value_that_cannot_be_one_of_our_tokens_is_rejected_before_any_lookup(value):
    assert pr.looks_like_token(value) is False


def test_email_masking_shows_a_hint_not_the_address():
    assert pr.mask_email("arslan@gmail.com") == "a***@g***.com"
    assert pr.mask_email("t@mail.school.kz") == "t***@m***.kz"
    assert pr.mask_email("x@localhost") == "x***@l***"
    assert pr.mask_email("nodomain") == "n***"
    assert "rslan" not in pr.mask_email("arslan@gmail.com") and "mail" not in pr.mask_email("arslan@gmail.com")


def test_the_password_policy_is_one_set_of_limits_for_every_door():
    assert (PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH) == (6, 128)  # unchanged from before the recovery existed
    fields = [
        OrgApplicationRequest.model_fields["password"],
        AcceptInviteRequest.model_fields["password"],
        ChangePasswordRequest.model_fields["new_password"],
        pr_router.ResetPasswordRequest.model_fields["new_password"],
    ]
    for field in fields:
        limits = {type(m).__name__: (getattr(m, "min_length", None), getattr(m, "max_length", None)) for m in field.metadata}
        assert limits["MinLen"][0] == PASSWORD_MIN_LENGTH and limits["MaxLen"][1] == PASSWORD_MAX_LENGTH


def test_link_base_url_is_frontend_url_else_a_cors_origin_never_anything_else(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "cors_origins", ["https://algolab.me", "http://localhost:3000"])
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    # no FRONTEND_URL: only an allow-listed Origin counts
    assert pr.frontend_base_url("https://algolab.me") == "https://algolab.me"
    assert pr.frontend_base_url("https://algolab.me/") == "https://algolab.me"
    assert pr.frontend_base_url("https://evil.example") is None
    assert pr.frontend_base_url("https://algolab.me.evil.example") is None
    assert pr.frontend_base_url(None) is None
    # a wildcard list is not a whitelist
    monkeypatch.setattr(settings, "cors_origins", ["*"])
    assert pr.frontend_base_url("https://evil.example") is None
    # FRONTEND_URL wins over the request, loses its path / credentials, and is never replaced by Origin
    monkeypatch.setenv("FRONTEND_URL", "https://stage.algolab.me/some/path/")
    assert pr.frontend_base_url("https://evil.example") == "https://stage.algolab.me"
    monkeypatch.setenv("FRONTEND_URL", "javascript:alert(1)")
    assert pr.frontend_base_url("https://evil.example") is None
    monkeypatch.setenv("FRONTEND_URL", "https://user:pw@algolab.me")
    assert pr.frontend_base_url(None) is None


def test_reset_url_carries_the_token_in_the_query_of_the_reset_page():
    assert pr.reset_url("https://algolab.me", "abc-DEF_123") == "https://algolab.me/reset-password?token=abc-DEF_123"


def _resolve_with(sessions_valid_after, iat):
    user_id, org_id = uuid4(), uuid4()
    payload = {"sub": str(user_id), "role": "teacher", "org_id": str(org_id), "org_role": "teacher", "type": "access"}
    if iat is not None:
        payload["iat"] = iat
    user = SimpleNamespace(id=user_id, role="teacher", is_active=True, sessions_valid_after=sessions_valid_after)

    def query(value):
        q = MagicMock()
        q.filter.return_value.first.return_value = value
        return q

    db = MagicMock()
    db.query.side_effect = [query(user), query(SimpleNamespace(org_role="teacher")), query(SimpleNamespace(id=org_id, status="active"))]
    return get_current_active_user_payload(payload=payload, db=db)


def test_access_token_issued_before_the_revocation_instant_is_rejected_a_later_one_is_not():
    now = pr.utcnow()
    stamp = int(now.timestamp())
    assert _resolve_with(None, stamp - 3600)["org_role"] == "teacher"            # never revoked: untouched
    assert _resolve_with(now, stamp + 5)["org_role"] == "teacher"                # logged in after the reset
    assert _resolve_with(now, stamp)["org_role"] == "teacher"                    # the same second: not locked out
    for iat in (stamp - 1, stamp - 3600, None, "not-a-number"):
        with pytest.raises(HTTPException) as exc:
            _resolve_with(now, iat)
        assert exc.value.status_code == 401 and exc.value.detail == "Сессия завершена. Войдите снова."


def test_reset_email_is_escaped_and_never_logged_with_its_token(monkeypatch, caplog):
    sent = []
    monkeypatch.setattr(email_module, "SMTP_HOST", "smtp.test.invalid")
    monkeypatch.setattr(email_module, "SMTP_USER", "mailer")
    monkeypatch.setattr(email_module, "SMTP_PASSWORD", "x")
    monkeypatch.setattr(email_module, "_send_raw", lambda to, subject, body: sent.append((to, subject, body)) or True)
    with caplog.at_level(logging.DEBUG):
        email_module.send_password_reset_email("a@b.kz", "<b>Иван</b>", f"{SITE}/reset-password?token=SECRETTOKEN123456", 60)
    to, subject, body = sent[0]
    assert to == "a@b.kz" and subject == "[ALGO] Восстановление пароля"
    assert "<b>Иван</b>" not in body and "&lt;b&gt;Иван&lt;/b&gt;" in body
    assert f"{SITE}/reset-password?token=SECRETTOKEN123456" in body and "1 ч" in body
    assert "SECRETTOKEN123456" not in caplog.text


def test_without_smtp_the_reset_email_is_not_even_logged_with_its_body(monkeypatch, caplog):
    monkeypatch.setattr(email_module, "SMTP_HOST", "")
    with caplog.at_level(logging.DEBUG):
        assert email_module.send_password_reset_email("a@b.kz", "Иван", f"{SITE}/reset-password?token=SECRETTOKEN123456", 60) is False
    assert "SECRETTOKEN123456" not in caplog.text
    assert email_module.smtp_configured() is False


# ---- «Забыли пароль?» -------------------------------------------------------------


@needs_pg
def test_forgot_answers_the_same_for_a_known_an_unknown_and_a_disabled_account(world, mailbox):
    w = world
    disabled = make_user(w.db, "teacher")
    disabled.is_active = False
    w.db.commit()
    client = client_for(w.db)
    answers = []
    for email in (w.teacher.email, f"nobody-{uuid4()}@n13.invalid", disabled.email, "not-an-email"):
        response = client.post(f"{API}/password/forgot", json={"email": email})
        answers.append((response.status_code, response.json(), response.headers.get("set-cookie")))
    assert answers == [(200, NEUTRAL, None)] * 4
    # ... and only the active, existing account got a link
    assert [m.to for m in mailbox] == [w.teacher.email]
    assert len(live_rows(w.db, w.teacher)) == 1 and not rows(w.db, disabled)


@needs_pg
def test_forgot_sends_a_link_to_the_reset_page_and_stores_only_the_hash(world, mailbox):
    w = world
    response = client_for(w.db).post(f"{API}/password/forgot", json={"email": f"  {w.teacher.email.upper()}  "})
    assert response.json() == NEUTRAL
    message = mailbox[0]
    raw = token_in(message)
    assert f"{SITE}/reset-password?token={raw}" in message.body
    (row,) = rows(w.db, w.teacher)
    assert row.purpose == "email" and row.created_by is None and row.used_at is None
    assert row.token_hash == hashlib.sha256(raw.encode()).hexdigest() and raw not in row.token_hash
    assert timedelta(minutes=59) < row.expires_at - row.created_at <= timedelta(hours=1)
    assert len(raw) >= 43


@needs_pg
def test_a_new_request_kills_the_older_unused_link_of_the_account(world, mailbox):
    w = world
    client = client_for(w.db)
    client.post(f"{API}/password/forgot", json={"email": w.teacher.email})
    client.post(f"{API}/password/forgot", json={"email": w.teacher.email})
    first, second = (token_in(m) for m in mailbox)
    assert first != second
    assert client.get(f"{API}/password/reset/check", params={"token": first}).json() == {"valid": False}
    assert client.get(f"{API}/password/reset/check", params={"token": second}).json()["valid"] is True
    assert len(live_rows(w.db, w.teacher)) == 1


@needs_pg
def test_forgot_without_smtp_says_so_and_sends_and_stores_nothing(world, monkeypatch, caplog):
    w = world
    monkeypatch.setattr(email_module, "SMTP_HOST", "")
    monkeypatch.setenv("FRONTEND_URL", SITE)
    with caplog.at_level(logging.DEBUG):
        response = client_for(w.db).post(f"{API}/password/forgot", json={"email": w.teacher.email})
    assert response.status_code == 200 and response.json() == {"ok": True, "email_delivery": False}
    assert not rows(w.db, w.teacher)
    assert "reset-password" not in caplog.text  # the log-instead-of-send fallback must not print a live link


@needs_pg
def test_forgot_does_not_send_without_a_trusted_site_address(world, mailbox, monkeypatch):
    w = world
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    monkeypatch.setattr(get_settings(), "cors_origins", [SITE])
    for origin in (None, "https://evil.example"):
        headers = {"Origin": origin} if origin else {}
        response = client_for(w.db).post(f"{API}/password/forgot", json={"email": w.teacher.email}, headers=headers)
        assert response.json() == {"ok": True, "email_delivery": False}
    assert not mailbox and not rows(w.db, w.teacher)
    # an Origin that IS a CORS origin is a trusted site address
    response = client_for(w.db).post(f"{API}/password/forgot", json={"email": w.teacher.email}, headers={"Origin": SITE})
    assert response.json() == NEUTRAL and token_in(mailbox[0])
    assert f"{SITE}/reset-password?token=" in mailbox[0].body


@needs_pg
def test_forgot_is_limited_per_mailbox_and_per_address_with_the_same_neutral_answer(world, mailbox):
    w = world
    client = client_for(w.db)
    for _ in range(5):
        assert client.post(f"{API}/password/forgot", json={"email": w.teacher.email}).json() == NEUTRAL
    assert len(mailbox) == 3                                             # 3 per hour per mailbox, the rest silently dropped
    # every request counts against the address, also the ones the mailbox limit dropped: 5 used of 10
    others = [make_user(w.db, "teacher") for _ in range(8)]
    w.db.commit()
    for user in others:
        assert client.post(f"{API}/password/forgot", json={"email": user.email}).json() == NEUTRAL
    assert len(mailbox) == 3 + 5                                          # 10 per hour per address: 5 + 5 counted, the other 3 dropped
    # a different client address has its own budget
    fresh = client_for(w.db, **{"X-Real-IP": "203.0.113.50"})
    assert fresh.post(f"{API}/password/forgot", json={"email": others[-1].email}).json() == NEUTRAL
    assert len(mailbox) == 3 + 5 + 1


# ---- check / reset ----------------------------------------------------------------


def issue_email_link(w, mailbox, user=None):
    user = user or w.teacher
    client_for(w.db).post(f"{API}/password/forgot", json={"email": user.email})
    return token_in(mailbox[-1])


@needs_pg
def test_check_reports_a_live_link_with_a_masked_address_and_hides_everything_else(world, mailbox):
    w = world
    raw = issue_email_link(w, mailbox)
    client = client_for(w.db)
    response = client.get(f"{API}/password/reset/check", params={"token": raw})
    body = response.json()
    assert response.status_code == 200 and body["valid"] is True
    assert body["email_masked"] == pr.mask_email(w.teacher.email) and w.teacher.email not in response.text
    assert body["expires_at"] and response.headers["cache-control"] == "no-store"
    for junk in ("", "x", "a" * 43, "../../etc/passwd", pr.new_raw_token()):
        assert client.get(f"{API}/password/reset/check", params={"token": junk}).json() == {"valid": False}


@needs_pg
def test_check_says_invalid_for_an_expired_a_used_and_a_disabled_accounts_link(world, mailbox):
    w = world
    client = client_for(w.db)
    expired = issue_email_link(w, mailbox)
    (row,) = live_rows(w.db, w.teacher)
    row.expires_at = pr.utcnow() - timedelta(seconds=1)
    w.db.commit()
    assert client.get(f"{API}/password/reset/check", params={"token": expired}).json() == {"valid": False}

    used = issue_email_link(w, mailbox)
    (row,) = live_rows(w.db, w.teacher)
    row.used_at = pr.utcnow()
    w.db.commit()
    assert client.get(f"{API}/password/reset/check", params={"token": used}).json() == {"valid": False}

    gone = issue_email_link(w, mailbox, w.teacher2)
    w.teacher2.is_active = False
    w.db.commit()
    assert client.get(f"{API}/password/reset/check", params={"token": gone}).json() == {"valid": False}


@needs_pg
def test_reset_changes_the_password_signs_nobody_in_and_works_only_once(world, mailbox):
    w = world
    with_password(w.db, w.teacher)
    w.teacher.must_change_password = True
    w.db.commit()
    raw = issue_email_link(w, mailbox)
    client = client_for(w.db)

    response = client.post(f"{API}/password/reset", json={"token": raw, "new_password": NEW_PASSWORD})
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert "set-cookie" not in response.headers and response.headers["cache-control"] == "no-store"
    assert "access_token" not in response.text
    w.db.expire_all()
    assert verify_password(NEW_PASSWORD, w.teacher.password_hash) and not verify_password(OLD_PASSWORD, w.teacher.password_hash)
    assert w.teacher.must_change_password is False
    (row,) = rows(w.db, w.teacher)
    assert row.used_at is not None

    # single use
    again = client.post(f"{API}/password/reset", json={"token": raw, "new_password": "Another-789"})
    assert again.status_code == 400 and again.json()["detail"] == INVALID
    w.db.expire_all()
    assert verify_password(NEW_PASSWORD, w.teacher.password_hash)
    # the new password logs in, the old one does not
    assert login(client, w.teacher, NEW_PASSWORD).status_code == 200
    assert login(client, w.teacher, OLD_PASSWORD).status_code == 401


@needs_pg
def test_every_way_a_link_can_be_dead_gets_the_same_400(world, mailbox):
    w = world
    client = client_for(w.db)
    expired = issue_email_link(w, mailbox)
    (row,) = live_rows(w.db, w.teacher)
    row.expires_at = pr.utcnow() - timedelta(minutes=1)
    w.db.commit()
    disabled = issue_email_link(w, mailbox, w.teacher2)
    w.teacher2.is_active = False
    w.db.commit()
    for token in (expired, disabled, pr.new_raw_token(), "x" * 30, "тест" * 10, "a" * 129, "a" * 512):
        response = client.post(f"{API}/password/reset", json={"token": token, "new_password": NEW_PASSWORD})
        assert (response.status_code, response.json()) == (400, {"detail": INVALID}), token
    w.db.expire_all()
    assert w.teacher2.password_hash == "synthetic-only"  # untouched by the dead links


@needs_pg
def test_reset_enforces_the_registration_password_policy_and_keeps_the_link_alive(world, mailbox):
    w = world
    raw = issue_email_link(w, mailbox)
    client = client_for(w.db)
    for bad in ("12345", "", "x" * (PASSWORD_MAX_LENGTH + 1)):
        assert client.post(f"{API}/password/reset", json={"token": raw, "new_password": bad}).status_code == 422
    assert client.get(f"{API}/password/reset/check", params={"token": raw}).json()["valid"] is True
    for ok in ("123456", "x" * PASSWORD_MAX_LENGTH):
        token = issue_email_link(w, mailbox)
        assert client.post(f"{API}/password/reset", json={"token": token, "new_password": ok}).status_code == 200


@needs_pg
def test_reset_ends_every_old_login_of_the_account(world, mailbox):
    w = world
    with_password(w.db, w.teacher, org=w.org)
    client = client_for(w.db)
    session = login(client, w.teacher)
    assert session.status_code == 200
    tokens = session.json()["tokens"]
    header = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert client.get(f"{API}/me", headers=header).status_code == 200

    # the revocation instant counts whole seconds like the JWT's iat: let the old token's second pass
    raw = issue_email_link(w, mailbox)
    time.sleep(1.1)
    assert client.post(f"{API}/password/reset", json={"token": raw, "new_password": NEW_PASSWORD}).status_code == 200

    w.db.expire_all()
    assert w.teacher.refresh_token_hash is None and w.teacher.sessions_valid_after is not None
    assert client.post(f"{API}/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401
    stale = client.get(f"{API}/me", headers=header)
    assert stale.status_code == 401 and stale.json()["detail"] == "Сессия завершена. Войдите снова."
    # a login made after the reset is a fresh session
    fresh = login(client, w.teacher, NEW_PASSWORD).json()["tokens"]["access_token"]
    assert client.get(f"{API}/me", headers={"Authorization": f"Bearer {fresh}"}).status_code == 200


@needs_pg
def test_spending_a_link_expires_every_other_unused_link_of_the_account(world, mailbox):
    w = world
    raw = issue_email_link(w, mailbox)
    # a second live link created behind the back of the "one live link" rule (e.g. by an older release)
    w.db.add(PasswordResetToken(user_id=w.teacher.id, token_hash=pr.token_digest(pr.new_raw_token()), purpose="issued",
                                expires_at=pr.utcnow() + timedelta(hours=5)))
    w.db.commit()
    assert len(live_rows(w.db, w.teacher)) == 2
    assert client_for(w.db).post(f"{API}/password/reset", json={"token": raw, "new_password": NEW_PASSWORD}).status_code == 200
    assert live_rows(w.db, w.teacher) == []


@needs_pg
def test_the_same_link_cannot_be_spent_twice_even_by_two_sessions(world, mailbox):
    """The claim is one UPDATE ... WHERE used_at IS NULL: whichever session runs second gets nothing."""
    from sqlalchemy.orm import sessionmaker

    w = world
    raw = issue_email_link(w, mailbox)
    other = sessionmaker(bind=w.db.get_bind())()
    try:
        assert pr.spend_token(other, raw, NEW_PASSWORD).id == w.teacher.id
        with pytest.raises(pr.InvalidResetLink):
            pr.spend_token(w.db, raw, "Second-attempt-1")
    finally:
        other.close()
    w.db.expire_all()
    assert verify_password(NEW_PASSWORD, w.teacher.password_hash)


@needs_pg
def test_reset_attempts_are_limited_per_address(world, mailbox):
    w = world
    client = client_for(w.db)
    for _ in range(20):
        assert client.post(f"{API}/password/reset", json={"token": pr.new_raw_token(), "new_password": NEW_PASSWORD}).status_code == 400
    blocked = client.post(f"{API}/password/reset", json={"token": pr.new_raw_token(), "new_password": NEW_PASSWORD})
    assert blocked.status_code == 429
    # another address is not affected
    other = client_for(w.db, **{"X-Real-IP": "203.0.113.77"})
    assert other.post(f"{API}/password/reset", json={"token": pr.new_raw_token(), "new_password": NEW_PASSWORD}).status_code == 400


# ---- the link an owner / admin / superadmin hands over -------------------------------


def issue(client, target, headers, **extra):
    return client.post(f"{API}/staff/{target.id}/password-reset-link", headers={**headers, **extra})


@pytest.fixture
def staff(world, monkeypatch):
    """The world plus a second owner, a platform superadmin and a user who sits in two organisations."""
    w = world
    monkeypatch.setenv("FRONTEND_URL", SITE)
    w.owner2 = make_user(w.db, "owner")
    make_member(w.db, w.org, w.owner2, "owner")
    w.admin2 = make_user(w.db, "admin")
    make_member(w.db, w.org, w.admin2, "admin")
    w.foreign_owner = make_user(w.db, "owner")
    make_member(w.db, w.other_org, w.foreign_owner, "owner")
    w.foreign_teacher = make_user(w.db, "teacher")
    make_member(w.db, w.other_org, w.foreign_teacher, "teacher")
    w.superadmin = make_user(w.db, "superadmin")
    w.both = make_user(w.db, "teacher")
    make_member(w.db, w.org, w.both, "teacher")
    make_member(w.db, w.other_org, w.both, "teacher")
    w.db.commit()
    w.client = client_for(w.db)
    w.as_owner = bearer(w.owner, w.org, "owner")
    w.as_admin = bearer(w.admin, w.org, "admin")
    w.as_teacher = bearer(w.teacher, w.org, "teacher")
    w.as_super = bearer(w.superadmin)
    return w


@needs_pg
def test_owner_may_issue_for_admins_and_teachers_of_their_own_organisation(staff):
    w = staff
    for target in (w.admin, w.admin2, w.teacher, w.teacher2):
        response = issue(w.client, target, w.as_owner)
        assert response.status_code == 200, (target.role, response.text)
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        raw = token_in_url(body["url"])
        assert set(body) == {"url", "expires_at"}
        (row,) = live_rows(w.db, target)
        assert row.purpose == "issued" and row.created_by == w.owner.id and row.token_hash == pr.token_digest(raw)
        assert timedelta(hours=23, minutes=59) < row.expires_at - row.created_at <= timedelta(hours=24)
        assert body["expires_at"] == row.expires_at.isoformat()


@needs_pg
def test_owner_cannot_reach_owners_themselves_other_organisations_or_strangers(staff):
    w = staff
    for target in (w.owner, w.owner2, w.foreign_owner, w.foreign_admin, w.foreign_teacher, w.superadmin, w.both):
        response = issue(w.client, target, w.as_owner)
        assert (response.status_code, response.json()) == (404, {"detail": "Сотрудник не найден"}), target.role
        assert not rows(w.db, target)
    for ident in (str(uuid4()), "not-a-uuid", "1", "' OR 1=1 --"):
        assert w.client.post(f"{API}/staff/{ident}/password-reset-link", headers=w.as_owner).status_code == 404


@needs_pg
def test_admin_may_issue_for_teachers_only(staff):
    w = staff
    for target in (w.teacher, w.teacher2):
        assert issue(w.client, target, w.as_admin).status_code == 200
    for target in (w.owner, w.owner2, w.admin, w.admin2, w.foreign_teacher, w.foreign_admin, w.both, w.superadmin):
        assert issue(w.client, target, w.as_admin).status_code == 404, target.role
        assert not rows(w.db, target)


@needs_pg
def test_teacher_and_anonymous_cannot_issue_and_the_foreign_admin_cannot_reach_this_org(staff):
    w = staff
    assert issue(w.client, w.teacher2, w.as_teacher).status_code == 403
    assert issue(w.client, w.teacher2, {}).status_code == 401
    assert issue(w.client, w.teacher2, bearer(w.foreign_admin, w.other_org, "admin")).status_code == 404
    # the claims of the token are not trusted: the membership is read live
    assert issue(w.client, w.teacher2, bearer(w.teacher, w.org, "owner")).status_code == 403
    assert not rows(w.db, w.teacher2)


@needs_pg
def test_superadmin_may_issue_for_organisation_owners_only(staff):
    w = staff
    for target in (w.owner, w.owner2, w.foreign_owner):
        assert issue(w.client, target, w.as_super).status_code == 200, target.role
    for target in (w.admin, w.teacher, w.foreign_teacher, w.foreign_admin, w.superadmin):
        assert issue(w.client, target, w.as_super).status_code == 404, target.role


@needs_pg
def test_a_disabled_account_and_a_removed_member_are_out_of_reach(staff):
    w = staff
    w.teacher.is_active = False
    w.db.commit()
    assert issue(w.client, w.teacher, w.as_owner).status_code == 404
    from src.models.auth import OrganizationMember

    member = w.db.query(OrganizationMember).filter_by(user_id=w.teacher2.id).one()
    member.is_active = False
    w.db.commit()
    assert issue(w.client, w.teacher2, w.as_owner).status_code == 404


@needs_pg
def test_issuing_replaces_the_previous_link_and_the_link_really_resets_the_password(staff):
    w = staff
    with_password(w.db, w.teacher)
    first = token_in_url(issue(w.client, w.teacher, w.as_owner).json()["url"])
    second = token_in_url(issue(w.client, w.teacher, w.as_admin).json()["url"])
    assert first != second
    assert w.client.get(f"{API}/password/reset/check", params={"token": first}).json() == {"valid": False}
    check = w.client.get(f"{API}/password/reset/check", params={"token": second}).json()
    assert check["valid"] is True and check["email_masked"] == pr.mask_email(w.teacher.email)
    assert w.client.post(f"{API}/password/reset", json={"token": first, "new_password": NEW_PASSWORD}).status_code == 400
    assert w.client.post(f"{API}/password/reset", json={"token": second, "new_password": NEW_PASSWORD}).status_code == 200
    assert login(w.client, w.teacher, NEW_PASSWORD).status_code == 200


@needs_pg
def test_the_issued_link_points_to_the_trusted_site_only(staff, monkeypatch):
    w = staff
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    monkeypatch.setattr(get_settings(), "cors_origins", [SITE])
    # no FRONTEND_URL and no usable Origin: refuse instead of inventing an address
    nothing = issue(w.client, w.teacher, w.as_owner)
    assert nothing.status_code == 503 and "token" not in nothing.text
    assert issue(w.client, w.teacher, w.as_owner, Origin="https://evil.example").status_code == 403
    assert not rows(w.db, w.teacher)
    ok = issue(w.client, w.teacher, w.as_owner, Origin=SITE)
    assert ok.status_code == 200 and ok.json()["url"].startswith(f"{SITE}/reset-password?token=")
    # FRONTEND_URL beats whatever the request says
    monkeypatch.setenv("FRONTEND_URL", "https://stage.algolab.me")
    other = issue(w.client, w.teacher, w.as_owner, Origin=SITE)
    assert other.json()["url"].startswith("https://stage.algolab.me/reset-password?token=")


@needs_pg
def test_a_request_started_by_another_site_is_refused(staff):
    w = staff
    assert issue(w.client, w.teacher, w.as_owner, Origin="https://evil.example").status_code == 403
    assert issue(w.client, w.teacher, w.as_owner, **{"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert not rows(w.db, w.teacher)
    assert issue(w.client, w.teacher, w.as_owner, Origin="http://localhost:3000", **{"Sec-Fetch-Site": "same-origin"}).status_code == 200


@needs_pg
def test_the_audit_line_names_actor_and_target_and_never_the_token(staff, caplog):
    w = staff
    with caplog.at_level(logging.INFO, logger=pr_router.logger.name):
        response = issue(w.client, w.teacher, w.as_owner)
    raw = token_in_url(response.json()["url"])
    lines = [r.getMessage() for r in caplog.records if "password reset link issued" in r.getMessage()]
    assert len(lines) == 1
    assert str(w.owner.id) in lines[0] and str(w.teacher.id) in lines[0] and "owner" in lines[0]
    assert raw not in caplog.text and pr.token_digest(raw) not in caplog.text


@needs_pg
def test_a_link_for_someone_who_sits_in_two_organisations_is_never_issued_by_either(staff):
    """Resetting their password would hand the issuer a login into the OTHER tenant."""
    w = staff
    assert issue(w.client, w.both, w.as_owner).status_code == 404
    assert issue(w.client, w.both, bearer(w.foreign_admin, w.other_org, "admin")).status_code == 404
    assert not rows(w.db, w.both)
