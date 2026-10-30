"""K2-1 (access and letters) on the real application and the disposable ``a2_auth`` PostgreSQL.

A1 invitation by letter only (R10, R12, S4, F10, T11), A2 no token in any answer (S3), A3 ``__Host-`` cookies and the sign-in that ends the
browser's previous session, A4 superadmin (S1), A5 the access token dies with its session (U9, S2, invariant 4), D «Запомнить меня» (K1, K2, K4-K8,
invariant 11), F8 (sign-in || password set).
"""
import json
import logging
import re
import threading
import time
from datetime import timedelta
from http.cookies import SimpleCookie
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from src.api import main_v2
from src.auth import cookies as cookies_module
from src.auth import dependencies, password_links as links, user_sessions
from src.auth.cookies import AUTH_COOKIE_NAME, REFRESH_COOKIE_NAME
from src.auth.schemas import LoginRequest
from src.auth.service import AuthService, create_access_token, hash_password, hash_token
from src.core.exceptions import InvalidCredentialsError
from src.models.auth import Invitation, OrganizationMember, User, UserSession
from src.models.platform import PasswordLink

from tests.console_helpers import PASSWORD, audit_rows, body_of, cw, letters_to, needs_pg  # noqa: F401  (cw is a fixture)
from tests.pg_world import session_clock  # noqa: F401  (fixture)

AUTH = "/api/v1/auth"
HOST = "app.test"
DAY = 86400


# ------------------------------------------------------------------------------------------------------------- helpers


def jar_of(response):
    jar = {}
    raws = response.headers.get_list("set-cookie")
    for raw in raws:
        parsed = SimpleCookie()
        parsed.load(raw)
        jar.update(parsed)
    return jar


def login(cw, who, remember=None, client=None, password=None):
    client = client or cw.new_client(host=HOST)
    body = {"email": who.email, "password": password or PASSWORD}
    if remember is not None:
        body["remember_me"] = remember
    return client, client.post(f"{AUTH}/login", json=body)


def signed_in(cw, who, remember=None):
    client, r = login(cw, who, remember)
    assert r.status_code == 200, r.text
    return client


def sessions_of(cw, user):
    cw.db.expire_all()
    return cw.db.query(UserSession).filter(UserSession.user_id == user.id).order_by(UserSession.created_at, UserSession.id).all()


def live_sessions(cw, user):
    return [s for s in sessions_of(cw, user) if s.revoked_at is None]


def assert_has_no_token_field(value, path="body"):
    if isinstance(value, dict):
        for key, item in value.items():
            assert "token" not in key.lower(), f"{path}.{key}"
            assert_has_no_token_field(item, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            assert_has_no_token_field(item, f"{path}[{i}]")


def invitation_code(cw, address):
    """The code in the link of the letter that went to *address* (the only place a code ever appears)."""
    msgs = letters_to(cw.outbox, address)
    assert msgs, f"no letter to {address}"
    found = re.findall(r"https://app\.test/invite/([A-Za-z0-9_%-]+)", body_of(msgs[-1]))
    assert len(found) == 1, found
    return found[0]


def invite(client, email, role="teacher"):
    return client.post(f"{AUTH}/org/invitations", json={"email": email, "org_role": role})


def accept(client, code, **over):
    body = {"code": code, "password": "pa55word-ok", "first_name": "Новый", "last_name": "Сотрудник"}
    body.update(over)
    return client.post(f"{AUTH}/invite/accept", json=body)


@pytest.fixture
def centre(cw):
    return cw.make_centre()


# ============================================================================================================ A1: the invitation by letter


@needs_pg
def test_r10_the_invitation_goes_by_letter_and_the_answer_has_neither_a_code_nor_a_link(cw, centre):
    owner = signed_in(cw, centre.owner)
    address = f"new-{uuid4().hex[:8]}@staff.test"
    r = invite(owner, address)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"id", "email_masked", "expires_at", "email_sent"}
    assert body["email_sent"] is True and body["email_masked"] == f"{address[0]}***@staff.test"
    assert address not in r.text and "invite" not in r.text.lower()
    code = invitation_code(cw, address)                       # ... the letter has it
    assert len(code) == 20 and code not in r.text
    msg = letters_to(cw.outbox, address)[-1]
    assert msg["Reply-To"] == "hello@example.test" and "ALGO" in str(msg["From"])
    assert f"https://app.test/invite/{code}" in body_of(msg) and centre.org.name in body_of(msg)


@needs_pg
def test_r10_accepting_by_the_link_proves_the_mailbox_and_signs_in_without_the_remember_mode(cw, centre):
    owner = signed_in(cw, centre.owner)
    address = f"new-{uuid4().hex[:8]}@staff.test"
    invite(owner, address)
    code = invitation_code(cw, address)
    stranger = cw.new_client(host=HOST)
    info = stranger.get(f"{AUTH}/invite/{code}")
    assert info.status_code == 200 and info.json() == {
        "org_name": centre.org.name, "org_role": "teacher", "email_masked": f"{address[0]}***@staff.test", "is_returning": False,
    }
    assert address not in info.text                           # the page of the link never shows the whole address
    r = accept(stranger, code)                                # no e-mail is sent: the letter was the proof
    assert r.status_code == 201, r.text
    assert_has_no_token_field(r.json())
    assert set(r.json()) == {"user", "org"} and r.json()["user"]["email"] == address
    user = cw.db.query(User).filter(User.email == address).one()
    assert user.is_verified is True                           # U8
    session = live_sessions(cw, user)[0]
    assert session.remember is False and (session.expires_at - session.created_at) == timedelta(hours=12)   # K6: a link signs in without the tick
    jar = jar_of(r)
    assert not jar[AUTH_COOKIE_NAME]["max-age"] and not jar[REFRESH_COOKIE_NAME]["max-age"] and not jar[AUTH_COOKIE_NAME]["expires"]
    assert stranger.get(f"{AUTH}/me").json()["email"] == address
    assert accept(cw.new_client(host=HOST), code).status_code in (400, 404)    # the code is spent


