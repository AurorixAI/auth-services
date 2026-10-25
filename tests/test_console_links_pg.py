"""«Задать пароль» / «Сброс пароля» by link: valid, used, expired, revoked by a newer link, unknown; sessions end after the reset."""
import re
from datetime import timedelta

import pytest

from tests.console_helpers import API, PASSWORD, actions, audit_rows, body_of, cw, letters_to, needs_pg  # noqa: F401
from src.auth import password_links as links  # noqa: E402
from src.auth.platform_common import utcnow  # noqa: E402
from src.models.auth import Organization, User, UserSession  # noqa: E402
from src.models.platform import PasswordLink  # noqa: E402

pytestmark = needs_pg
APP = "app.algolab.me"
LINKS = "/api/v1/auth/password-links"
NEW_PASSWORD = "new-owner-password-1"


def invited_owner(cw):
    """An owner of an approved centre who has not set a password yet, with the token of his link."""
    centre = cw.make_centre(members=("owner",))
    owner = centre.owner
    token, link = links.issue(cw.db, owner.id, "centre_invite", created_by=cw.owner.user.id)
    owner.last_login_at = None
    cw.db.commit()
    return centre, owner, token, link


def test_a_valid_link_shows_who_it_is_for(cw):
    centre, owner, token, link = invited_owner(cw)
    r = cw.new_client(APP).get(f"{LINKS}/{token}")
    assert r.status_code == 200
    j = r.json()
    assert j["valid"] is True and j["purpose"] == "centre_invite" and j["email"] == owner.email
    assert j["first_name"] == owner.first_name and j["org_name"] == centre.org.name and j["expires_at"].endswith("Z")
    assert "password" not in str(j).lower() or "password_hash" not in j


