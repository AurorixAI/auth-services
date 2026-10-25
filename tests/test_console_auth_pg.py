"""Console sign-in: session cookie, token audiences, the console host, the 5-failure lock, forced password change, journal rows."""
from datetime import timedelta
from http.cookies import SimpleCookie

import pytest
from sqlalchemy import text

from tests.console_helpers import (  # noqa: F401  (first: it loads the application, see the import cycle note there)
    API, CONSOLE_HOST, PASSWORD, actions, advance_clock, audit_rows, cw, needs_pg,
)
from src.auth.platform_common import utcnow  # noqa: E402
from src.auth.service import create_access_token  # noqa: E402
from src.models.platform import PlatformSession, PlatformStaff  # noqa: E402

pytestmark = needs_pg
COOKIE = "algo_console_session"


def morsel(response, name=COOKIE):
    jar = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        jar.load(header)
    return jar[name] if name in jar else None


def test_login_gives_a_12_hour_host_only_strict_httponly_cookie(cw, monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")                                           # Secure on the real stacks
    staff = cw.make_staff(["requests.view"])
    r = cw.login(cw.new_client(), staff)
    assert r.status_code == 200
    m = morsel(r)
    assert m is not None and int(m["max-age"]) == 12 * 3600
    assert m["httponly"] and m["samesite"].lower() == "strict" and m["secure"] and m["path"] == "/" and not m["domain"]
    body = r.json()
    assert body["user"]["email"] == staff.email and body["is_owner"] is False and body["permissions"] == ["requests.view"]
    assert body["must_change_password"] is False and body["totp_enabled"] is False and body["role"]["name"] == staff.role.name
    row = cw.db.query(PlatformSession).filter_by(user_id=staff.user.id).one()
    assert (row.expires_at - row.created_at).total_seconds() == 12 * 3600 and row.revoked_at is None


def test_me_logout_and_the_session_row(cw):
    me = cw.client.get(f"{API}/auth/me")
    assert me.status_code == 200 and me.json()["is_owner"] is True and "team.manage" in me.json()["permissions"]
    r = cw.client.post(f"{API}/auth/logout")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert cw.client.get(f"{API}/auth/me").status_code == 401                          # the cookie is gone ...
    stale = cw.new_client()
    stale.cookies.set(COOKIE, "garbage")
    assert stale.get(f"{API}/auth/me").json()["code"] == "session_expired"
    cw.db.expire_all()
    assert cw.db.query(PlatformSession).filter_by(user_id=cw.owner.user.id).one().revoke_reason == "logout"


def test_a_revoked_session_does_not_come_back_with_the_old_cookie(cw):
    token = cw.client.cookies.get(COOKIE)
    cw.client.post(f"{API}/auth/logout")
    other = cw.new_client()
    other.cookies.set(COOKIE, token)
    r = other.get(f"{API}/auth/me")
    assert r.status_code == 401 and r.json()["code"] == "session_expired"


def test_an_expired_session_is_401(cw):
    cw.db.query(PlatformSession).update({"expires_at": utcnow() - timedelta(seconds=1)})
    cw.db.commit()
    r = cw.client.get(f"{API}/auth/me")
    assert r.status_code == 401 and r.json()["code"] == "session_expired" and "Войдите" in r.json()["detail"]


def test_no_cookie_is_401_not_authenticated(cw):
    r = cw.new_client().get(f"{API}/orgs")
    assert r.status_code == 401 and r.json()["code"] == "not_authenticated"


def test_logout_all_ends_every_session_of_the_staff_member(cw):
    second = cw.signed_in(cw.owner)
    r = cw.client.post(f"{API}/auth/logout-all")
    assert r.status_code == 200 and r.json()["revoked"] == 2
    assert second.get(f"{API}/auth/me").status_code == 401 and cw.client.get(f"{API}/auth/me").status_code == 401
    assert "console.logout_all" in actions(cw.db)


def test_wrong_password_and_unknown_email_are_the_same_401(cw):
    c = cw.new_client()
    a = c.post(f"{API}/auth/login", json={"email": cw.owner.email, "password": "wrong-wrong-wrong"})
    b = c.post(f"{API}/auth/login", json={"email": "nobody@cn1.test", "password": "wrong-wrong-wrong"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()
    assert a.json() == {"detail": "Неверная почта или пароль", "code": "invalid_credentials"}


def test_five_failures_lock_the_email_for_15_minutes_even_for_the_right_password(cw, monkeypatch):
    staff = cw.make_staff(["orgs.view"])
    c = cw.new_client()
    codes = [c.post(f"{API}/auth/login", json={"email": staff.email, "password": "bad-bad-bad-bad"}).status_code for _ in range(4)]
    assert codes == [401] * 4
    fifth = c.post(f"{API}/auth/login", json={"email": staff.email, "password": "bad-bad-bad-bad"})
    assert fifth.status_code == 429 and fifth.json()["code"] == "login_locked" and int(fifth.headers["Retry-After"]) >= 890
    right = cw.login(c, staff)
    assert right.status_code == 429 and right.json()["code"] == "login_locked"
    other_ip = c.post(f"{API}/auth/login", json={"email": staff.email, "password": PASSWORD}, headers={"X-Real-IP": "198.51.100.9"})
    assert other_ip.status_code == 429                                                  # the lock is by e-mail, not only by address
    cw.db.expire_all()
    assert cw.db.query(PlatformStaff).filter_by(user_id=staff.user.id).one().locked_until is not None
    advance_clock(monkeypatch, 15 * 60 + 5)
    assert cw.login(c, staff).status_code == 200                                         # after 15 minutes the right password works


def test_a_success_before_the_fifth_failure_resets_the_count(cw):
    staff = cw.make_staff(["orgs.view"])
    c = cw.new_client()
    for _ in range(4):
        c.post(f"{API}/auth/login", json={"email": staff.email, "password": "bad-bad-bad-bad"})
    assert cw.login(c, staff).status_code == 200
    # the e-mail count starts again (the address count does not: a stolen account must not reset it), so use another address
    for _ in range(4):
        r = c.post(f"{API}/auth/login", json={"email": staff.email, "password": "bad-bad-bad-bad"}, headers={"X-Real-IP": "198.51.100.77"})
        assert r.status_code == 401


def test_five_failures_from_one_address_lock_that_address_not_another(cw):
    c = cw.new_client()
    hdr = {"X-Real-IP": "203.0.113.50"}
    codes = [c.post(f"{API}/auth/login", json={"email": f"x{i}@cn1.test", "password": "bad-bad-bad-bad"}, headers=hdr).status_code for i in range(5)]
    assert codes == [401, 401, 401, 401, 429]
    mine = c.post(f"{API}/auth/login", json={"email": cw.owner.email, "password": PASSWORD}, headers=hdr)
    assert mine.status_code == 429                                                       # the right password from a locked address
    assert c.post(f"{API}/auth/login", json={"email": cw.owner.email, "password": PASSWORD}, headers={"X-Real-IP": "203.0.113.51"}).status_code == 200


def test_every_attempt_is_one_journal_row_and_never_holds_the_password(cw):
    staff = cw.make_staff([])
    c = cw.new_client()
    c.post(f"{API}/auth/login", json={"email": staff.email, "password": "wrong-wrong-wrong"})
    cw.login(c, staff)
    c.post(f"{API}/auth/login", json={"email": "ghost@cn1.test", "password": "wrong-wrong-wrong"})
    rows = [r for r in audit_rows(cw.db) if r.target_type == "console" and r.action.startswith("console.login")]
    got = [(r.action, str(r.actor_user_id) if r.actor_user_id else None, r.details["email"]) for r in rows]
    assert got[-3:] == [
        ("console.login.failed", str(staff.user.id), staff.email), ("console.login.success", str(staff.user.id), staff.email),
        ("console.login.failed", None, "ghost@cn1.test"),
    ]
    blob = " ".join(str(r.details) + str(r.reason) for r in audit_rows(cw.db))
    assert "wrong-wrong-wrong" not in blob and PASSWORD not in blob and rows[-1].ip is not None


def test_a_lock_is_journaled_as_locked(cw):
    staff = cw.make_staff([])
    c = cw.new_client()
    for _ in range(5):
        c.post(f"{API}/auth/login", json={"email": staff.email, "password": "bad-bad-bad-bad"})
    c.post(f"{API}/auth/login", json={"email": staff.email, "password": PASSWORD})
    assert [r.action for r in audit_rows(cw.db, target_id=str(staff.user.id))].count("console.login.failed") == 5
    assert "console.login.locked" in actions(cw.db)


def test_a_cabinet_user_is_403_not_staff_and_a_disabled_staff_member_is_403(cw):
    centre = cw.make_centre()
    r = cw.login(cw.new_client(), centre.owner, centre.password)
    assert r.status_code == 403 and r.json()["code"] == "not_staff" and r.json()["detail"] == "Нет доступа к консоли"
    off = cw.make_staff(["orgs.view"], active=False)
    r = cw.login(cw.new_client(), off)
    assert r.status_code == 403 and r.json()["code"] == "staff_inactive"
    assert [a for a in actions(cw.db) if a == "console.login.forbidden"] == ["console.login.forbidden"] * 2


def test_a_staff_member_switched_off_while_signed_in_is_cut_off_at_once(cw):
    staff = cw.make_staff(["orgs.view"])
    c = cw.signed_in(staff)
    assert c.get(f"{API}/orgs").status_code == 200
    cw.db.query(PlatformStaff).filter_by(user_id=staff.user.id).update({"is_active": False})
    cw.db.commit()
    r = c.get(f"{API}/orgs")
    assert r.status_code == 403 and r.json()["code"] == "staff_inactive"


# ── audiences ────────────────────────────────────────────────────────────────────────────────


def test_a_cabinet_token_does_not_open_the_console(cw):
    centre = cw.make_centre()
    app_token = create_access_token(str(centre.owner.id), "owner", str(centre.org.id), "owner")
    c = cw.new_client()
    c.cookies.set(COOKIE, app_token)
    r = c.get(f"{API}/orgs")
    assert r.status_code == 401
    r = cw.new_client().get(f"{API}/orgs", headers={"Authorization": f"Bearer {app_token}"})
    assert r.status_code == 401 and r.json()["code"] == "not_authenticated"
    app_token_of_staff = create_access_token(str(cw.owner.user.id), "teacher", None, None)
    c2 = cw.new_client()
    c2.cookies.set(COOKIE, app_token_of_staff)
    assert c2.get(f"{API}/auth/me").status_code == 401


def test_a_console_token_does_not_open_the_cabinets(cw):
    console_token = cw.client.cookies.get(COOKIE)
    app = cw.new_client("app.algolab.me")
    app.cookies.set("algo_auth_token", console_token)
    assert app.get("/api/v1/auth/me").status_code == 401
    assert app.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {console_token}"}).status_code == 401


def test_a_console_account_cannot_sign_in_to_the_cabinets(cw):
    app = cw.new_client("app.algolab.me")
    r = app.post("/api/v1/auth/login", json={"email": cw.owner.email, "password": PASSWORD})
    assert r.status_code == 401


# ── host ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["algolab.me", "app.algolab.me", "stage.algolab.me", "console.algolab.me.evil.test", "evil.test"])
def test_the_platform_api_is_404_from_any_other_host(cw, host):
    token = cw.client.cookies.get(COOKIE)
    other = cw.new_client(host)
    other.cookies.set(COOKIE, token)                                                     # even with a valid session
    for path in ("/auth/me", "/orgs", "/demo-requests"):
        r = other.get(f"{API}{path}")
        assert r.status_code == 404, path
    assert other.post(f"{API}/auth/login", json={"email": cw.owner.email, "password": PASSWORD}).status_code == 404


def test_the_forwarded_host_must_be_a_console_host_too(cw):
    token = cw.client.cookies.get(COOKIE)
    assert cw.client.get(f"{API}/auth/me", headers={"X-Forwarded-Host": "console.stage.algolab.me"}).status_code == 200
    assert cw.client.get(f"{API}/auth/me", headers={"X-Forwarded-Host": "app.algolab.me"}).status_code == 404
    other = cw.new_client("app.algolab.me")
    other.cookies.set(COOKIE, token)
    assert other.get(f"{API}/auth/me", headers={"X-Forwarded-Host": CONSOLE_HOST}).status_code == 404   # a spoofed header does not open it


def test_console_hosts_come_from_the_environment(cw, monkeypatch):
    monkeypatch.setenv("CONSOLE_HOSTS", "ops.example.test")
    assert cw.client.get(f"{API}/auth/me").status_code == 404
    other = cw.new_client("ops.example.test")
    other.cookies.set(COOKIE, cw.client.cookies.get(COOKIE))
    assert other.get(f"{API}/auth/me").status_code == 200


def test_a_write_from_a_foreign_origin_is_refused(cw):
    r = cw.client.post(f"{API}/auth/logout-all", headers={"Origin": "https://evil.test"})
    assert r.status_code == 403 and r.json()["code"] == "bad_origin"
    assert cw.client.post(f"{API}/auth/logout-all", headers={"Origin": f"https://{CONSOLE_HOST}"}).status_code == 200


# ── forced password change ───────────────────────────────────────────────────────────────────


def test_must_change_password_opens_only_the_password_change(cw):
    staff = cw.make_staff(["orgs.view", "audit.view"], must_change=True)
    c = cw.signed_in(staff)
    me = c.get(f"{API}/auth/me")
    assert me.status_code == 200 and me.json()["must_change_password"] is True
    for path in ("/orgs", "/audit", "/demo-requests", "/permissions"):
        r = c.get(f"{API}{path}")
        assert r.status_code == 403 and r.json()["code"] == "must_change_password", path
    new = "a-brand-new-long-password"
    assert c.post(f"{API}/auth/change-password", json={"current_password": PASSWORD, "new_password": new}).json() == {"ok": True}
    assert c.get(f"{API}/orgs").status_code == 200
    assert cw.login(cw.new_client(), staff, new).status_code == 200
    assert cw.login(cw.new_client(), staff).status_code == 401


def test_change_password_rules(cw):
    url = f"{API}/auth/change-password"
    r = cw.client.post(url, json={"current_password": "not-the-password", "new_password": "a-brand-new-long-password"})
    assert r.status_code == 403 and r.json()["code"] == "wrong_password"
    r = cw.client.post(url, json={"current_password": PASSWORD, "new_password": "short"})
    assert r.status_code == 422 and "12" in r.json()["fields"]["new_password"]
    r = cw.client.post(url, json={"current_password": PASSWORD, "new_password": PASSWORD})
    assert r.status_code == 422 and r.json()["fields"]["new_password"]
    assert cw.client.post(url, json={"current_password": PASSWORD}).status_code == 422


def test_change_password_ends_the_other_sessions_and_keeps_this_one_and_is_journaled(cw):
    second = cw.signed_in(cw.owner)
    new = "another-very-long-password"
    assert cw.client.post(f"{API}/auth/change-password", json={"current_password": PASSWORD, "new_password": new}).status_code == 200
    assert second.get(f"{API}/auth/me").status_code == 401 and cw.client.get(f"{API}/auth/me").status_code == 200
    rows = audit_rows(cw.db, "console.password.changed")
    assert len(rows) == 1 and rows[0].actor_user_id == cw.owner.user.id
    assert new not in str(rows[0].details) and PASSWORD not in str(rows[0].details)


def test_the_perimeter_does_not_answer_with_a_stack_trace(cw):
    r = cw.client.post(f"{API}/auth/login", content=b"{", headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