@needs_pg
def test_r10_a_wrong_mailbox_in_the_body_is_still_refused(cw, centre):
    owner = signed_in(cw, centre.owner)
    address = f"new-{uuid4().hex[:8]}@staff.test"
    invite(owner, address)
    r = accept(cw.new_client(host=HOST), invitation_code(cw, address), email="someone-else@staff.test")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "INVITE_EMAIL_MISMATCH"


@needs_pg
def test_s4_no_list_of_invitations_shows_a_code_to_the_owner_or_the_admin(cw, centre):
    owner, admin = signed_in(cw, centre.owner), signed_in(cw, centre.admin)
    a1, a2 = f"t1-{uuid4().hex[:6]}@staff.test", f"t2-{uuid4().hex[:6]}@staff.test"
    assert invite(owner, a1, "admin").status_code == 200
    assert invite(admin, a2).status_code == 200
    codes = {invitation_code(cw, a1), invitation_code(cw, a2)}
    for who, client in (("owner", owner), ("admin", admin)):
        r = client.get(f"{AUTH}/org/invitations")
        assert r.status_code == 200 and len(r.json()) >= 2, who
        for row in r.json():
            assert "code" not in row and not [k for k in row if "link" in k or "url" in k or "token" in k]
        assert not any(code in r.text for code in codes), who
    # the other ways to see an invitation hand out no code either
    assert not any(code in owner.get(f"{AUTH}/org/members").text for code in codes)
    rows = cw.db.query(Invitation).filter(Invitation.org_id == centre.org.id).all()
    assert {row.code for row in rows} >= codes                 # (the database has them: the letter's link needs them)


@needs_pg
def test_s4_sending_again_issues_a_new_link_by_letter_and_the_old_one_answers_410(cw, centre):
    owner = signed_in(cw, centre.owner)
    address = f"new-{uuid4().hex[:8]}@staff.test"
    first = invite(owner, address).json()
    old_code = invitation_code(cw, address)
    again = owner.post(f"{AUTH}/org/invitations/{first['id']}/resend")
    assert again.status_code == 200 and set(again.json()) == {"id", "email_masked", "expires_at", "email_sent"} and again.json()["email_sent"] is True
    assert again.json()["id"] != first["id"]
    new_code = invitation_code(cw, address)
    assert new_code != old_code and len(letters_to(cw.outbox, address)) == 2
    stranger = cw.new_client(host=HOST)
    for call in (lambda: stranger.get(f"{AUTH}/invite/{old_code}"), lambda: accept(stranger, old_code)):
        r = call()
        assert r.status_code == 410 and r.json()["code"] == "link_revoked", r.text
        assert "последнего письма" in r.json()["detail"]
    assert stranger.get(f"{AUTH}/invite/{new_code}").status_code == 200
    assert accept(stranger, new_code).status_code == 201


@needs_pg
def test_s4_resend_is_for_the_own_centre_only_and_by_the_right_roles(cw, centre):
    owner = signed_in(cw, centre.owner)
    other = cw.make_centre()
    other_owner = signed_in(cw, other.owner)
    row = invite(owner, f"x-{uuid4().hex[:6]}@staff.test").json()
    assert other_owner.post(f"{AUTH}/org/invitations/{row['id']}/resend").status_code == 404      # another centre: "no such"
    assert owner.post(f"{AUTH}/org/invitations/not-a-uuid/resend").status_code == 404
    assert owner.post(f"{AUTH}/org/invitations/{uuid4()}/resend").status_code == 404
    teacher = signed_in(cw, centre.teacher)
    assert teacher.post(f"{AUTH}/org/invitations/{row['id']}/resend").status_code == 403
    admin = signed_in(cw, centre.admin)
    admin_row = invite(owner, f"adm-{uuid4().hex[:6]}@staff.test", "admin").json()
    assert admin.post(f"{AUTH}/org/invitations/{admin_row['id']}/resend").status_code == 403       # an admin invites teachers only (the rule of creating)


@needs_pg
def test_r12_a_letter_that_did_not_go_is_said_so_and_resend_issues_a_new_link(cw, centre, monkeypatch):
    from src.mail import sender

    owner = signed_in(cw, centre.owner)
    monkeypatch.setattr(sender, "deliver", lambda msg, cfg=None: False)
    address = f"new-{uuid4().hex[:8]}@staff.test"
    r = invite(owner, address)
    assert r.status_code == 200 and r.json()["email_sent"] is False and "code" not in r.text
    first_id = r.json()["id"]
    first_code = cw.db.query(Invitation).filter(Invitation.id == first_id).one().code
    monkeypatch.setattr(sender, "deliver", lambda msg, cfg=None: cw.outbox.append(msg) or True)
    again = owner.post(f"{AUTH}/org/invitations/{first_id}/resend")
    assert again.json()["email_sent"] is True
    assert cw.new_client(host=HOST).get(f"{AUTH}/invite/{first_code}").status_code == 410
    assert cw.new_client(host=HOST).get(f"{AUTH}/invite/{invitation_code(cw, address)}").status_code == 200