def test_setting_the_password_logs_in_and_burns_the_link(cw):
    centre, owner, token, link = invited_owner(cw)
    c = cw.new_client(APP)
    r = c.post(f"{LINKS}/{token}", json={"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["user"]["email"] == owner.email and j["tokens"]["access_token"] and j["org"]["name"] == centre.org.name
    assert c.cookies.get("algo_auth_token") and c.get("/api/v1/auth/me").status_code == 200       # signed in through the cookie
    assert cw.new_client(APP).post("/api/v1/auth/login", json={"email": owner.email, "password": NEW_PASSWORD}).status_code == 200
    # once only
    again = c.post(f"{LINKS}/{token}", json={"password": "another-password-2", "password_confirm": "another-password-2"})
    assert again.status_code == 410 and again.json()["code"] == "link_used" and "уже использована" in again.json()["detail"]
    assert cw.new_client(APP).get(f"{LINKS}/{token}").json()["code"] == "link_used"
    assert cw.new_client(APP).post("/api/v1/auth/login", json={"email": owner.email, "password": "another-password-2"}).status_code == 401
    cw.db.expire_all()
    assert cw.db.get(PasswordLink, link.id).used_at is not None and cw.db.get(User, owner.id).is_verified is True


def test_an_expired_link_says_so_and_changes_nothing(cw):
    centre, owner, token, link = invited_owner(cw)
    link.expires_at = utcnow() - timedelta(seconds=1)
    cw.db.commit()
    c = cw.new_client(APP)
    for r in (c.get(f"{LINKS}/{token}"), c.post(f"{LINKS}/{token}", json={"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD})):
        assert r.status_code == 410 and r.json()["code"] == "link_expired" and "истёк" in r.json()["detail"] and "новую" in r.json()["detail"]
    assert c.post("/api/v1/auth/login", json={"email": owner.email, "password": NEW_PASSWORD}).status_code == 401


@pytest.mark.parametrize("token", ["garbage", "x" * 43, "a" * 500, "%00", "a b"])
def test_an_unknown_or_foreign_link_is_404(cw, token):
    c = cw.new_client(APP)
    assert c.get(f"{LINKS}/{token}").status_code in (404,)
    r = c.post(f"{LINKS}/{token}", json={"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD})
    assert r.status_code == 404 and r.json()["code"] == "link_not_found"


def test_a_new_link_revokes_the_old_one(cw):
    centre, owner, old_token, old = invited_owner(cw)
    new_token, new = links.issue(cw.db, owner.id, "reset", created_by=None)
    cw.db.commit()
    c = cw.new_client(APP)
    assert c.get(f"{LINKS}/{old_token}").json()["code"] == "link_revoked"
    assert c.post(f"{LINKS}/{old_token}", json={"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD}).status_code == 410
    assert c.get(f"{LINKS}/{new_token}").json()["purpose"] == "reset"
    assert c.post(f"{LINKS}/{new_token}", json={"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD}).status_code == 200


def test_password_rules_are_in_russian_and_the_link_stays_valid(cw):
    centre, owner, token, link = invited_owner(cw)
    c = cw.new_client(APP)
    r = c.post(f"{LINKS}/{token}", json={"password": "short", "password_confirm": "short"})
    assert r.status_code == 422 and "8" in r.json()["fields"]["password"]
    r = c.post(f"{LINKS}/{token}", json={"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD + "x"})
    assert r.status_code == 422 and r.json()["fields"]["password_confirm"] == "Пароли не совпадают"
    assert c.post(f"{LINKS}/{token}", json={}).status_code == 422
    assert c.post(f"{LINKS}/{token}", json={"password": "bad\x00pass-word", "password_confirm": "bad\x00pass-word"}).status_code == 422
    assert c.get(f"{LINKS}/{token}").status_code == 200                                      # nothing was burned


def test_a_blocked_centre_cannot_get_in_through_a_link(cw):
    centre, owner, token, link = invited_owner(cw)
    centre.org.blocked_at = utcnow()
    cw.db.commit()
    r = cw.new_client(APP).post(f"{LINKS}/{token}", json={"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD})
    assert r.status_code == 403 and r.json()["code"] == "org_blocked" and r.json()["detail"] == "Доступ центра приостановлен. Свяжитесь с ALGO"
    cw.db.expire_all()
    assert cw.db.get(PasswordLink, link.id).used_at is None


def test_the_links_of_the_console_api_are_rate_limited(cw):
    c = cw.new_client(APP)
    codes = [c.get(f"{LINKS}/nope-{i}").status_code for i in range(62)]
    assert codes[:60] == [404] * 60 and set(codes[60:]) == {429}


def test_reset_by_the_console_ends_the_old_sessions_after_the_password_changes(cw):
    centre = cw.make_centre(members=("owner",))
    owner = centre.owner
    cabinet = cw.new_client(APP)
    login = cabinet.post("/api/v1/auth/login", json={"email": owner.email, "password": PASSWORD})
    assert login.status_code == 200
    refresh = login.json()["tokens"]["refresh_token"]

    support = cw.signed_in(cw.make_staff(["users.view", "users.manage"]))
    r = support.post(f"{API}/users/{owner.id}/send-reset")
    assert r.status_code == 200 and r.json()["email_sent"] is True
    mail = letters_to(cw.outbox, owner.email)[-1]
    assert mail["Subject"] == "Сброс пароля ALGO"
    token = re.search(r"https://app\.test/set-password/([\w-]+)", body_of(mail)).group(1)
    assert "1 час" in body_of(mail)
    row = cw.db.query(PasswordLink).filter_by(user_id=owner.id, purpose="reset").one()
    assert 0.99 * 3600 < (row.expires_at - row.created_at).total_seconds() < 1.01 * 3600

    # until the password is changed, the old session lives
    assert cabinet.get("/api/v1/auth/me").status_code == 200
    done = cw.new_client(APP).post(f"{LINKS}/{token}", json={"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD})
    assert done.status_code == 200
    stale = cw.new_client(APP)
    assert stale.post("/api/v1/auth/refresh", json={"refresh_token": refresh}).status_code == 401      # the old device is signed out
    cw.db.expire_all()
    assert cw.db.query(UserSession).filter(UserSession.user_id == owner.id, UserSession.revoke_reason == "password_changed").count() >= 1
    assert cw.new_client(APP).post("/api/v1/auth/login", json={"email": owner.email, "password": PASSWORD}).status_code == 401
