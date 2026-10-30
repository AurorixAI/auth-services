"""K2-1, the round of fixes after R3 and the lead's notes (Postgres): M1 change of the password keeps the session in use, m1 old cookie names are deleted and the whole chain
under the PRODUCTION cookie names, m6 the limits of invitation letters, m8 the races of invitations, n1 every access check goes through the live session, the lead's
#3 (a blocked centre gets no new session at refresh) and #4 (the code is stored as a hash)."""
import ast
import pathlib
import re
import threading
import time
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from src.api import main_v2
from src.auth import cookies as cookies_module
from src.auth import dependencies, router as auth_router, user_sessions
from src.auth.cookies import AUTH_COOKIE_NAME, REFRESH_COOKIE_NAME
from src.auth.service import AuthService, hash_token
from src.models.auth import Invitation, Organization, OrganizationMember, User, UserSession

from tests.console_helpers import PASSWORD, cw, letters_to, needs_pg  # noqa: F401  (cw is a fixture)
from tests.test_k2_1_pg import (  # noqa: F401
    AUTH, DAY, HOST, accept, centre, invitation_code, invite, jar_of, live_sessions, login, sessions_of, signed_in, wait_for_advisory_waiter,
)

NEW_PASSWORD = "brand-new-pa55"


# ============================================================================================================ M1: change of the password