@needs_pg
def test_f10_a_template_that_fails_gives_email_sent_false_and_a_normal_answer(cw, centre, monkeypatch):
    from src.mail import letters

    owner = signed_in(cw, centre.owner)

    def boom(**kw):
        raise KeyError("StrictUndefined")

    monkeypatch.setattr(letters, "render_staff_invite", boom)
    r = invite(owner, f"new-{uuid4().hex[:8]}@staff.test")
    assert r.status_code == 200 and r.json()["email_sent"] is False


@needs_pg
def test_the_letters_to_one_mailbox_are_limited_and_the_limit_is_a_429(cw, centre):
    owner = signed_in(cw, centre.owner)
    address = f"same-{uuid4().hex[:6]}@staff.test"
    codes = [invite(owner, address).status_code for _ in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429
    assert len(letters_to(cw.outbox, address)) == 10


@needs_pg
def test_t11_no_log_line_carries_a_code_or_a_whole_address(cw, centre, caplog):
    owner = signed_in(cw, centre.owner)
    address = f"secret.person-{uuid4().hex[:6]}@staff.test"
    with caplog.at_level(logging.DEBUG):
        invite(owner, address)
        code = invitation_code(cw, address)
        stranger = cw.new_client(host=HOST)
        stranger.get(f"{AUTH}/invite/{code}")
        r = accept(stranger, code)
        assert r.status_code == 201
        login(cw, cw.db.query(User).filter(User.email == address).one())      # (a wrong password: wrong anyway)
    # the lines of OUR code (the test client's own httpx log line is the caller's side, not the service's)
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith(("src", "uvicorn")))
    assert ours, "the service logged nothing at all: the check would prove nothing"
    assert code not in ours
    assert address not in ours and "secret.person" not in ours


def test_t11_no_logging_call_of_the_service_names_an_address_or_a_code():
    """LESSONS 51: every path, not one: an AST scan of EVERY logging call in src."""
    import ast
    import pathlib

    offenders = []
    for path in pathlib.Path("src").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("debug", "info", "warning", "error", "exception", "critical")
                    and isinstance(node.func.value, ast.Name) and node.func.value.id in ("logger", "log", "logging")):
                call = ast.get_source_segment(source, node) or ""
                if re.search(r"\.email\b|\bemail\}|invite\.code|\.code\b|request\.code|owner_email|invite_code", call):
                    offenders.append(f"{path}:{node.lineno}")
    assert offenders == []


# ============================================================================================================ A2 / S3: no token in any answer


def schema_properties(spec, schema, seen=()):
    """Every property name reachable from a response schema of the OpenAPI document."""
    names = set()
    if not isinstance(schema, dict):
        return names
    if "$ref" in schema:
        ref = schema["$ref"].split("/")[-1]
        if ref in seen:
            return names
        return schema_properties(spec, spec["components"]["schemas"][ref], seen + (ref,))
    for key in ("properties",):
        for name, sub in (schema.get(key) or {}).items():
            names.add(name)
            names |= schema_properties(spec, sub, seen)
    for key in ("items", "additionalProperties"):
        names |= schema_properties(spec, schema.get(key), seen)
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key) or []:
            names |= schema_properties(spec, sub, seen)
    return names


def test_s3_no_response_schema_of_an_auth_route_has_a_token_field():
    """By the schema of ALL the routes of the auth router (not one route): a ``*token*`` property in an answer would put a credential in the page."""
    spec = main_v2.app.openapi()
    offenders = []
    checked = 0
    for path, methods in spec["paths"].items():
        if not path.startswith("/api/v1/auth"):
            continue
        for method, operation in methods.items():
            for status, response in (operation.get("responses") or {}).items():
                for media in (response.get("content") or {}).values():
                    checked += 1
                    bad = sorted(n for n in schema_properties(spec, media.get("schema")) if "token" in n.lower())
                    if bad:
                        offenders.append((method.upper(), path, status, bad))
    assert checked > 50
    assert offenders == []


@needs_pg
def test_s3_the_bodies_of_login_refresh_accept_and_the_password_link_have_no_token(cw, centre):
    client, r = login(cw, centre.owner, remember=True)
    assert r.status_code == 200 and set(r.json()) == {"user", "org"}
    assert_has_no_token_field(r.json())
    refreshed = client.post(f"{AUTH}/refresh", json={})
    assert refreshed.status_code == 200 and not re.search(r"token|eyJ", refreshed.text)
    # the password link signs in too: the same shape
    raw, _ = links.issue(cw.db, centre.teacher.id, "reset")
    cw.db.commit()
    anonymous = cw.new_client(host=HOST)
    done = anonymous.post(f"{AUTH}/password-links/{raw}", json={"password": "another-pa55word", "password_confirm": "another-pa55word"})
    assert done.status_code == 200 and set(done.json()) == {"user", "org"} and not re.search(r"token|eyJ", done.text)
    assert centre.teacher.is_verified is True


@needs_pg
def test_a2_refresh_reads_the_cookie_only(cw, centre):
    client, r = login(cw, centre.owner)
    refresh_value = jar_of(r)[REFRESH_COOKIE_NAME].value
    assert refresh_value and r.status_code == 200
    # a refresh token in the BODY is not accepted, whatever the cookies say
    nobody = cw.new_client(host=HOST)
    assert nobody.post(f"{AUTH}/refresh", json={"refresh_token": refresh_value}).status_code == 401
    assert nobody.post(f"{AUTH}/refresh", json={}).status_code == 401
    assert client.post(f"{AUTH}/refresh", json={"refresh_token": "junk"}).status_code == 200   # the cookie wins; the body is ignored
    spec = main_v2.app.openapi()["paths"][f"{AUTH}/refresh"]["post"]
    assert "requestBody" not in spec


