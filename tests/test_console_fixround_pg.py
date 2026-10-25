"""R3 fix round of the console pack: platform block vs the cabinets' own reactivations, log redaction, lock rules, CORS / cache headers,
cookie name, form abuse, journal details, password limits."""
import logging
from datetime import timedelta
from uuid import uuid4

import pytest

from tests.console_helpers import (  # noqa: F401  (first: it loads the application)
    API, PASSWORD, actions, advance_clock, audit_rows, body_of, cw, letters_to, needs_pg, request_body,
)
from src.auth import password_links as links  # noqa: E402
from src.auth.platform_common import utcnow  # noqa: E402
from src.core.middleware import DiagnosticCapabilityLogFilter, sentry_before_send  # noqa: E402
from src.models.auth import Organization, OrganizationMember, User  # noqa: E402
from src.models.platform import DemoRequest, PlatformAudit, PlatformStaff  # noqa: E402

APP = "app.algolab.me"
MEMBERS = "/api/v1/auth/org/members"
BLOCKED_USER = "Пользователь заблокирован командой ALGO"


def cabinet(cw, user, password=PASSWORD):
    client = cw.new_client(APP)
    r = client.post("/api/v1/auth/login", json={"email": user.email, "password": password})
    assert r.status_code == 200, r.text
    return client


def member_id(cw, centre, user):
    cw.db.expire_all()
    return cw.db.query(OrganizationMember).filter_by(org_id=centre.org.id, user_id=user.id).one().id


# ── M1: a platform block is not lifted by the centre's own tools; an unblock restores what it found ──────────────────


@needs_pg
def test_a_blocked_member_cannot_be_restored_by_the_centre_and_stays_off_after_the_unblock(cw):
    centre = cw.make_centre()
    owner = cabinet(cw, centre.owner)
    mid = member_id(cw, centre, centre.teacher)
    assert owner.delete(f"{MEMBERS}/{mid}").status_code == 200                 # the centre removed him (is_active off)
    assert cw.client.post(f"{API}/users/{centre.teacher.id}/block", json={"reason": "Нарушение условий"}).status_code == 200
    r = owner.post(f"{MEMBERS}/{mid}/restore")
    assert r.status_code == 409 and BLOCKED_USER in str(r.json()) and r.json()["detail"]["code"] == "USER_BLOCKED_BY_PLATFORM"
    cw.db.expire_all()
    assert cw.db.get(User, centre.teacher.id).is_active is False
    assert cw.db.query(OrganizationMember).get(mid).is_active is False
    # the unblock does not bring back a person the centre had removed before the block
    r = cw.client.post(f"{API}/users/{centre.teacher.id}/unblock", json={})
    assert r.status_code == 200 and r.json()["is_active"] is False and r.json()["blocked"] is False
    assert audit_rows(cw.db, "user.unblocked")[0].details == {"reactivated": False}
    # and now the centre may restore him itself
    assert owner.post(f"{MEMBERS}/{mid}/restore").status_code == 200