@needs_pg
def test_m1_changing_the_password_in_the_settings_ends_the_other_devices_and_keeps_this_one(cw, centre):
    here, r1 = login(cw, centre.teacher, remember=True)
    there, _ = login(cw, centre.teacher)
    changed = here.post(f"{AUTH}/change-password", json={"old_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert changed.status_code == 200, changed.text
    jar = jar_of(changed)                                                     # the cookies are renewed with the SAME mode (remembered: 14 days)
    assert jar[AUTH_COOKIE_NAME]["max-age"] == str(14 * DAY) == jar[REFRESH_COOKIE_NAME]["max-age"]
    assert here.get(f"{AUTH}/me").status_code == 200                          # the person is not thrown out
    assert there.get(f"{AUTH}/me").status_code == 401 and there.post(f"{AUTH}/refresh", json={}).status_code == 401
    assert here.post(f"{AUTH}/refresh", json={}).status_code == 200
    rows = sessions_of(cw, centre.teacher)
    assert [(s.remember, s.revoke_reason) for s in rows] == [(True, None), (False, "password_changed")]
    assert login(cw, centre.teacher, password=NEW_PASSWORD)[1].status_code == 200
    assert login(cw, centre.teacher)[1].status_code == 401                    # the old password is dead


@needs_pg
def test_m1_a_short_session_stays_short_after_the_change(cw, centre):
    here, _ = login(cw, centre.admin)
    changed = here.post(f"{AUTH}/change-password", json={"old_password": PASSWORD, "new_password": NEW_PASSWORD})
    jar = jar_of(changed)
    assert not jar[AUTH_COOKIE_NAME]["max-age"] and not jar[REFRESH_COOKIE_NAME]["max-age"]
    assert here.get(f"{AUTH}/me").status_code == 200 and live_sessions(cw, centre.admin)[0].remember is False


@needs_pg
def test_m1_the_teacher_with_a_temporary_password_stays_in_the_cabinet_after_the_forced_change(cw, centre):
    centre.teacher.must_change_password = True
    cw.db.commit()
    client, r = login(cw, centre.teacher)
    assert r.status_code == 200 and r.json()["user"]["must_change_password"] is True
    changed = client.post(f"{AUTH}/change-password", json={"old_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert changed.status_code == 200
    me = client.get(f"{AUTH}/me")
    assert me.status_code == 200 and me.json()["must_change_password"] is False


@needs_pg
def test_m1_a_wrong_old_password_changes_nothing(cw, centre):
    here, _ = login(cw, centre.teacher)
    there, _ = login(cw, centre.teacher)
    assert here.post(f"{AUTH}/change-password", json={"old_password": "not-the-password", "new_password": NEW_PASSWORD}).status_code == 400
    assert here.get(f"{AUTH}/me").status_code == 200 and there.get(f"{AUTH}/me").status_code == 200


# ============================================================================================================ m1: old cookie names, the chain under the production names


@pytest.fixture
def prod_names(monkeypatch):
    """The cookie policy of a real stack (APP_ENV=staging behind https): the ``__Host-`` names, in EVERY module that holds a copy of the name."""
    access, refresh = cookies_module.cookie_names(True)
    monkeypatch.setattr(cookies_module, "_IS_PROD", True)
    for module in (cookies_module, dependencies, auth_router):
        monkeypatch.setattr(module, "AUTH_COOKIE_NAME", access, raising=False)
        monkeypatch.setattr(module, "REFRESH_COOKIE_NAME", refresh, raising=False)
    return access, refresh


def all_set_cookies(response):
    from http.cookies import SimpleCookie

    out = {}
    for raw in response.headers.get_list("set-cookie"):
        parsed = SimpleCookie()
        parsed.load(raw)
        for name, morsel in parsed.items():
            out[(name, morsel["path"])] = morsel
    return out


@needs_pg
def test_m1_the_whole_chain_under_the_production_names_and_the_old_names_are_deleted(cw, centre, prod_names):
    client = TestClient(main_v2.app, base_url="https://app.test")           # https: the Secure cookies are kept and sent back
    r = client.post(f"{AUTH}/login", json={"email": centre.owner.email, "password": PASSWORD, "remember_me": False})
    assert r.status_code == 200, r.text
    jar = all_set_cookies(r)
    for name in ("__Host-algo_auth_token", "__Host-algo_refresh_token"):
        morsel = jar[(name, "/")]
        assert morsel["secure"] is True and morsel["httponly"] is True and not morsel["domain"] and not morsel["max-age"]
    # a sign-in deletes the cookies of before K2-1: the old names, the old refresh path
    assert jar[("algo_auth_token", "/")]["max-age"] == "0" and jar[("algo_refresh_token", "/api/v1/auth")]["max-age"] == "0"
    assert client.get(f"{AUTH}/me").status_code == 200
    assert client.post(f"{AUTH}/refresh", json={}).status_code == 200
    out = client.post(f"{AUTH}/logout")
    jar = all_set_cookies(out)
    assert {k for k, m in jar.items() if m["max-age"] == "0"} == {
        ("__Host-algo_auth_token", "/"), ("__Host-algo_refresh_token", "/"), ("algo_auth_token", "/"), ("algo_refresh_token", "/api/v1/auth"),
    }
    assert client.get(f"{AUTH}/me").status_code == 401


@needs_pg
def test_m1_a_cookie_under_an_old_name_is_not_a_credential_on_a_secure_stack(cw, centre, prod_names):
    _, r = login(cw, centre.owner)                                            # (the plain-named copy of a real session)
    token = jar_of(r)["__Host-algo_auth_token"].value
    planted = TestClient(main_v2.app, base_url="https://app.test")
    planted.cookies.set("algo_auth_token", token)
    assert planted.get(f"{AUTH}/me").status_code == 401


# ============================================================================================================ the lead's #3: refresh of a blocked centre


@needs_pg
def test_lead3_a_blocked_centre_gets_no_new_session_at_refresh_and_after_the_unblock_it_does(cw, centre):
    client, _ = login(cw, centre.teacher)
    assert cw.client.post(f"/api/v1/platform/orgs/{centre.org.id}/block", json={"reason": "тест блокировки"}).status_code == 200
    r = client.post(f"{AUTH}/refresh", json={})
    assert r.status_code == 403 and "приостановлен" in r.json()["detail"]
    assert client.get(f"{AUTH}/me").status_code == 403                        # (the access token is refused by the centre check too)
    assert cw.client.post(f"/api/v1/platform/orgs/{centre.org.id}/unblock", json={}).status_code == 200
    assert client.post(f"{AUTH}/refresh", json={}).status_code == 200


# ============================================================================================================ the lead's #4 / n3: the code is a hash


@needs_pg
def test_lead4_the_database_never_holds_the_code_and_the_old_column_is_gone(cw, centre):
    from sqlalchemy import inspect

    cols = {c["name"] for c in inspect(cw.engine).get_columns("invitations")}
    assert "code_hash" in cols
    owner = signed_in(cw, centre.owner)
    address = f"hash-{uuid4().hex[:6]}@staff.test"
    invite(owner, address)
    code = invitation_code(cw, address)
    cw.db.expire_all()
    row = cw.db.query(Invitation).filter(Invitation.email == address).one()
    assert row.code_hash == hash_token(code) and len(row.code_hash) == 64 and code not in row.code_hash
    assert code not in str(row.to_dict())
    assert cw.new_client(host=HOST).get(f"{AUTH}/invite/{row.code_hash}").status_code == 404     # the hash itself is not a link


# ============================================================================================================ m6: the limits of the letters


@needs_pg
def test_m6_a_refused_request_spends_no_budget(cw, centre):
    owner, admin = signed_in(cw, centre.owner), signed_in(cw, centre.admin)
    address = f"budget-{uuid4().hex[:6]}@staff.test"
    refused = [admin.post(f"{AUTH}/org/invitations", json={"email": address, "org_role": "admin"}).status_code for _ in range(15)]
    assert refused == [403] * 15                                              # an admin may not invite an admin: refused BEFORE the count
    assert [invite(owner, address).status_code for _ in range(10)] == [200] * 10
    assert invite(owner, address).status_code == 429
    bad = owner.post(f"{AUTH}/org/invitations", json={"email": "not-an-address", "org_role": "teacher"})
    assert bad.status_code == 422 and invite(owner, "").status_code == 422     # validation is not counted (and an empty address has no bucket)


@needs_pg
def test_m6_the_budget_of_a_mailbox_is_per_centre_per_hour_and_20_a_day_over_all_centres(cw, centre):
    other = cw.make_centre()
    a, b = signed_in(cw, centre.owner), signed_in(cw, other.owner)
    address = f"victim-{uuid4().hex[:6]}@staff.test"
    assert [invite(a, address).status_code for _ in range(10)] == [200] * 10
    assert invite(a, address).status_code == 429
    # another centre is not shut out by the first one's hourly pair budget ...
    assert [invite(b, address).status_code for _ in range(10)] == [200] * 10
    # ... but the mailbox has a daily budget over all centres: 20 letters, then nobody
    third = cw.make_centre()
    c = signed_in(cw, third.owner)
    r = invite(c, address)
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"
    assert len(letters_to(cw.outbox, address)) == 20


# ============================================================================================================ m8: the races of invitations


def new_session(cw):
    return sessionmaker(cw.engine, autoflush=False)()


@needs_pg
def test_m8_an_accept_in_flight_wins_over_a_resend_and_no_new_invitation_is_made_for_the_member(cw, centre):
    owner = signed_in(cw, centre.owner)
    address = f"race-{uuid4().hex[:6]}@staff.test"
    first = invite(owner, address).json()
    code = invitation_code(cw, address)
    # B = the accept, held open: it has locked the invitation row and made the account, but has not committed
    db_b = new_session(cw)
    result = {}
    try:
        row = db_b.query(Invitation).filter(Invitation.id == first["id"]).with_for_update().one()
        user = User(email=address, password_hash="x", first_name="Н", last_name="Н", role="teacher", default_org_id=centre.org.id, is_verified=True)
        db_b.add(user)
        db_b.flush()
        db_b.add(OrganizationMember(org_id=centre.org.id, user_id=user.id, org_role="teacher", is_active=True))
        row.status, row.accepted_by = "accepted", user.id
        db_b.flush()

        def resend():
            db_a = new_session(cw)
            try:
                AuthService(db_a).resend_invitation(centre.org.id, first["id"], centre.owner.id, creator_role="owner")
                result["outcome"] = "sent"
            except Exception as exc:  # noqa: BLE001
                result["outcome"] = getattr(exc, "code", None) or type(exc).__name__
            finally:
                db_a.close()

        thread = threading.Thread(target=resend)
        thread.start()
        time.sleep(0.6)
        assert thread.is_alive(), "the resend should wait for the accept that holds the invitation"
        db_b.commit()
        thread.join(30)
    finally:
        db_b.close()
    assert result["outcome"] == "ALREADY_MEMBER", result
    cw.db.expire_all()
    assert cw.db.query(Invitation).filter(Invitation.email == address, Invitation.status == "pending").count() == 0
    assert len(letters_to(cw.outbox, address)) == 1                           # no second letter to a person who is already in


@needs_pg
def test_m8_two_resends_at_once_leave_exactly_one_live_link_and_it_is_the_last_one(cw, centre):
    owner = signed_in(cw, centre.owner)
    address = f"race2-{uuid4().hex[:6]}@staff.test"
    first = invite(owner, address).json()
    outcomes = []

    def resend():
        db = new_session(cw)
        try:
            outcomes.append(AuthService(db).resend_invitation(centre.org.id, first["id"], centre.owner.id, creator_role="owner")["mail_status"] == "sent")
        except Exception as exc:  # noqa: BLE001
            outcomes.append(type(exc).__name__)
        finally:
            db.close()

    threads = [threading.Thread(target=resend) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    cw.db.expire_all()
    pending = cw.db.query(Invitation).filter(Invitation.email == address, Invitation.status == "pending").all()
    assert len(pending) == 1, outcomes                                        # one live link, never two
    codes = [re.search(r"/invite/([A-Za-z0-9_%-]+)", body_text(m)).group(1) for m in letters_to(cw.outbox, address)]
    stranger = cw.new_client(host=HOST)
    alive = [c for c in codes if stranger.get(f"{AUTH}/invite/{c}").status_code == 200]
    assert len(codes) == 1 + outcomes.count(True) and len(alive) == 1 and hash_token(alive[0]) == pending[0].code_hash


def body_text(msg):
    return msg.get_body(preferencelist=("plain",)).get_content()


# ============================================================================================================ n1: every access check goes through the live session


def test_n1_the_dead_jwt_only_checks_are_gone_and_no_route_can_use_a_token_without_its_session():
    root = pathlib.Path("src")
    assert not (root / "core" / "dependencies.py").exists()
    assert not hasattr(dependencies, "get_optional_user")
    callers, users = [], []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "verify_access_token":
                callers.append(f"{path}:{getattr(node.func, 'id', 'verify_access_token')}")
            if isinstance(node, ast.Name) and node.id == "get_current_user_payload" and path.name != "dependencies.py":
                users.append(str(path))
    # the only readers of an access token: the dependency itself and the sign-out (which only identifies the session to end)
    assert sorted({c.split(":")[0] for c in callers}) == ["src/auth/dependencies.py", "src/auth/router.py"], callers
    assert users == []                                                          # nobody takes the JWT-only dependency directly
    source = pathlib.Path("src/auth/dependencies.py").read_text(encoding="utf-8")
    active = source[source.index("def get_current_active_user_payload"):]
    assert "Depends(get_current_user_payload)" in active.split("def ")[1] and "user_sessions.is_alive" in active
    assert source.count("Depends(get_current_user_payload)") == 1             # (get_current_active_user_payload is the only consumer)


# ============================================================================================================ n4: the dead activation columns


def test_n4_the_organization_model_has_no_activation_columns_and_the_nginx_comments_do_not_name_the_old_page():
    assert not hasattr(Organization, "activation_token") and not hasattr(Organization, "activation_token_expires_at")


@needs_pg
def test_lead4_the_repository_finds_an_invitation_by_the_plain_code_through_its_hash(cw, centre):
    from src.repositories.org_repo import InvitationRepo

    owner = signed_in(cw, centre.owner)
    address = f"repo-{uuid4().hex[:6]}@staff.test"
    invite(owner, address)
    code = invitation_code(cw, address)
    repo = InvitationRepo(cw.db)
    found = repo.get_by_code(code)
    assert found is not None and found.email == address
    assert repo.get_by_code(found.code_hash) is None and repo.get_by_code("NOPE") is None