def test_a2_the_dead_activation_route_and_the_old_mail_module_are_gone():
    import pathlib

    assert main_v2.app.openapi()["paths"].get(f"{AUTH}/activate") is None
    assert not pathlib.Path("src/core/email.py").exists()
    hits = [str(p) for p in pathlib.Path("src").rglob("*.py") if "algo.kz" in p.read_text(encoding="utf-8")]
    assert hits == []                                                             # L12
    imports = [str(p) for p in pathlib.Path("src").rglob("*.py") if re.search(r"core\.email|core import email", p.read_text(encoding="utf-8"))]
    assert imports == []


# ============================================================================================================ A3: __Host- cookies


def test_a3_the_secure_cookies_are_host_prefixed_root_path_no_domain_lax(monkeypatch):
    from starlette.responses import JSONResponse

    monkeypatch.setenv("COOKIE_DOMAIN", ".algolab.me")        # an old setting must not put a Domain back
    for remember in (False, True):
        resp = cookies_module.set_auth_cookies(JSONResponse({}), "A.B.C", "D.E.F", remember, secure=True)
        jar = {}
        for raw in resp.headers.getlist("set-cookie"):
            parsed = SimpleCookie()
            parsed.load(raw)
            jar.update(parsed)
        assert set(jar) == {"__Host-algo_auth_token", "__Host-algo_refresh_token"}
        for morsel in jar.values():
            assert morsel["path"] == "/" and not morsel["domain"] and morsel["secure"] is True and morsel["httponly"] is True
            assert morsel["samesite"].lower() == "lax"
            assert bool(morsel["max-age"]) is remember and not morsel["expires"]
    cleared = cookies_module.clear_auth_cookies(JSONResponse({}), secure=True)
    names = {raw.split("=", 1)[0] for raw in cleared.headers.getlist("set-cookie")}
    assert names == {"__Host-algo_auth_token", "__Host-algo_refresh_token"}


def test_a3_http_development_keeps_the_plain_names_without_secure():
    from starlette.responses import JSONResponse

    assert cookies_module.cookie_names(False) == ("algo_auth_token", "algo_refresh_token")
    assert cookies_module.cookies_are_secure({"APP_ENV": "staging"}) and cookies_module.cookies_are_secure({"APP_ENV": "production"})
    assert not cookies_module.cookies_are_secure({"APP_ENV": "development"}) and not cookies_module.cookies_are_secure({"APP_ENV": "testing"})
    resp = cookies_module.set_auth_cookies(JSONResponse({}), "a", "r", False, secure=False)
    assert all("Secure" not in raw for raw in resp.headers.getlist("set-cookie"))


@needs_pg
def test_a3_a_cookie_under_the_old_name_is_not_a_credential_when_the_prefixed_names_are_active(cw, centre, monkeypatch):
    client, r = login(cw, centre.owner)
    access = jar_of(r)[AUTH_COOKIE_NAME].value
    assert client.get(f"{AUTH}/me").status_code == 200
    monkeypatch.setattr(dependencies, "AUTH_COOKIE_NAME", "__Host-algo_auth_token")
    planted = cw.new_client(host=HOST)
    planted.cookies.set("algo_auth_token", access)             # what a sibling subdomain could plant under the old name
    assert planted.get(f"{AUTH}/me").status_code == 401


@needs_pg
def test_a3_a_new_sign_in_ends_the_session_the_browser_still_held(cw, centre):
    shared_pc = cw.new_client(host=HOST)
    first_client, r1 = login(cw, centre.teacher, client=shared_pc)
    assert r1.status_code == 200
    old_refresh = jar_of(r1)[REFRESH_COOKIE_NAME].value
    first_session = live_sessions(cw, centre.teacher)[0]
    # the next person signs in at the same browser (the jar still holds the previous refresh cookie)
    _, r2 = login(cw, centre.admin, client=shared_pc)
    assert r2.status_code == 200
    cw.db.expire_all()
    assert cw.db.get(UserSession, first_session.id).revoked_at is not None and cw.db.get(UserSession, first_session.id).revoke_reason == "new_login"
    assert len(live_sessions(cw, centre.admin)) == 1
    # the copied old cookie is dead
    thief = cw.new_client(host=HOST)
    thief.cookies.set(REFRESH_COOKIE_NAME, old_refresh)
    assert thief.post(f"{AUTH}/refresh", json={}).status_code == 401


@needs_pg
def test_a3_a_sign_in_in_another_browser_does_not_touch_the_first_one(cw, centre):
    a, _ = login(cw, centre.teacher)
    b, rb = login(cw, centre.teacher)
    assert rb.status_code == 200 and len(live_sessions(cw, centre.teacher)) == 2
    assert a.post(f"{AUTH}/refresh", json={}).status_code == 200


# ============================================================================================================ A4 / S1: superadmin


@needs_pg
def test_s1_a_superadmin_cannot_sign_in_to_the_cabinet_and_the_attempt_is_journalled(cw):
    sup = cw.make_user(role="superadmin")
    cw.db.commit()
    client, bad = login(cw, sup, password="wrong-password-here")
    assert bad.status_code == 401
    _, good = login(cw, sup)
    assert good.status_code == 401
    assert good.json() == bad.json() and "set-cookie" not in good.headers                      # the answer of a wrong password
    rows = audit_rows(cw.db, "login.superadmin_denied", target_id=str(sup.id))
    assert len(rows) == 1 and rows[0].actor_user_id is None and rows[0].details == {"where": "cabinet"}
    assert live_sessions(cw, sup) == []
    # a normal user's wrong password leaves no such row
    _, other = login(cw, cw.make_user(), password="nope")
    assert len(audit_rows(cw.db, "login.superadmin_denied")) == 1