@needs_pg
def test_an_invitation_does_not_lift_a_platform_block(cw):
    centre = cw.make_centre()
    owner = cabinet(cw, centre.owner)
    mid = member_id(cw, centre, centre.admin)
    assert owner.delete(f"{MEMBERS}/{mid}").status_code == 200
    cw.client.post(f"{API}/users/{centre.admin.id}/block", json={"reason": "Нарушение условий"})
    inv = owner.post("/api/v1/auth/org/invitations", json={"email": centre.admin.email, "org_role": "teacher"})
    assert inv.status_code == 200, inv.text
    r = cw.new_client(APP).post("/api/v1/auth/invite/accept", json={"code": inv.json()["code"], "password": "brand-new-pass-1"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "USER_BLOCKED_BY_PLATFORM"
    cw.db.expire_all()
    assert cw.db.get(User, centre.admin.id).is_active is False and cw.db.get(User, centre.admin.id).platform_blocked_at is not None


@needs_pg
def test_unblock_restores_an_active_member_only_while_his_membership_is_active(cw):
    centre = cw.make_centre()
    # (a) active before, active member: back on
    cw.client.post(f"{API}/users/{centre.teacher.id}/block", json={"reason": "Тест блокировки"})
    cw.db.expire_all()
    assert cw.db.get(User, centre.teacher.id).platform_block_was_active is True
    r = cw.client.post(f"{API}/users/{centre.teacher.id}/unblock", json={})
    assert r.json()["is_active"] is True and audit_rows(cw.db, "user.unblocked")[-1].details == {"reactivated": True}
    cw.db.expire_all()
    assert cw.db.get(User, centre.teacher.id).platform_block_was_active is None
    # (b) blocked, then removed by the centre meanwhile (membership inactive): stays off
    cw.client.post(f"{API}/users/{centre.admin.id}/block", json={"reason": "Тест блокировки"})
    cw.db.query(OrganizationMember).filter_by(user_id=centre.admin.id).update({"is_active": False})
    cw.db.commit()
    r = cw.client.post(f"{API}/users/{centre.admin.id}/unblock", json={})
    assert r.json()["is_active"] is False and r.json()["status"] == "inactive"
    cw.db.expire_all()
    assert cw.db.get(User, centre.admin.id).is_active is False


# ── M2: tokens out of logs ──────────────────────────────────────────────────────────────────────────────────────────


def test_link_tokens_never_reach_a_log_line():
    token = "AbCdEf0123456789_-AbCdEf0123456789_-abc"
    redact = DiagnosticCapabilityLogFilter._redact
    for raw in (f"/api/v1/auth/password-links/{token}", f"/auth/password-links/{token}?x=1", f"https://app.algolab.me/set-password/{token}"):
        out = redact(raw)
        assert token not in out and "[redacted]" in out, raw
    assert redact("/api/v1/auth/password-links/") == "/api/v1/auth/password-links/"
    # an access-log record as uvicorn builds it (five arguments)
    record = logging.LogRecord("uvicorn.access", logging.INFO, "x", 1, '%s - "%s %s HTTP/%s" %d',
                               ("203.0.113.5:1", "POST", f"/api/v1/auth/password-links/{token}", "1.1", 200), None)
    assert DiagnosticCapabilityLogFilter().filter(record) is True
    assert token not in record.getMessage() and "/api/v1/auth/password-links/[redacted]" in record.getMessage()


def test_the_log_filter_is_on_the_access_loggers_and_the_handler():
    from src.core.middleware import configure_logging

    configure_logging(is_development=False)
    for name in ("uvicorn.access", "gunicorn.access"):
        assert any(isinstance(f, DiagnosticCapabilityLogFilter) for f in logging.getLogger(name).filters)
    assert any(isinstance(f, DiagnosticCapabilityLogFilter) for f in logging.getLogger().handlers[0].filters)


def test_sentry_before_send_scrubs_the_link_token():
    token = "T" * 43
    event = {
        "request": {"url": f"https://app.algolab.me/api/v1/auth/password-links/{token}", "query_string": "invite_code=ABCD1234"},
        "transaction": f"/api/v1/auth/password-links/{token}",
        "breadcrumbs": {"values": [{"message": f"POST /api/v1/auth/password-links/{token}", "data": {"url": f"/set-password/{token}"}}]},
        "exception": {"values": [{"value": f"404 for /api/v1/auth/password-links/{token}"}]},
    }
    out = str(sentry_before_send(event))
    assert token not in out and "ABCD1234" not in out and "[redacted]" in out


@needs_pg
def test_a_real_request_with_a_link_token_leaves_no_token_in_the_log(cw, caplog):
    token = "Z" * 43
    handler = logging.StreamHandler()
    stream = []
    handler.emit = lambda rec: stream.append(rec.getMessage())  # noqa: E731
    handler.addFilter(DiagnosticCapabilityLogFilter())
    logging.getLogger("uvicorn.access").addHandler(handler)
    try:
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", f"/api/v1/auth/password-links/{token}", "1.1", 404)
    finally:
        logging.getLogger("uvicorn.access").removeHandler(handler)
    assert stream and token not in stream[0]


# ── M3: a lock is journaled once; the login door has a per-address limit ──────────────────────────────────────────────


@needs_pg
def test_a_lock_writes_one_row_when_it_starts_and_refused_attempts_write_none(cw):
    staff = cw.make_staff([])
    c = cw.new_client()
    for _ in range(5):
        c.post(f"{API}/auth/login", json={"email": staff.email, "password": "bad-bad-bad-bad"})
    n = cw.db.query(PlatformAudit).count()
    for _ in range(6):
        assert c.post(f"{API}/auth/login", json={"email": staff.email, "password": PASSWORD}).status_code == 429
    cw.db.expire_all()
    assert cw.db.query(PlatformAudit).count() == n                              # refused attempts: no rows
    locked = audit_rows(cw.db, "console.login.locked")
    assert len(locked) == 1 and locked[0].details["scopes"] and locked[0].actor_user_id == staff.user.id


@needs_pg
def test_the_login_door_is_limited_per_address_before_any_database_write(cw):
    c = cw.new_client()
    hdr = {"X-Real-IP": "198.51.100.200"}
    codes = []
    for i in range(32):
        # a different e-mail every time: no pair / e-mail lock, only the door limit can answer 429
        codes.append(c.post(f"{API}/auth/login", json={"email": f"nobody{i}@cn1.test", "password": "x" * 12}, headers=hdr).status_code)
    assert 429 in codes and codes.index(429) <= 30
    cw.db.expire_all()
    before = cw.db.query(PlatformAudit).count()
    r = c.post(f"{API}/auth/login", json={"email": "nobody99@cn1.test", "password": "x" * 12}, headers=hdr)
    assert r.status_code == 429 and r.json()["code"] in ("rate_limited", "login_locked") and int(r.headers["Retry-After"]) > 0
    cw.db.expire_all()
    assert cw.db.query(PlatformAudit).count() == before


# ── m1: origin for every method, no CORS, no cache ───────────────────────────────────────────────────────────────────


@needs_pg
def test_a_foreign_origin_is_refused_on_reads_too_and_gets_no_cors_headers(cw):
    for origin in ("https://evil.test", "http://localhost:3000", "null", "https://console.algolab.me.evil.test"):
        r = cw.client.get(f"{API}/auth/me", headers={"Origin": origin})
        assert r.status_code == 403 and r.json()["code"] == "bad_origin", origin
        assert not [h for h in r.headers if h.lower().startswith("access-control-")], origin
    ok = cw.client.get(f"{API}/auth/me", headers={"Origin": "https://console.algolab.me"})
    assert ok.status_code == 200 and not [h for h in ok.headers if h.lower().startswith("access-control-")]
    pre = cw.new_client().options(f"{API}/orgs", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"})
    assert not [h for h in pre.headers if h.lower().startswith("access-control-")]
    # the cabinets keep their own CORS
    health = cw.new_client(APP).get("/health/live", headers={"Origin": "http://localhost:3000"})
    assert health.headers.get("access-control-allow-origin") == "http://localhost:3000"


@needs_pg
def test_on_a_secure_stack_only_the_https_default_port_origin_of_a_console_host_passes(cw, monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    from src.auth.platform_security import is_console_origin

    assert is_console_origin("https://console.algolab.me") and is_console_origin("https://console.stage.algolab.me:443")
    for bad in ("http://console.algolab.me", "https://console.algolab.me:8443", "https://console.algolab.me.evil.test", "https://user@console.algolab.me",
                "https://app.algolab.me", "null", "", "https://console.algolab.me/path"):
        assert not is_console_origin(bad), bad


@needs_pg
def test_every_console_answer_is_no_store(cw):
    for r in (cw.client.get(f"{API}/auth/me"), cw.new_client().get(f"{API}/orgs"), cw.client.get(f"{API}/orgs/xx"),
              cw.new_client("app.algolab.me").get(f"{API}/auth/me"), cw.client.post(f"{API}/auth/login", json={})):
        assert r.headers["cache-control"] == "no-store" and r.headers["pragma"] == "no-cache"
    assert "no-store" not in (cw.new_client(APP).get("/health/live").headers.get("cache-control") or "")


# ── m2 / m3: the lock keys ───────────────────────────────────────────────────────────────────────────────────────────


@needs_pg
def test_an_attackers_guesses_do_not_lock_the_owner_out_of_his_own_address(cw):
    home = {"X-Real-IP": "203.0.113.5"}
    owner = cw.make_staff(owner=True)
    c = cw.new_client()
    assert c.post(f"{API}/auth/login", json={"email": owner.email, "password": PASSWORD}, headers=home).status_code == 200   # a known address now
    attacker = {"X-Real-IP": "198.51.100.66"}
    for _ in range(5):
        c.post(f"{API}/auth/login", json={"email": owner.email, "password": "bad-bad-bad-bad"}, headers=attacker)
    assert c.post(f"{API}/auth/login", json={"email": owner.email, "password": PASSWORD}, headers=attacker).status_code == 429   # the attacker is locked
    assert c.post(f"{API}/auth/login", json={"email": owner.email, "password": PASSWORD}, headers=home).status_code == 200      # the owner at home is not
    stranger = {"X-Real-IP": "192.0.2.77"}
    assert c.post(f"{API}/auth/login", json={"email": owner.email, "password": PASSWORD}, headers=stranger).status_code == 429   # a new address is
    for _ in range(5):                                                                                                           # the pair lock still binds the owner's own address
        c.post(f"{API}/auth/login", json={"email": owner.email, "password": "bad-bad-bad-bad"}, headers=home)
    assert c.post(f"{API}/auth/login", json={"email": owner.email, "password": PASSWORD}, headers=home).status_code == 429


@needs_pg
def test_failures_are_counted_inside_a_15_minute_window_for_staff_like_for_everybody(cw, monkeypatch):
    staff = cw.make_staff([])
    c = cw.new_client()
    for _ in range(4):
        assert c.post(f"{API}/auth/login", json={"email": staff.email, "password": "bad-bad-bad-bad"}).status_code == 401
    advance_clock(monkeypatch, 16 * 60)
    for _ in range(4):                                                         # 4 + 4 failures, 16 minutes apart: never 5 in a window
        assert c.post(f"{API}/auth/login", json={"email": staff.email, "password": "bad-bad-bad-bad"}).status_code == 401
    cw.db.expire_all()
    row = cw.db.query(PlatformStaff).filter_by(user_id=staff.user.id).one()
    assert row.failed_logins == 4 and row.locked_until is None


@needs_pg
def test_the_owner_reset_script_unlocks_and_sets_a_new_password(cw, monkeypatch, capsys):
    import getpass
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import reset_platform_owner as script

    owner = cw.make_staff(owner=True)
    c = cw.new_client()
    old_session = cw.signed_in(owner)
    far = {"X-Real-IP": "198.51.100.9"}
    for _ in range(5):
        c.post(f"{API}/auth/login", json={"email": owner.email, "password": "bad-bad-bad-bad"}, headers=far)
    assert c.post(f"{API}/auth/login", json={"email": owner.email, "password": PASSWORD}, headers=far).status_code == 429
    assert cw.login(cw.new_client(), owner).status_code in (200, 429)   # (the owner's own address may be exempt from the e-mail lock)
    new = "the-new-owner-password-1"

    def run(email, passwords):
        answers, secrets = iter([email]), iter(passwords)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
        monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(secrets))
        code = script.main()
        out = capsys.readouterr()
        return code, out.out, out.err

    code, out, err = run(cw.make_staff(["orgs.view"]).email, [new, new])           # not an owner
    assert code == 1 and "не найден" in err
    code, out, err = run(owner.email, [new, new + "x"])
    assert code == 1 and "не совпадают" in err
    code, out, err = run(owner.email.upper(), [new, new])
    assert code == 0 and err == "" and out.count("\n") == 1 and new not in out and owner.email in out
    assert old_session.get(f"{API}/auth/me").status_code == 401                    # the old sessions are over
    assert cw.new_client().post(f"{API}/auth/login", json={"email": owner.email, "password": new}, headers={"X-Real-IP": "192.0.2.9"}).status_code == 200   # the e-mail lock is gone
    assert cw.login(cw.new_client(), owner).status_code == 401
    row = audit_rows(cw.db, "staff.password_reset", target_id=str(owner.user.id))[-1]
    assert row.actor_user_id is None and new not in str(row.details)


# ── m4: what the journal keeps of a typed login ───────────────────────────────────────────────────────────────────────


@needs_pg
def test_a_typed_login_that_is_not_an_email_is_not_journaled(cw):
    typed = "my secret password 123"
    cw.new_client().post(f"{API}/auth/login", json={"email": typed, "password": "x" * 12})
    row = audit_rows(cw.db, "console.login.failed")[-1]
    assert row.details == {"email": "[не почта]", "email_length": len(typed)} and typed not in str(row.details)
    cw.new_client().post(f"{API}/auth/login", json={"email": "Real@Example.test", "password": "x" * 12})
    assert audit_rows(cw.db, "console.login.failed")[-1].details == {"email": "real@example.test"}


# ── m5: blocked centre vs invitations and activation ─────────────────────────────────────────────────────────────────


@needs_pg
def test_a_blocked_centre_takes_nobody_in_by_invitation_or_activation(cw):
    centre = cw.make_centre()
    owner = cabinet(cw, centre.owner)
    inv = owner.post("/api/v1/auth/org/invitations", json={"email": f"new-{uuid4().hex[:8]}@example.test", "org_role": "teacher"})
    code = inv.json()["code"]
    cw.client.post(f"{API}/orgs/{centre.org.id}/block", json={"reason": "Нарушение условий"})
    r = cw.new_client(APP).post("/api/v1/auth/invite/accept", json={"code": code, "password": "brand-new-pass-1", "first_name": "А", "last_name": "Б"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "ORG_BLOCKED" and "приостановлен" in r.json()["detail"]["message"]
    # activation by an old token
    from src.auth.service import hash_token

    token = "activation-token-" + uuid4().hex
    org = cw.db.get(Organization, centre.org.id)
    org.activation_token, org.activation_token_expires_at = hash_token(token), utcnow() + timedelta(days=1)
    cw.db.commit()
    r = cw.new_client(APP).post("/api/v1/auth/activate", json={"token": token})
    assert r.status_code == 400 and "приостановлен" in r.json()["detail"]
    cw.client.post(f"{API}/orgs/{centre.org.id}/unblock", json={})
    centre.owner.platform_blocked_at = utcnow()
    cw.db.commit()
    r = cw.new_client(APP).post("/api/v1/auth/activate", json={"token": token})
    assert r.status_code == 400 and "заблокирован" in r.json()["detail"]


# ── m7: the demo form against floods and tricks ──────────────────────────────────────────────────────────────────────


@needs_pg
def test_the_whole_form_has_an_hourly_cap_and_so_has_every_email_domain(cw):
    c = cw.new_client(APP)
    codes = [c.post("/api/v1/public/demo-requests", json=request_body(email=f"u{i}@dom{i}.test"), headers={"X-Real-IP": f"10.0.{i // 200}.{i % 200 + 1}"}).status_code
             for i in range(62)]
    assert codes[:60] == [201] * 60 and codes[60:] == [429, 429]
    from src.api import rate_limit

    rate_limit._window_store.clear()
    codes = [c.post("/api/v1/public/demo-requests", json=request_body(email=f"u{i}@same-domain.test"), headers={"X-Real-IP": f"10.1.0.{i + 1}"}).status_code
             for i in range(12)]
    assert codes[:10] == [201] * 10 and codes[10:] == [429, 429]


@needs_pg
def test_invisible_format_characters_are_stripped_from_every_field(cw):
    body = request_body(contact_name="Иван‮Иванов​", centre_name="Центр⁦ А", city="Таш‍кент", comment="Хотим‮ тест",
                        source={"utm_source": "tele​gram", "landing": "‮https://x"})
    r = cw.new_client(APP).post("/api/v1/public/demo-requests", json=body)
    assert r.status_code == 201
    row = cw.db.query(DemoRequest).filter_by(email=body["email"]).one()
    for value in (row.contact_name, row.centre_name, row.city, row.comment, *row.source.values()):
        assert not any(__import__("unicodedata").category(ch) == "Cf" for ch in value), value
    assert row.contact_name == "ИванИванов" and row.source["utm_source"] == "telegram"


@needs_pg
def test_urls_in_the_owners_letter_are_plain_text_and_short(cw):
    body = request_body(comment="Смотрите https://evil.example/pay и www.evil.example и evil.com " + "x" * 800,
                        source={"referrer": "https://phish.example/" + "a" * 300, "landing": "http://x.example", "utm_source": "tg"})
    assert cw.new_client(APP).post("/api/v1/public/demo-requests", json=body).status_code == 201
    mail = letters_to(cw.outbox, "owner-notify@example.test")[0]
    text, html = body_of(mail), body_of(mail, "html")
    assert "https://" not in text.replace("https://console.test", "").replace("https://algolab.me", "")
    assert "https[:]//evil[.]example/pay" in text and "www[.]evil[.]example" in text and "evil[.]com" in text
    import re

    hrefs = re.findall(r'href="([^"]+)"', html)
    assert all(h.startswith(("https://console.test", "https://algolab.me", "mailto:", "tel:")) for h in hrefs), hrefs
    assert "…" in text and len(text) < 3500                                         # the long comment and referrer are cut


# ── m9 / m10 / nits ──────────────────────────────────────────────────────────────────────────────────────────────────


@needs_pg
def test_the_cookie_is_a_host_prefixed_name_on_a_secure_stack(cw, monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    staff = cw.make_staff(["orgs.view"])
    c = cw.new_client()
    r = cw.login(c, staff)
    header = [h for h in r.headers.get_list("set-cookie")][0]
    assert header.startswith("__Host-algo_console_session=") and "Secure" in header and "Path=/" in header and "Domain" not in header
    c2 = cw.new_client()
    c2.cookies.set("algo_console_session", c.cookies.get("__Host-algo_console_session") or "x")
    assert c2.get(f"{API}/auth/me").status_code == 401                              # the plain name means nothing on a secure stack
    monkeypatch.setenv("APP_ENV", "testing")
    assert "algo_console_session=" in cw.login(cw.new_client(), staff).headers.get_list("set-cookie")[0]
    assert not cw.login(cw.new_client(), staff).headers.get_list("set-cookie")[0].startswith("__Host-")


@needs_pg
def test_a_failed_password_change_and_a_password_set_by_link_are_journaled(cw):
    r = cw.client.post(f"{API}/auth/change-password", json={"current_password": "not-the-password", "new_password": "a-brand-new-long-password"})
    assert r.status_code == 403
    rows = audit_rows(cw.db, "console.password.change_failed")
    assert len(rows) == 1 and rows[0].actor_user_id == cw.owner.user.id and "not-the-password" not in str(rows[0].details)
    centre = cw.make_centre(members=("owner",))
    token, link = links.issue(cw.db, centre.owner.id, "centre_invite")
    cw.db.commit()
    assert cw.new_client(APP).post(f"/api/v1/auth/password-links/{token}", json={"password": "fine-password-9", "password_confirm": "fine-password-9"}).status_code == 200
    rows = audit_rows(cw.db, "user.password_set_by_link")
    assert len(rows) == 1 and rows[0].actor_user_id is None and rows[0].target_id == str(centre.owner.id) and rows[0].details == {"purpose": "centre_invite"}
    assert "fine-password-9" not in str(rows[0].details) and token not in str(rows[0].details)


@needs_pg
def test_a_password_over_72_bytes_is_refused_with_a_clear_text_everywhere(cw):
    long_ru = "я" * 40                                                              # 40 characters, 80 bytes
    ok_ru = "я" * 36                                                                # 72 bytes: still fine
    msg = "72 байт"
    r = cw.client.post(f"{API}/auth/change-password", json={"current_password": PASSWORD, "new_password": long_ru})
    assert r.status_code == 422 and msg in r.json()["fields"]["new_password"]
    r = cw.client.post(f"{API}/staff", json={"email": f"b-{uuid4().hex[:6]}@cn1.test", "first_name": "А", "last_name": "Б",
                                              "role_id": str(cw.make_staff([]).role.id), "password": long_ru})
    assert r.status_code == 422 and msg in r.json()["fields"]["password"]
    member = cw.make_staff([])
    assert cw.client.post(f"{API}/staff/{member.user.id}/reset-password", json={"password": long_ru}).status_code == 422
    assert cw.client.post(f"{API}/staff/{member.user.id}/reset-password", json={"password": ok_ru}).status_code == 200
    centre = cw.make_centre(members=("owner",))
    token, _ = links.issue(cw.db, centre.owner.id, "reset")
    cw.db.commit()
    r = cw.new_client(APP).post(f"/api/v1/auth/password-links/{token}", json={"password": "я" * 37, "password_confirm": "я" * 37})
    assert r.status_code == 422 and msg in r.json()["fields"]["password"]
    from src.auth.platform_bootstrap import OwnerError, create_owner

    with pytest.raises(OwnerError, match="72 байт"):
        create_owner(cw.db, f"o-{uuid4().hex[:6]}@cn1.test", long_ru)


def test_the_dummy_hash_exists_at_import_and_the_url_check_names_localhost(monkeypatch):
    from src.auth import platform_auth
    from src.mail import config as mail_config

    assert platform_auth._DUMMY_HASH.startswith("$2")
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
    monkeypatch.setenv("CONSOLE_URL", "https://console.algolab.me")
    assert mail_config.localhost_urls() == ["FRONTEND_URL"]
    monkeypatch.setenv("CONSOLE_URL", "http://127.0.0.1:3001")
    assert mail_config.localhost_urls() == ["FRONTEND_URL", "CONSOLE_URL"]
    monkeypatch.setenv("FRONTEND_URL", "https://algolab.me")
    monkeypatch.setenv("CONSOLE_URL", "https://console.algolab.me")
    assert mail_config.localhost_urls() == []