@needs_pg
def test_s1_an_old_session_of_a_superadmin_cannot_be_refreshed_and_the_console_is_untouched(cw):
    sup = cw.make_user(role="superadmin")
    cw.db.commit()
    token = AuthService(cw.db)._create_tokens(sup)
    cw.db.commit()
    client = cw.new_client(host=HOST)
    client.cookies.set(REFRESH_COOKIE_NAME, token.refresh_token)
    assert client.post(f"{AUTH}/refresh", json={}).status_code == 401
    # the console owner (a staff account, not a cabinet one) still signs in to the console
    r = cw.new_client().post("/api/v1/platform/auth/login", json={"email": cw.owner.email, "password": cw.owner.password})
    assert r.status_code == 200, r.text


# ============================================================================================================ A5: the access token dies with its session


@needs_pg
def test_a5_the_access_token_names_its_session_and_a_token_without_one_is_refused(cw, centre):
    client, r = login(cw, centre.owner)
    access = jar_of(r)[AUTH_COOKIE_NAME].value
    from jose import jwt as jose_jwt
    from src.auth.service import JWT_SECRET

    claims = jose_jwt.decode(access, JWT_SECRET, algorithms=["HS256"])
    assert claims["sid"] == str(live_sessions(cw, centre.owner)[0].id)
    old_style = create_access_token(str(centre.owner.id), "owner", str(centre.org.id), "owner")          # (no sid)
    nobody = cw.new_client(host=HOST)
    nobody.cookies.set(AUTH_COOKIE_NAME, old_style)
    assert nobody.get(f"{AUTH}/me").status_code == 401
    foreign = create_access_token(str(centre.owner.id), "owner", str(centre.org.id), "owner", sid=str(uuid4()))
    nobody.cookies.set(AUTH_COOKIE_NAME, foreign)
    assert nobody.get(f"{AUTH}/me").status_code == 401
    # a session of ANOTHER user does not carry this user's token
    _, r2 = login(cw, centre.admin)
    admin_sid = str(live_sessions(cw, centre.admin)[0].id)
    crossed = create_access_token(str(centre.owner.id), "owner", str(centre.org.id), "owner", sid=admin_sid)
    nobody.cookies.set(AUTH_COOKIE_NAME, crossed)
    assert nobody.get(f"{AUTH}/me").status_code == 401


@needs_pg
def test_u9_s2_after_a_sign_out_the_old_access_cookie_is_refused_at_once_by_me_which_is_all_exam_and_diagnostics_ask(cw, centre):
    client, r = login(cw, centre.owner)
    jar = jar_of(r)
    # exactly how exam-service / diagnostic-service ask: GET /auth/me with the browser's Cookie header copied whole (and other cookies beside ours)
    header = {"cookie": f"theme=dark; {AUTH_COOKIE_NAME}={jar[AUTH_COOKIE_NAME].value}; {REFRESH_COOKIE_NAME}={jar[REFRESH_COOKIE_NAME].value}; x=1"}
    server = cw.new_client(host=HOST)
    assert server.get(f"{AUTH}/me", headers=header).status_code == 200
    out = client.post(f"{AUTH}/logout")
    assert out.status_code == 200
    stale = cw.new_client(host=HOST)
    assert stale.get(f"{AUTH}/me", headers=header).status_code == 401           # S2: at once, not in 60 minutes
    assert stale.post(f"{AUTH}/refresh", headers=header, json={}).status_code == 401


@needs_pg
def test_a5_logout_with_the_access_cookie_alone_revokes_that_session_only(cw, centre):
    a, ra = login(cw, centre.teacher)
    b, rb = login(cw, centre.teacher)
    access_a = jar_of(ra)[AUTH_COOKIE_NAME].value
    only_access = cw.new_client(host=HOST)
    only_access.cookies.set(AUTH_COOKIE_NAME, access_a)
    assert only_access.post(f"{AUTH}/logout").status_code == 200
    assert a.get(f"{AUTH}/me").status_code == 401
    assert b.get(f"{AUTH}/me").status_code == 200                                  # the other device stays signed in (#101)


@needs_pg
def test_u9_a_password_set_by_a_link_ends_the_open_session_and_its_access_token_at_once(cw, centre):
    client, r = login(cw, centre.teacher)
    old_access = jar_of(r)[AUTH_COOKIE_NAME].value
    assert client.get(f"{AUTH}/me").status_code == 200
    raw, _ = links.issue(cw.db, centre.teacher.id, "reset")
    cw.db.commit()
    done = cw.new_client(host=HOST).post(f"{AUTH}/password-links/{raw}", json={"password": "brand-new-pa55", "password_confirm": "brand-new-pa55"})
    assert done.status_code == 200
    assert client.get(f"{AUTH}/me").status_code == 401                              # the open session's access
    assert client.post(f"{AUTH}/refresh", json={}).status_code == 401               # ... and its refresh
    holder = cw.new_client(host=HOST)
    holder.cookies.set(AUTH_COOKIE_NAME, old_access)
    assert holder.get(f"{AUTH}/me").status_code == 401
    assert len(live_sessions(cw, centre.teacher)) == 1                              # only the new sign-in by the link


@needs_pg
def test_a5_removing_or_blocking_an_employee_ends_his_access_at_once(cw, centre):
    owner = signed_in(cw, centre.owner)
    teacher = signed_in(cw, centre.teacher)
    assert teacher.get(f"{AUTH}/me").status_code == 200
    member = cw.db.query(OrganizationMember).filter_by(org_id=centre.org.id, user_id=centre.teacher.id).one()
    assert owner.delete(f"{AUTH}/org/members/{member.id}").status_code == 200
    assert teacher.get(f"{AUTH}/me").status_code == 401 and teacher.post(f"{AUTH}/refresh", json={}).status_code == 401
    # blocked from the console: the user is switched off and every session revoked
    admin = signed_in(cw, centre.admin)
    assert admin.get(f"{AUTH}/me").status_code == 200
    r = cw.client.post(f"/api/v1/platform/users/{centre.admin.id}/block", json={"reason": "тест блокировки"})
    assert r.status_code in (200, 204), r.text
    assert admin.get(f"{AUTH}/me").status_code == 401


@needs_pg
def test_a5_a_revoked_session_is_refused_by_a_business_route_too_not_only_by_me(cw, centre):
    owner = signed_in(cw, centre.owner)
    assert owner.get(f"{AUTH}/org/members").status_code == 200
    user_sessions.revoke_all(cw.db, centre.owner.id, "password_changed")
    cw.db.commit()
    assert owner.get(f"{AUTH}/org/members").status_code == 401


def test_a5_the_session_check_is_one_primary_key_read():
    """Measured on the disposable database: the check costs one indexed read; no cache is needed (and no cache means a revocation is instant)."""
    import inspect

    source = inspect.getsource(user_sessions.is_alive)
    assert source.count("db.execute") == 1 and "cache" not in source.lower().replace("no cache", "")


# ============================================================================================================ D: «Запомнить меня»


@needs_pg
def test_k1_without_the_tick_both_cookies_have_no_max_age_nor_expires_also_after_a_refresh(cw, centre):
    client, r = login(cw, centre.owner)
    for response in (r, client.post(f"{AUTH}/refresh", json={})):
        jar = jar_of(response)
        assert set(jar) == {AUTH_COOKIE_NAME, REFRESH_COOKIE_NAME}
        assert not jar[AUTH_COOKIE_NAME]["max-age"] and not jar[REFRESH_COOKIE_NAME]["max-age"]
        assert not jar[AUTH_COOKIE_NAME]["expires"] and not jar[REFRESH_COOKIE_NAME]["expires"]
        assert jar[AUTH_COOKIE_NAME]["httponly"] is True
    s = live_sessions(cw, centre.owner)[0]
    assert s.remember is False


@needs_pg
def test_k2_with_the_tick_both_cookies_live_14_days_also_after_a_refresh(cw, centre):
    client, r = login(cw, centre.owner, remember=True)
    for response in (r, client.post(f"{AUTH}/refresh", json={})):
        jar = jar_of(response)
        assert jar[AUTH_COOKIE_NAME]["max-age"] == str(14 * DAY) == jar[REFRESH_COOKIE_NAME]["max-age"]
    assert live_sessions(cw, centre.owner)[0].remember is True


def test_the_default_lifetime_of_a_remembered_session_is_14_days_not_30():
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "JWT_REFRESH_TTL_DAYS"}
    out = subprocess.run([sys.executable, "-c", "import src.auth.service as s; print(s.REFRESH_TOKEN_EXPIRE_DAYS)"], env=env, capture_output=True, text=True, timeout=120)
    assert out.stdout.strip().splitlines()[-1] == "14", out.stderr[-300:]


@needs_pg
def test_k2_the_remembered_session_slides_14_days_from_every_refresh_with_no_ceiling(cw, centre, session_clock):
    client, _ = login(cw, centre.owner, remember=True)
    session = live_sessions(cw, centre.owner)[0]
    first_end = session.expires_at
    assert timedelta(days=13, hours=23) < first_end - session.created_at <= timedelta(days=14, minutes=1)
    for step in range(5):                                    # 5 refreshes, each 10 days apart: 50 days in all, far beyond 14 and 30
        session_clock.advance(10 * DAY)
        r = client.post(f"{AUTH}/refresh", json={})
        assert r.status_code == 200, step
    cw.db.expire_all()
    session = cw.db.get(UserSession, session.id)
    assert session.expires_at - first_end >= timedelta(days=49)
    assert session.revoked_at is None
    # 15 days of silence ends it
    session_clock.advance(15 * DAY)
    assert client.post(f"{AUTH}/refresh", json={}).status_code == 401


@needs_pg
def test_k4_the_mode_survives_two_rotations_in_a_row_and_a_race_of_two_tabs(cw, centre):
    client, r = login(cw, centre.owner, remember=True)
    first = jar_of(r)[REFRESH_COOKIE_NAME].value
    second_r = client.post(f"{AUTH}/refresh", json={})
    third_r = client.post(f"{AUTH}/refresh", json={})
    for response in (second_r, third_r):
        assert response.status_code == 200 and jar_of(response)[REFRESH_COOKIE_NAME]["max-age"] == str(14 * DAY)
    # a second tab still sends the FIRST cookie (the race): it gets a sibling token and the same mode
    tab2 = cw.new_client(host=HOST)
    tab2.cookies.set(REFRESH_COOKIE_NAME, first)
    raced = tab2.post(f"{AUTH}/refresh", json={})
    assert raced.status_code in (200, 401)
    if raced.status_code == 200:
        assert jar_of(raced)[AUTH_COOKIE_NAME]["max-age"] == str(14 * DAY)
    assert live_sessions(cw, centre.owner)[0].remember is True
    # and without the tick the mode is also kept over several rotations
    c2, r2 = login(cw, centre.admin)
    for _ in range(3):
        again = c2.post(f"{AUTH}/refresh", json={})
        assert again.status_code == 200 and not jar_of(again)[REFRESH_COOKIE_NAME]["max-age"]
    assert live_sessions(cw, centre.admin)[0].remember is False


@needs_pg
def test_k5_k8_a_session_without_the_tick_ends_12_hours_after_the_sign_in_whatever_the_refreshes(cw, centre, session_clock):
    client, _ = login(cw, centre.teacher)
    session = live_sessions(cw, centre.teacher)[0]
    assert session.expires_at - session.created_at == timedelta(hours=12)
    end = session.expires_at
    for hours in (3, 3, 3, 2):                               # refreshes at +3, +6, +9, +11 hours
        session_clock.advance(hours * 3600)
        assert client.post(f"{AUTH}/refresh", json={}).status_code == 200
        cw.db.expire_all()
        assert cw.db.get(UserSession, session.id).expires_at == end                           # the refresh does not extend it
    session_clock.advance(3600 + 60)                         # +12 h 1 min since the sign-in
    refused = client.post(f"{AUTH}/refresh", json={})
    assert refused.status_code == 401                        # even with a perfectly valid (copied) cookie


@needs_pg
def test_k6_the_password_link_signs_in_without_the_tick(cw, centre):
    raw, _ = links.issue(cw.db, centre.teacher.id, "reset")
    cw.db.commit()
    r = cw.new_client(host=HOST).post(f"{AUTH}/password-links/{raw}", json={"password": "brand-new-pa55", "password_confirm": "brand-new-pa55"})
    jar = jar_of(r)
    assert not jar[AUTH_COOKIE_NAME]["max-age"] and not jar[REFRESH_COOKIE_NAME]["max-age"]
    s = live_sessions(cw, centre.teacher)[0]
    assert s.remember is False and s.expires_at - s.created_at == timedelta(hours=12)


@needs_pg
def test_the_tick_reaches_the_server_and_is_off_by_default(cw, centre):
    _, r1 = login(cw, centre.owner)
    _, r2 = login(cw, centre.owner, remember=False)
    _, r3 = login(cw, centre.owner, remember=True)
    assert [s.remember for s in sessions_of(cw, centre.owner)] == [False, False, True]
    # a junk value is a validation error, never "remembered"
    bad = cw.new_client(host=HOST).post(f"{AUTH}/login", json={"email": centre.owner.email, "password": PASSWORD, "remember_me": "maybe"})
    assert bad.status_code == 422


# ============================================================================================================ F8: sign-in || password set


def wait_for_advisory_waiter(engine, seconds=15):
    deadline = time.time() + seconds
    with engine.connect() as c:
        while time.time() < deadline:
            n = c.execute(text("select count(*) from pg_stat_activity where wait_event_type='Lock' and wait_event='advisory'")).scalar()
            if n:
                return True
            time.sleep(0.05)
    return False


@needs_pg
def test_f8_a_sign_in_that_verified_the_old_password_is_refused_when_the_password_changed_meanwhile(cw, centre):
    user = centre.teacher
    factory = sessionmaker(cw.engine, autoflush=False)
    db_b, db_a = factory(), factory()
    outcome = {}
    try:
        # B: the password is changed and the sessions revoked, NOT yet committed (it holds the session lock)
        b_user = db_b.get(User, user.id)
        b_user.password_hash = hash_password("the-new-password-1")
        user_sessions.revoke_all(db_b, user.id, user_sessions.REVOKE_PASSWORD_CHANGED)

        def sign_in_with_the_old_password():
            try:
                AuthService(db_a).login(LoginRequest(email=user.email, password=PASSWORD))
                outcome["result"] = "signed in"
            except InvalidCredentialsError:
                outcome["result"] = "refused"
            except Exception as exc:  # noqa: BLE001
                outcome["result"] = f"error {type(exc).__name__}"

        thread = threading.Thread(target=sign_in_with_the_old_password)
        thread.start()
        assert wait_for_advisory_waiter(cw.engine), "the sign-in should be waiting for the session lock"
        db_b.commit()                                           # the password change lands
        thread.join(30)
        assert not thread.is_alive()
        assert outcome["result"] == "refused"
        cw.db.expire_all()
        assert live_sessions(cw, user) == []                    # no session issued by the OLD password survives
        # the new password works, the old one does not
        assert AuthService(factory()).login(LoginRequest(email=user.email, password="the-new-password-1")).user.email == user.email
    finally:
        db_a.close()
        db_b.close()


@needs_pg
def test_f8_a_sign_in_that_lands_first_is_revoked_by_the_password_change_that_follows(cw, centre):
    user = centre.admin
    factory = sessionmaker(cw.engine, autoflush=False)
    AuthService(factory()).login(LoginRequest(email=user.email, password=PASSWORD))
    assert len(live_sessions(cw, user)) == 1
    db_b = factory()
    try:
        db_b.get(User, user.id).password_hash = hash_password("the-new-password-2")
        user_sessions.revoke_all(db_b, user.id, user_sessions.REVOKE_PASSWORD_CHANGED)
        db_b.commit()
    finally:
        db_b.close()
    assert live_sessions(cw, user) == []


@needs_pg
def test_the_dev_router_hands_the_invitation_code_to_the_e2e_harness_only(cw, centre):
    """``/auth/dev-invite-code`` exists only in the router that is mounted when APP_ENV=development (the e2e stack has no mailbox)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.auth.dev_router import router as dev_router
    from src.core.database import get_db

    owner = signed_in(cw, centre.owner)
    address = f"dev-{uuid4().hex[:6]}@staff.test"
    invite(owner, address)
    app = FastAPI()
    app.include_router(dev_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: cw.db
    client = TestClient(app)
    assert client.get(f"{AUTH}/dev-invite-code", params={"email": address}).json() == {"code": invitation_code(cw, address)}
    assert client.get(f"{AUTH}/dev-invite-code", params={"email": "nobody@staff.test"}).status_code == 404
    assert "/api/v1/auth/dev-invite-code" not in main_v2.app.openapi()["paths"]      # not in the real application (APP_ENV=testing)


@needs_pg
def test_the_superadmin_count_script_reads_only_and_prints_no_address(cw):
    import os
    import subprocess
    import sys

    sup = cw.make_user(role="superadmin")
    cw.db.commit()
    out = subprocess.run([sys.executable, "scripts/count_superadmins.py"], env={**os.environ, "DATABASE_URL": os.environ["A2_AUTH_TEST_DATABASE_URL"]},
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-300:]
    assert re.search(r"superadmin users: \d+ \(active: \d+, with a console account: \d+\)", out.stdout)
    assert str(sup.id) in out.stdout and "@" not in out.stdout


# ============================================================================================================ T6, T7, T9, T11: the links of passwords


def link_row(cw, link_id):
    cw.db.expire_all()
    return cw.db.get(PasswordLink, link_id)


@needs_pg
def test_t9_a_get_never_changes_the_row_of_a_link(cw, centre):
    raw, link = links.issue(cw.db, centre.teacher.id, "reset")
    cw.db.commit()
    before = (link_row(cw, link.id).used_at, link_row(cw, link.id).revoked_at, link_row(cw, link.id).expires_at)
    scanner = cw.new_client(host=HOST)
    for _ in range(3):
        assert scanner.get(f"{AUTH}/password-links/{raw}").status_code == 200
    row = link_row(cw, link.id)
    assert (row.used_at, row.revoked_at, row.expires_at) == before and row.used_at is None and row.revoked_at is None


@needs_pg
def test_t7_a_blocked_centre_keeps_the_link_alive_and_it_works_after_the_unblock(cw, centre):
    raw, link = links.issue(cw.db, centre.owner.id, "centre_invite")
    cw.db.commit()
    body = {"password": "brand-new-pa55", "password_confirm": "brand-new-pa55"}
    assert cw.client.post(f"/api/v1/platform/orgs/{centre.org.id}/block", json={"reason": "тест блокировки"}).status_code == 200
    r = cw.new_client(host=HOST).post(f"{AUTH}/password-links/{raw}", json=body)
    assert r.status_code == 403 and r.json()["code"] == "org_blocked"
    assert link_row(cw, link.id).used_at is None
    assert cw.client.post(f"/api/v1/platform/orgs/{centre.org.id}/unblock", json={}).status_code == 200
    assert cw.new_client(host=HOST).post(f"{AUTH}/password-links/{raw}", json=body).status_code == 200


@needs_pg
def test_t6_an_employee_removed_by_the_centre_cannot_use_a_live_link(cw, centre):
    owner = signed_in(cw, centre.owner)
    raw, link = links.issue(cw.db, centre.teacher.id, "reset")
    cw.db.commit()
    member = cw.db.query(OrganizationMember).filter_by(org_id=centre.org.id, user_id=centre.teacher.id).one()
    assert owner.delete(f"{AUTH}/org/members/{member.id}").status_code == 200
    r = cw.new_client(host=HOST).post(f"{AUTH}/password-links/{raw}", json={"password": "brand-new-pa55", "password_confirm": "brand-new-pa55"})
    assert r.status_code == 403 and r.json()["code"] == "user_blocked"          # (the wording for «removed by the centre» is K2-2, block F)
    assert link_row(cw, link.id).used_at is None
    assert live_sessions(cw, centre.teacher) == []


@needs_pg
def test_t11_the_token_of_a_link_is_in_no_log_no_journal_row_and_no_answer_of_the_list(cw, centre, caplog):
    raw, link = links.issue(cw.db, centre.teacher.id, "reset")
    cw.db.commit()
    with caplog.at_level(logging.DEBUG):
        anon = cw.new_client(host=HOST)
        anon.get(f"{AUTH}/password-links/{raw}")
        done = anon.post(f"{AUTH}/password-links/{raw}", json={"password": "brand-new-pa55", "password_confirm": "brand-new-pa55"})
        assert done.status_code == 200
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith(("src", "uvicorn")))
    assert raw not in ours and "brand-new-pa55" not in ours
    rows = audit_rows(cw.db, "user.password_set_by_link", target_id=str(centre.teacher.id))
    assert rows and raw not in json.dumps(rows[-1].details, default=str)
    owner = signed_in(cw, centre.owner)
    assert raw not in owner.get(f"{AUTH}/org/members").text


def test_t11_the_access_log_hides_the_code_of_an_invitation_but_not_the_accept_route():
    from src.core.middleware import DiagnosticCapabilityLogFilter as F

    assert F._redact('"GET /api/v1/auth/invite/Zk3Q9dR2mT7vB5nX8aLw HTTP/1.1" 200') == '"GET /api/v1/auth/invite/[redacted] HTTP/1.1" 200'
    assert F._redact('"POST /api/v1/auth/invite/accept HTTP/1.1" 201') == '"POST /api/v1/auth/invite/accept HTTP/1.1" 201'
    assert F._redact('"POST /api/v1/auth/password-links/abcDEF-ghi_123 HTTP/1.1" 200') == '"POST /api/v1/auth/password-links/[redacted] HTTP/1.1" 200'
