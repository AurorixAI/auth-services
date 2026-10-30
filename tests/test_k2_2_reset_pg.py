"""K2-2 on the real application and the disposable ``a2_auth`` PostgreSQL: G2 (letters in the background, the status of an invitation's letter), B «Забыли пароль»,
C the link from the leadership of a centre, F the one password rule / the messages of the sign-in / the console answer, H no ``superadmin``.

Scenario ids are those of ``artifacts/analysis/K2_SPEC.md`` (R, T, U, F, L, M, P, H, TS) and of the analysis §8 (invariants 1-3, 6, 7, 10, 12, 13)."""
import json
import logging
import re
import threading
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from src.api import main_v2
from src.auth import password_links as links, password_reset as reset, password_reset_router, turnstile, user_sessions
from src.auth.service import AuthService, hash_token
from src.models.auth import Group, Invitation, MemberAudit, Organization, OrganizationMember, User, UserSession
from src.models.platform import PasswordLink, PlatformStaff

from tests.console_helpers import PASSWORD, advance_clock, body_of, cw, letters_to, needs_pg  # noqa: F401  (cw is a fixture)
from tests.test_k2_1_pg import (  # noqa: F401
    AUTH, HOST, accept, centre, invitation_code, invite, jar_of, live_sessions, login, sessions_of, signed_in,
)

LINKS = f"{AUTH}/password-links"
NEW_PASSWORD = "brand-new-pa55"
NEUTRAL = reset.NEUTRAL_TEXT


def forgot(client, email, token="ok-token"):
    return client.post(f"{AUTH}/password/forgot", json={"email": email, "turnstile_token": token})


def link_token(cw, address, which=-1):
    msgs = letters_to(cw.outbox, address)
    assert msgs, f"no letter to {address}"
    found = re.findall(r"https://app\.test/set-password/([A-Za-z0-9_%-]+)", body_of(msgs[which]))
    assert len(found) == 1, found
    return found[0]


def anonymous(cw):
    return cw.new_client(host=HOST)


def member_of(cw, user, org=None):
    cw.db.expire_all()
    q = cw.db.query(OrganizationMember).filter(OrganizationMember.user_id == user.id)
    if org is not None:
        q = q.filter(OrganizationMember.org_id == org.id)
    return q.first()


def add_member(cw, centre_, org_role, *, first=None):
    user = cw.make_user(first=first or org_role.capitalize(), role=org_role)
    user.default_org_id = centre_.org.id
    cw.db.add(OrganizationMember(org_id=centre_.org.id, user_id=user.id, org_role=org_role))
    cw.db.commit()
    return user


def live_resets(cw, user):
    cw.db.expire_all()
    return cw.db.query(PasswordLink).filter(
        PasswordLink.user_id == user.id, PasswordLink.purpose == "reset", PasswordLink.used_at.is_(None), PasswordLink.revoked_at.is_(None)
    ).all()


def set_by_link(client, token, password=NEW_PASSWORD):
    return client.post(f"{LINKS}/{token}", json={"password": password, "password_confirm": password})


# ============================================================================================================ B: «Забыли пароль» (R1-R3, R8, R9)


@needs_pg
@pytest.mark.parametrize("who", ["owner", "admin", "teacher"])
def test_r1_r3_the_owner_the_admin_and_the_teacher_each_get_a_link_by_letter_from_the_one_screen(cw, centre, who):
    user = getattr(centre, who)
    r = forgot(anonymous(cw), user.email)
    assert r.status_code == 200 and r.json() == {"ok": True, "message": NEUTRAL}
    msgs = letters_to(cw.outbox, user.email)
    assert len(msgs) == 1 and msgs[0]["Subject"] == "Сброс пароля ALGO"
    text = body_of(msgs[0])
    assert "на странице входа" in text and "до " in text and user.email in text and "(по Ташкенту)" in text
    token = link_token(cw, user.email)
    live = live_resets(cw, user)
    assert len(live) == 1 and live[0].token_hash == hash_token(token) and live[0].created_by is None
    assert (live[0].expires_at - live[0].created_at) <= timedelta(hours=1, seconds=1)


@needs_pg
def test_r8_a_student_or_a_parent_has_no_account_so_no_letter_and_the_role_cannot_be_made(cw, centre):
    from sqlalchemy.exc import IntegrityError

    for role in ("student", "parent"):
        cw.db.add(User(email=f"{role}-{uuid4().hex[:6]}@x.test", password_hash="x", first_name="A", last_name="B", role=role))
        with pytest.raises(IntegrityError):
            cw.db.commit()
        cw.db.rollback()
    before = len(cw.outbox)
    r = forgot(anonymous(cw), "some-parent@family.test")
    assert r.status_code == 200 and r.json()["message"] == NEUTRAL and len(cw.outbox) == before


@needs_pg
def test_r9_a_console_account_gets_no_letter_and_its_link_is_not_found(cw, centre):
    staff = cw.make_staff(owner=True, email=f"console-{uuid4().hex[:6]}@cn1.test")
    before = len(cw.outbox)
    r = forgot(anonymous(cw), staff.email)
    assert r.status_code == 200 and r.json()["message"] == NEUTRAL
    assert len(cw.outbox) == before and not live_resets(cw, staff.user)
    token, _ = links.issue(cw.db, staff.user.id, "reset")                       # a link made by hand for a console account is still no link
    cw.db.commit()
    c = anonymous(cw)
    r = set_by_link(c, token)
    assert r.status_code == 404 and r.json()["code"] == "link_not_found"


@needs_pg
def test_u1_u2_a_blocked_or_a_removed_person_gets_nothing_and_the_answer_does_not_change(cw, centre):
    blocked = add_member(cw, centre, "teacher", first="Блок")
    assert cw.client.post(f"/api/v1/platform/users/{blocked.id}/block", json={"reason": "тест блокировки"}).status_code == 200
    removed = add_member(cw, centre, "teacher", first="Удалён")
    assert signed_in(cw, centre.owner).delete(f"{AUTH}/org/members/{member_of(cw, removed).id}").status_code == 200
    before = len(cw.outbox)
    for user in (blocked, removed):
        r = forgot(anonymous(cw), user.email)
        assert r.status_code == 200 and r.json()["message"] == NEUTRAL
    assert len(cw.outbox) == before


@needs_pg
def test_t6_u2_the_console_says_removed_not_blocked_and_the_link_of_a_removed_person_says_the_centre_closed_it(cw, centre):
    token_user = add_member(cw, centre, "teacher", first="Ссылка")
    token, _ = links.issue(cw.db, token_user.id, "reset")
    cw.db.commit()
    assert signed_in(cw, centre.owner).delete(f"{AUTH}/org/members/{member_of(cw, token_user).id}").status_code == 200
    r = set_by_link(anonymous(cw), token)
    assert r.status_code == 403 and "отключил центр" in r.json()["detail"] and "ALGO" not in r.json()["detail"]
    sent = cw.client.post(f"/api/v1/platform/users/{token_user.id}/send-reset")
    assert sent.status_code == 409 and "удалён" in sent.json()["detail"] and sent.json()["code"] == "user_removed"
    blocked = add_member(cw, centre, "teacher", first="Блок")
    token2, _ = links.issue(cw.db, blocked.id, "reset")
    cw.db.commit()
    assert cw.client.post(f"/api/v1/platform/users/{blocked.id}/block", json={"reason": "тест блокировки"}).status_code == 200
    r = set_by_link(anonymous(cw), token2)
    assert r.status_code == 403 and "ALGO" in r.json()["detail"]
    sent = cw.client.post(f"/api/v1/platform/users/{blocked.id}/send-reset")
    assert sent.status_code == 409 and sent.json()["code"] == "user_blocked"


@needs_pg
def test_u3_a_person_in_two_centres_gets_his_own_forgot_letter_but_no_centre_can_reset_him(cw, centre):
    two = add_member(cw, centre, "teacher", first="Два")
    other = cw.make_centre()
    cw.db.add(OrganizationMember(org_id=other.org.id, user_id=two.id, org_role="teacher"))
    cw.db.commit()
    assert forgot(anonymous(cw), two.email).status_code == 200 and len(letters_to(cw.outbox, two.email)) == 1
    r = signed_in(cw, centre.owner).post(f"{AUTH}/org/members/{member_of(cw, two, centre.org).id}/password-link")
    assert r.status_code == 404 and r.json()["code"] == "member_not_found"
    assert len(letters_to(cw.outbox, two.email)) == 1


@needs_pg
def test_u4_u5_plus_addresses_are_other_accounts_and_the_case_and_the_spaces_are_ignored(cw, centre):
    base = centre.teacher
    local, _, domain = base.email.partition("@")
    plus = cw.make_user(email=f"{local}+tag@{domain}", role="teacher")
    plus.default_org_id = centre.org.id
    cw.db.add(OrganizationMember(org_id=centre.org.id, user_id=plus.id, org_role="teacher"))
    cw.db.commit()
    forgot(anonymous(cw), f"  {base.email.upper()}  ")
    assert len(letters_to(cw.outbox, base.email)) == 1 and not live_resets(cw, plus)
    assert letters_to(cw.outbox, plus.email) == [] or all(plus.email not in m["To"] for m in letters_to(cw.outbox, base.email))
    forgot(anonymous(cw), plus.email)
    assert len(live_resets(cw, plus)) == 1 and len(live_resets(cw, base)) == 1


@needs_pg
def test_u6_the_answer_asks_to_check_the_address_and_does_not_say_whether_it_exists(cw, centre):
    for address in (centre.teacher.email, "nobody-here@staff.test"):
        text = forgot(anonymous(cw), address).json()["message"]
        assert "Спам" in text and "такой же, как при входе" in text and "зарегистрирована" in text


# ============================================================================================================ inv 1: neutrality


def _shape(response):
    keep = {k: v for k, v in response.headers.items() if k.lower() not in ("date", "x-request-id", "server")}
    return response.status_code, response.json(), keep


@needs_pg
def test_f5_f6_inv1_the_answer_is_the_same_for_every_kind_of_address(cw, centre):
    blocked = add_member(cw, centre, "teacher", first="Блок")
    cw.client.post(f"/api/v1/platform/users/{blocked.id}/block", json={"reason": "тест блокировки"})
    removed = add_member(cw, centre, "teacher", first="Удалён")
    signed_in(cw, centre.owner).delete(f"{AUTH}/org/members/{member_of(cw, removed).id}")
    staff = cw.make_staff(owner=True, email=f"console-{uuid4().hex[:6]}@cn1.test")
    spent = centre.admin.email
    for _ in range(4):                                                       # the 4th is over the mailbox limit (3 per hour)
        forgot(anonymous(cw), spent)
    kinds = {
        "existing": centre.teacher.email, "unknown": "nobody@staff.test", "blocked": blocked.email, "removed": removed.email, "console": staff.email,
        "over the limit": spent, "malformed": "not-an-address", "empty": "", "very long": "a" * 400 + "@x.test",
    }
    shapes = {name: _shape(forgot(anonymous(cw), address)) for name, address in kinds.items()}
    reference = shapes["unknown"]
    assert reference[0] == 200
    for name, shape in shapes.items():
        assert shape == reference, name


@needs_pg
def test_f5_the_request_itself_touches_neither_the_database_nor_the_mail_it_only_queues_the_work(cw, centre, monkeypatch):
    """The page answers before anything is looked up: no database in the handler's dependencies, the letter is a task that has not run yet."""
    def explode():
        raise AssertionError("the database was opened inside the request")

    monkeypatch.setitem(main_v2.app.dependency_overrides, __import__("src.core.database", fromlist=["get_db"]).get_db, explode)
    scope = {"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("203.0.113.9", 4000), "query_string": b""}
    background = BackgroundTasks()
    out = password_reset_router.forgot_password(Request(scope), background, {"email": centre.teacher.email, "turnstile_token": "x"})
    assert out.status_code == 200 and len(background.tasks) == 1
    assert letters_to(cw.outbox, centre.teacher.email) == [] and not live_resets(cw, centre.teacher)
    # an address that does not exist queues the same work: the request cannot tell them apart
    other = BackgroundTasks()
    password_reset_router.forgot_password(Request(scope), other, {"email": "nobody@staff.test", "turnstile_token": "x"})
    assert len(other.tasks) == len(background.tasks) == 1


@needs_pg
def test_f4_a_mail_server_that_fails_changes_nothing_in_the_answer_and_the_log_has_no_address(cw, centre, monkeypatch, caplog):
    from src.mail import sender

    def boom(msg, cfg=None):
        raise ConnectionError("smtp down: " + str(msg["To"]))

    monkeypatch.setattr(sender, "deliver", boom)
    caplog.set_level(logging.INFO)
    r = forgot(anonymous(cw), centre.teacher.email)
    assert r.status_code == 200 and r.json()["message"] == NEUTRAL
    assert centre.teacher.email not in caplog.text and "smtp down" not in caplog.text


# ============================================================================================================ inv 7, F3: the limits


@needs_pg
def test_f3_inv7_the_fourth_request_for_one_mailbox_sends_nothing_and_answers_alike(cw, centre):
    for _ in range(5):
        r = forgot(anonymous(cw), centre.teacher.email)
        assert r.status_code == 200 and r.json()["message"] == NEUTRAL
    assert len(letters_to(cw.outbox, centre.teacher.email)) == 3
    assert len(live_resets(cw, centre.teacher)) == 1


@needs_pg
def test_f3_inv7_the_eleventh_request_from_one_address_and_the_thirty_first_to_one_domain_send_nothing(cw, centre):
    users = []
    for i in range(12):
        u = cw.make_user(email=f"ip-{i}-{uuid4().hex[:5]}@staff.test", role="teacher")
        u.default_org_id = centre.org.id
        cw.db.add(OrganizationMember(org_id=centre.org.id, user_id=u.id, org_role="teacher"))
        users.append(u)
    cw.db.commit()
    c = anonymous(cw)
    for u in users:
        forgot(c, u.email)
    sent = [u for u in users if letters_to(cw.outbox, u.email)]
    assert len(sent) == 10                                                   # the 11th and 12th request of the same client address got nothing


@needs_pg
def test_f3_inv7_the_domain_budget_is_thirty_per_hour(cw, centre, monkeypatch):
    from src.api import rate_limit

    monkeypatch.setattr(reset, "PER_ADDRESS", (1000, 3600))
    users = []
    for i in range(32):
        u = cw.make_user(email=f"dom-{i}-{uuid4().hex[:5]}@one-domain.test", role="teacher")
        u.default_org_id = centre.org.id
        cw.db.add(OrganizationMember(org_id=centre.org.id, user_id=u.id, org_role="teacher"))
        users.append(u)
    cw.db.commit()
    for u in users:
        rate_limit._window_store.pop(f"rl:forgot_ip:testclient", None)
        forgot(anonymous(cw), u.email)
    assert len([u for u in users if letters_to(cw.outbox, u.email)]) == 30


@needs_pg
def test_f3_r3_made_up_addresses_of_a_big_domain_do_not_shut_a_real_user_of_that_domain_out(cw, centre, monkeypatch):
    monkeypatch.setattr(reset, "PER_ADDRESS", (1000, 3600))
    real = cw.make_user(email=f"real-{uuid4().hex[:6]}@gmail.com", role="teacher")
    real.default_org_id = centre.org.id
    cw.db.add(OrganizationMember(org_id=centre.org.id, user_id=real.id, org_role="teacher"))
    cw.db.commit()
    for i in range(40):
        if i == 20:
            from src.api import rate_limit

            rate_limit._memory_store.clear()                                            # (the burst guard of 30 a minute per client address is not what is tested)
        r = forgot(anonymous(cw), f"nobody-{i}-{uuid4().hex[:5]}@gmail.com")
        assert r.status_code == 200 and r.json()["message"] == NEUTRAL
    assert forgot(anonymous(cw), real.email).status_code == 200
    assert len(letters_to(cw.outbox, real.email)) == 1                                  # the 40 made-up requests spent no part of the domain budget


@needs_pg
def test_m2_the_platform_budget_is_150_a_day_then_nothing_goes_and_the_log_warns_once(cw, centre, monkeypatch, caplog):
    assert reset.PER_PLATFORM_DAY == 150
    monkeypatch.setattr(reset, "PER_PLATFORM_DAY", 2)
    monkeypatch.setattr(reset, "PER_ADDRESS", (1000, 3600))
    caplog.set_level(logging.WARNING)
    people = []
    for i in range(4):
        u = cw.make_user(email=f"day-{i}-{uuid4().hex[:5]}@d{i}.test", role="teacher")
        u.default_org_id = centre.org.id
        cw.db.add(OrganizationMember(org_id=centre.org.id, user_id=u.id, org_role="teacher"))
        people.append(u)
    cw.db.commit()
    for u in people:
        r = forgot(anonymous(cw), u.email)
        assert r.status_code == 200 and r.json()["message"] == NEUTRAL
    assert [bool(letters_to(cw.outbox, u.email)) for u in people] == [True, True, False, False]
    assert len([r for r in caplog.records if "daily budget" in r.getMessage()]) == 1


@needs_pg
def test_ts1_the_server_asks_cloudflare_the_same_way_for_every_address(cw, centre, monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret")
    asked = []

    def fake(secret_key, token, remote_ip=None):
        asked.append(token)
        return {"success": token == "good", "hostname": "app.test", "action": "forgot_password"}

    monkeypatch.setattr(turnstile, "_siteverify", fake)
    for address in (centre.teacher.email, "nobody@staff.test"):
        bad = forgot(anonymous(cw), address, token="bad")
        assert bad.status_code == 400 and bad.json()["code"] == "captcha_failed"
        none = anonymous(cw).post(f"{AUTH}/password/forgot", json={"email": address})
        assert none.status_code == 400 and none.json()["code"] == "captcha_failed"
    assert letters_to(cw.outbox, centre.teacher.email) == []
    assert forgot(anonymous(cw), centre.teacher.email, token="good").status_code == 200
    assert len(letters_to(cw.outbox, centre.teacher.email)) == 1


# ============================================================================================================ the link (T1-T5, T8, T10, inv 2, 3)


@needs_pg
def test_t1_the_new_password_closes_every_session_and_every_other_link_signs_in_without_the_tick_and_writes_the_letter(cw, centre):
    teacher_other, _ = login(cw, centre.teacher, remember=True)
    forgot(anonymous(cw), centre.teacher.email)
    token = link_token(cw, centre.teacher.email)
    page = anonymous(cw).get(f"{LINKS}/{token}")
    assert page.status_code == 200 and page.json()["purpose"] == "reset" and page.json()["expires_at"].endswith("Z")
    c = anonymous(cw)
    r = set_by_link(c, token)
    assert r.status_code == 200, r.text
    assert teacher_other.get(f"{AUTH}/me").status_code == 401 and teacher_other.post(f"{AUTH}/refresh", json={}).status_code == 401
    assert c.get(f"{AUTH}/me").status_code == 200                                     # the new session is the only live one
    live = live_sessions(cw, centre.teacher)
    assert len(live) == 1 and live[0].remember is False
    assert not live_resets(cw, centre.teacher)
    assert login(cw, centre.teacher)[1].status_code == 401 and login(cw, centre.teacher, password=NEW_PASSWORD)[1].status_code == 200
    mails = [m for m in letters_to(cw.outbox, centre.teacher.email) if m["Subject"] == "Пароль ALGO изменён"]
    assert len(mails) == 1
    text = body_of(mails[0])
    assert "support@algolab.me" in text or "hello@example.test" in text
    assert "set-password" not in text and "http" not in text.replace("https://algolab.me", "")  # no link with a token in the notice (L9)


@needs_pg
def test_t2_a_dead_link_says_what_to_do_and_a_reset_has_five_minutes_of_grace_for_the_post(cw, centre, monkeypatch):
    forgot(anonymous(cw), centre.teacher.email)
    token = link_token(cw, centre.teacher.email)
    advance_clock(monkeypatch, 3600 + 120)                                           # two minutes past the hour
    c = anonymous(cw)
    page = c.get(f"{LINKS}/{token}")
    assert page.status_code == 410 and page.json()["code"] == "link_expired" and page.json()["purpose"] == "reset"
    assert "на странице входа" in page.json()["detail"] and "ответьте на письмо" not in page.json()["detail"]
    assert set_by_link(c, token).status_code == 200                                  # the form was open when the hour ran out: the POST still works
    forgot_again = centre.admin
    forgot(anonymous(cw), forgot_again.email)
    token2 = link_token(cw, forgot_again.email)
    advance_clock(monkeypatch, 3600 + 120 + 3600 + 6 * 60)                             # more than the grace
    r = set_by_link(anonymous(cw), token2)
    assert r.status_code == 410 and r.json()["code"] == "link_expired"


@needs_pg
def test_t2_the_welcome_link_of_an_owner_does_not_send_him_to_a_page_that_cannot_help(cw, centre):
    token, link = links.issue(cw.db, centre.owner.id, "centre_invite")
    link.expires_at = link.created_at - timedelta(seconds=1)
    cw.db.commit()
    r = anonymous(cw).get(f"{LINKS}/{token}")
    assert r.status_code == 410 and r.json()["purpose"] == "centre_invite" and "Напишите нам" in r.json()["detail"]


@needs_pg
def test_t3_t4_a_used_link_and_a_replaced_link_answer_410_with_their_own_words(cw, centre):
    forgot(anonymous(cw), centre.teacher.email)
    first = link_token(cw, centre.teacher.email)
    forgot(anonymous(cw), centre.teacher.email)
    second = link_token(cw, centre.teacher.email)
    assert first != second
    c = anonymous(cw)
    gone = c.get(f"{LINKS}/{first}")
    assert gone.status_code == 410 and gone.json()["code"] == "link_revoked" and "последнего письма" in gone.json()["detail"]
    assert set_by_link(c, first).status_code == 410
    assert set_by_link(c, second).status_code == 200
    used = anonymous(cw).get(f"{LINKS}/{second}")
    assert used.status_code == 410 and used.json()["code"] == "link_used" and used.json()["purpose"] == "reset"


@needs_pg
def test_t5_a_sign_in_with_the_password_ends_the_live_reset_links_but_not_the_welcome_link(cw, centre):
    welcome, _ = links.issue(cw.db, centre.teacher.id, "centre_invite")                # a welcome link of the same person must survive
    cw.db.commit()
    forgot(anonymous(cw), centre.teacher.email)
    token = link_token(cw, centre.teacher.email)
    assert len(live_resets(cw, centre.teacher)) == 1
    assert login(cw, centre.teacher)[1].status_code == 200
    assert not live_resets(cw, centre.teacher)
    c = anonymous(cw)
    gone = c.get(f"{LINKS}/{token}")
    assert gone.status_code == 410 and gone.json()["code"] == "link_revoked"
    assert c.get(f"{LINKS}/{welcome}").status_code == 200


@needs_pg
def test_t5_the_settings_password_change_ends_a_live_reset_link(cw, centre):
    client, _ = login(cw, centre.teacher)
    forgot(anonymous(cw), centre.teacher.email)
    token = link_token(cw, centre.teacher.email)
    assert anonymous(cw).get(f"{LINKS}/{token}").status_code == 200
    assert client.post(f"{AUTH}/change-password", json={"old_password": PASSWORD, "new_password": NEW_PASSWORD}).status_code == 200
    r = anonymous(cw).get(f"{LINKS}/{token}")
    assert r.status_code == 410 and r.json()["code"] == "link_revoked"
    mails = [m for m in letters_to(cw.outbox, centre.teacher.email) if m["Subject"] == "Пароль ALGO изменён"]
    assert len(mails) == 1                                                            # L9: the notice after a change in the settings too


@needs_pg
def test_t8_forged_cut_or_odd_tokens_are_404_without_an_exception(cw):
    c = anonymous(cw)
    for odd in ("", "x" * 201, "..%2Fx", "a b", "%00", "тест", "' OR 1=1 --", "x" * 43):
        for call in (lambda t: c.get(f"{LINKS}/{t}"), lambda t: set_by_link(c, t)):
            r = call(odd)
            assert r.status_code in (404, 405, 422, 410), (odd, r.status_code)
            assert r.status_code != 500


@needs_pg
def test_t10_inv3_two_posts_at_once_set_the_password_exactly_once(cw, centre):
    forgot(anonymous(cw), centre.teacher.email)
    token = link_token(cw, centre.teacher.email)
    engine = cw.db.get_bind()
    maker = sessionmaker(engine, autoflush=False)
    results = [None, None]
    barrier = threading.Barrier(2)
    scope = {"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("203.0.113.9", 4000), "query_string": b""}

    def go(i):
        db = maker()
        try:
            barrier.wait(timeout=10)
            from src.auth.password_links_router import set_password

            out = set_password(token, Request(scope), BackgroundTasks(), {"password": NEW_PASSWORD + str(i), "password_confirm": NEW_PASSWORD + str(i)}, db)
            results[i] = ("ok", out.status_code)
        except Exception as exc:  # noqa: BLE001
            results[i] = ("err", getattr(exc, "status_code", type(exc).__name__))
            db.rollback()
        finally:
            db.close()

    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert sorted(results) == [("err", 410), ("ok", 200)], results


@needs_pg
def test_inv2_after_many_parallel_issues_exactly_one_live_reset_link_remains_and_the_welcome_link_lives(cw, centre):
    welcome, _ = links.issue(cw.db, centre.teacher.id, "centre_invite")
    cw.db.commit()
    maker = sessionmaker(cw.db.get_bind(), autoflush=False)
    barrier = threading.Barrier(6)
    errors = []

    def go():
        db = maker()
        try:
            barrier.wait(timeout=10)
            links.issue(db, centre.teacher.id, "reset")
            db.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
            db.rollback()
        finally:
            db.close()

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert not errors, errors
    assert len(live_resets(cw, centre.teacher)) == 1
    assert anonymous(cw).get(f"{LINKS}/{welcome}").status_code == 200                # «Добро пожаловать» is not touched by a reset


@needs_pg
def test_b_a_stranger_who_types_the_address_of_a_new_owner_does_not_kill_his_welcome_link(cw, centre):
    welcome, _ = links.issue(cw.db, centre.owner.id, "centre_invite")
    cw.db.commit()
    forgot(anonymous(cw), centre.owner.email)
    assert anonymous(cw).get(f"{LINKS}/{welcome}").status_code == 200
    assert len(live_resets(cw, centre.owner)) == 1


@needs_pg
def test_b_the_link_lock_order_does_not_deadlock_a_sign_in_a_set_password_and_a_new_issue(cw, centre):
    """A sign-in (session lock -> user row), a consuming POST (link row -> session lock -> user row) and an issue (link rows -> session lock) at once: nobody is aborted."""
    forgot(anonymous(cw), centre.teacher.email)
    token = link_token(cw, centre.teacher.email)
    maker = sessionmaker(cw.db.get_bind(), autoflush=False)
    barrier = threading.Barrier(3)
    outcome = {}
    scope = {"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("203.0.113.9", 4000), "query_string": b""}

    def sign_in():
        db = maker()
        try:
            barrier.wait(timeout=10)
            from src.auth.schemas import LoginRequest

            AuthService(db).login(LoginRequest(email=centre.teacher.email, password=PASSWORD))
            outcome["login"] = "ok"
        except Exception as exc:  # noqa: BLE001
            outcome["login"] = type(exc).__name__
        finally:
            db.close()

    def consume():
        db = maker()
        try:
            barrier.wait(timeout=10)
            from src.auth.password_links_router import set_password

            set_password(token, Request(scope), BackgroundTasks(), {"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD}, db)
            outcome["consume"] = "ok"
        except Exception as exc:  # noqa: BLE001
            outcome["consume"] = getattr(exc, "status_code", type(exc).__name__)
            db.rollback()
        finally:
            db.close()

    def issue_new():
        db = maker()
        try:
            barrier.wait(timeout=10)
            links.issue(db, centre.teacher.id, "reset")
            db.commit()
            outcome["issue"] = "ok"
        except Exception as exc:  # noqa: BLE001
            outcome["issue"] = type(exc).__name__
            db.rollback()
        finally:
            db.close()

    threads = [threading.Thread(target=f) for f in (sign_in, consume, issue_new)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert outcome.get("issue") == "ok", outcome
    assert outcome.get("login") in ("ok", "InvalidCredentialsError"), outcome       # (a sign-in that raced the new password is refused, not aborted)
    assert outcome.get("consume") in ("ok", 410), outcome
    assert "DBAPIError" not in str(outcome) and "OperationalError" not in str(outcome), outcome


# ============================================================================================================ C: from the leadership (R4-R7, F7, inv 6, 13)


@needs_pg
def test_r4_the_owner_sends_a_link_to_the_admin_and_to_the_teacher_it_goes_to_the_employee_and_names_the_sender(cw, centre):
    owner = signed_in(cw, centre.owner)
    admin_client, _ = login(cw, centre.admin)
    for target in (centre.admin, centre.teacher):
        r = owner.post(f"{AUTH}/org/members/{member_of(cw, target).id}/password-link")
        assert r.status_code == 200, r.text
        msgs = letters_to(cw.outbox, target.email)
        assert len(msgs) == 1
        text = body_of(msgs[0])
        assert f"«{centre.org.name}»" in text and "Владелец" in text and "Owner Центра" in text
        assert "Пароль не изменится, пока вы сами не зададите новый" in text
        token = link_token(cw, target.email)
        assert token not in r.text and "set-password" not in r.text and "token" not in r.text.lower()   # the leadership never sees the link
    assert admin_client.get(f"{AUTH}/me").status_code == 200                          # his sessions and his password are alive until he chooses a new one
    assert login(cw, centre.admin)[1].status_code == 200
    rows = cw.db.query(MemberAudit).filter(MemberAudit.org_id == centre.org.id, MemberAudit.action == "password_link_sent").all()
    assert {(str(r.actor_id), str(r.user_id)) for r in rows} == {(str(centre.owner.id), str(centre.admin.id)), (str(centre.owner.id), str(centre.teacher.id))}
    for row in rows:
        blob = json.dumps(row.snapshot)
        assert not [token for token in (link_token(cw, centre.admin.email), link_token(cw, centre.teacher.email)) if token in blob]


@needs_pg
def test_r5_the_admin_sends_to_a_teacher_and_the_letter_says_administrator(cw, centre):
    r = signed_in(cw, centre.admin).post(f"{AUTH}/org/members/{member_of(cw, centre.teacher).id}/password-link")
    assert r.status_code == 200
    text = body_of(letters_to(cw.outbox, centre.teacher.email)[0])
    assert "Администратор центра" in text and "Admin Центра" in text


@needs_pg
def test_r6_f7_inv6_the_whole_matrix_actor_by_target_one_404_for_everything_else(cw, centre):
    admin2, teacher2 = add_member(cw, centre, "admin", first="Admin2"), add_member(cw, centre, "teacher", first="Teacher2")
    console = cw.make_staff(owner=True, email=f"console-{uuid4().hex[:6]}@cn1.test")
    cw.db.add(OrganizationMember(org_id=centre.org.id, user_id=console.user.id, org_role="teacher"))
    removed = add_member(cw, centre, "teacher", first="Removed")
    member_of(cw, removed).is_active = False
    two = add_member(cw, centre, "teacher", first="Two")
    other = cw.make_centre()
    cw.db.add(OrganizationMember(org_id=other.org.id, user_id=two.id, org_role="teacher"))
    off = add_member(cw, centre, "teacher", first="Off")
    off.is_active = False
    cw.db.commit()
    mid = lambda u, org=centre.org: str(member_of(cw, u, org).id)  # noqa: E731
    targets = {
        "owner": mid(centre.owner), "admin": mid(centre.admin), "admin2": mid(admin2), "teacher": mid(centre.teacher), "teacher2": mid(teacher2),
        "console": mid(console.user), "removed": mid(removed), "two centres": mid(two), "another centre": mid(other.owner, other.org), "inactive user": mid(off),
        "unknown": str(uuid4()), "not a uuid": "not-a-uuid",
    }
    allowed = {
        "owner": {"admin", "admin2", "teacher", "teacher2"},
        "admin": {"teacher", "teacher2"},
        "teacher": set(),
    }
    clients = {"owner": signed_in(cw, centre.owner), "admin": signed_in(cw, centre.admin), "teacher": signed_in(cw, centre.teacher)}
    refused = set()
    for actor, client in clients.items():
        for name, member_id in targets.items():
            r = client.post(f"{AUTH}/org/members/{member_id}/password-link")
            if name in allowed[actor]:
                assert r.status_code == 200, (actor, name, r.text)
            else:
                assert r.status_code == 404, (actor, name, r.status_code)
                refused.add(json.dumps(r.json(), sort_keys=True))
    assert refused == {json.dumps({"detail": "Сотрудник не найден", "code": "member_not_found"}, sort_keys=True)}   # the same body for every «no»
    assert letters_to(cw.outbox, other.owner.email) == [] and letters_to(cw.outbox, console.email) == []


@needs_pg
def test_r7_the_console_send_reset_to_the_owner_of_a_centre_stays(cw, centre):
    r = cw.client.post(f"/api/v1/platform/users/{centre.owner.id}/send-reset")
    assert r.status_code == 200 and r.json()["email_sent"] is True
    assert "запрошена смена пароля" in body_of(letters_to(cw.outbox, centre.owner.email)[-1])


@needs_pg
def test_c_five_links_an_hour_per_employee_the_sixth_is_429(cw, centre):
    owner = signed_in(cw, centre.owner)
    url = f"{AUTH}/org/members/{member_of(cw, centre.teacher).id}/password-link"
    assert [owner.post(url).status_code for _ in range(6)] == [200, 200, 200, 200, 200, 429]
    assert len(live_resets(cw, centre.teacher)) == 1


@needs_pg
def test_c_a_link_from_the_owner_works_once_like_any_other_and_ends_the_old_sessions(cw, centre):
    other_device, _ = login(cw, centre.teacher)
    signed_in(cw, centre.owner).post(f"{AUTH}/org/members/{member_of(cw, centre.teacher).id}/password-link")
    token = link_token(cw, centre.teacher.email)
    assert set_by_link(anonymous(cw), token).status_code == 200
    assert other_device.get(f"{AUTH}/me").status_code == 401
    assert set_by_link(anonymous(cw), token).status_code == 410


# ============================================================================================================ F: the one rule, the messages


@needs_pg
def test_p8_f10_the_password_rule_is_the_same_on_all_four_forms(cw, centre):
    bad = {
        "seven": "abcdefg", "long bytes": "я" * 37, "nul": "abcd\x00efgh", "empty": "",
    }
    owner = signed_in(cw, centre.owner)
    address = f"p8-{uuid4().hex[:6]}@staff.test"
    invite(owner, address)
    code = invitation_code(cw, address)
    teacher, _ = login(cw, centre.teacher)                                            # (a sign-in ends a live reset link: the link is asked for after it)
    forgot(anonymous(cw), centre.teacher.email)
    token = link_token(cw, centre.teacher.email)
    for name, password in bad.items():
        r1 = accept(anonymous(cw), code, password=password)
        r2 = set_by_link(anonymous(cw), token, password)
        r3 = teacher.post(f"{AUTH}/change-password", json={"old_password": PASSWORD, "new_password": password})
        assert (r1.status_code, r2.status_code, r3.status_code) == (422, 422, 422), (name, r1.text, r2.text, r3.text)
    same = accept(anonymous(cw), code, password=address)                              # equal to the e-mail
    assert same.status_code == 400 and "совпадать с почтой" in same.json()["detail"]
    same2 = teacher.post(f"{AUTH}/change-password", json={"old_password": PASSWORD, "new_password": centre.teacher.email})
    assert same2.status_code == 400 and "совпадать с почтой" in same2.json()["detail"]
    r = set_by_link(anonymous(cw), token, centre.teacher.email)
    assert r.status_code == 422 and "совпадать с почтой" in r.json()["fields"]["password"]
    # eight characters is enough everywhere
    assert teacher.post(f"{AUTH}/change-password", json={"old_password": PASSWORD, "new_password": "abcdefgh"}).status_code == 200
    assert accept(anonymous(cw), code, password="abcdefgh").status_code == 201
    from src.auth import password_rules

    assert password_rules.HINT == "Минимум 8 символов"


@needs_pg
def test_f_the_sign_in_says_blocked_or_closed_only_after_the_right_password(cw, centre):
    blocked = add_member(cw, centre, "teacher", first="Блок")
    cw.client.post(f"/api/v1/platform/users/{blocked.id}/block", json={"reason": "тест блокировки"})
    removed = add_member(cw, centre, "teacher", first="Удалён")
    signed_in(cw, centre.owner).delete(f"{AUTH}/org/members/{member_of(cw, removed).id}")
    for who in (blocked, removed):
        wrong = login(cw, who, password="wrong-password")[1]
        assert wrong.status_code == 401 and wrong.json()["detail"]["message"] == "Неверная почта или пароль"
    assert login(cw, blocked)[1].status_code == 403 and "заблокирован" in login(cw, blocked)[1].json()["detail"]
    closed = login(cw, removed)[1]
    assert closed.status_code == 403 and closed.json()["detail"] == "Доступ закрыт центром. Обратитесь к руководству центра"
    unknown = login(cw, centre.teacher, password="wrong")[1]
    assert unknown.status_code == 401
    nobody = cw.new_client(host=HOST).post(f"{AUTH}/login", json={"email": "nobody@staff.test", "password": "whatever-1"})
    assert nobody.status_code == 401 and nobody.json() == unknown.json() == {"detail": {"code": "INVALID_CREDENTIALS", "message": "Неверная почта или пароль"}}


@needs_pg
def test_f_a_cabinet_password_at_the_console_is_401_like_a_wrong_one_and_counts_as_a_failure(cw, centre):
    console_client = cw.new_client()
    wrong = console_client.post("/api/v1/platform/auth/login", json={"email": centre.owner.email, "password": "not-it-at-all"})
    right = console_client.post("/api/v1/platform/auth/login", json={"email": centre.owner.email, "password": PASSWORD})
    assert wrong.status_code == right.status_code == 401
    assert right.json() == wrong.json() and right.json()["code"] == "invalid_credentials"
    for _ in range(4):
        last = console_client.post("/api/v1/platform/auth/login", json={"email": centre.owner.email, "password": PASSWORD})
    assert last.status_code == 429                                                    # the failures of the right password are counted: one budget, no oracle


# ============================================================================================================ G2: the letters in the background, the status of an invitation (M1)


@needs_pg
def test_m1_the_answer_of_an_invitation_does_not_wait_for_the_letter_and_the_status_follows_it(cw, centre):
    from src.auth import router as auth_router
    from src.auth.ops_schemas import InviteMemberRequestB

    address = f"bg-{uuid4().hex[:6]}@staff.test"
    background = BackgroundTasks()
    user = {"org_id": str(centre.org.id), "user_id": str(centre.owner.id), "org_role": "owner", "role": "owner"}
    out = auth_router.create_invitation(InviteMemberRequestB(email=address, org_role="teacher"), background, user, cw.db)
    assert out["mail_status"] == "pending" and len(background.tasks) == 1 and letters_to(cw.outbox, address) == []   # the answer came first
    row = cw.db.query(Invitation).filter(Invitation.email == address).one()
    assert row.status == "pending" and row.mail_status == "pending"
    owner = signed_in(cw, centre.owner)
    listed = {i["email"]: i for i in owner.get(f"{AUTH}/org/invitations").json()}
    assert listed[address]["mail_status"] == "pending"
    background.tasks[0].func(*background.tasks[0].args)                                         # the task runs after the response
    cw.db.expire_all()
    assert cw.db.query(Invitation).filter(Invitation.email == address).one().mail_status == "sent"
    assert {i["email"]: i for i in owner.get(f"{AUTH}/org/invitations").json()}[address]["mail_status"] == "sent"
    assert len(letters_to(cw.outbox, address)) == 1


@needs_pg
def test_m1_a_first_letter_that_did_not_go_is_marked_failed_in_the_list_and_the_resend_makes_it_sent(cw, centre, monkeypatch):
    from src.mail import sender

    owner = signed_in(cw, centre.owner)
    address = f"fail-{uuid4().hex[:6]}@staff.test"
    monkeypatch.setattr(sender, "deliver", lambda msg, cfg=None: False)
    r = invite(owner, address)
    assert r.status_code == 200 and r.json()["mail_status"] == "pending"                       # what the page saw first
    listed = {i["email"]: i for i in owner.get(f"{AUTH}/org/invitations").json()}
    assert listed[address]["mail_status"] == "failed" and listed[address]["status"] == "pending"
    monkeypatch.setattr(sender, "deliver", lambda msg, cfg=None: cw.outbox.append(msg) or True)
    again = owner.post(f"{AUTH}/org/invitations/{listed[address]['id']}/resend")
    assert again.status_code == 200
    listed = [i for i in owner.get(f"{AUTH}/org/invitations").json() if i["email"] == address and i["status"] == "pending"]
    assert len(listed) == 1 and listed[0]["mail_status"] == "sent" and len(letters_to(cw.outbox, address)) == 1


@needs_pg
def test_m1_m4_a_replacement_that_did_not_go_leaves_the_old_link_alive_and_marks_it_failed(cw, centre, monkeypatch):
    from src.mail import sender

    owner = signed_in(cw, centre.owner)
    address = f"keep-{uuid4().hex[:6]}@staff.test"
    first = invite(owner, address).json()
    old_code = invitation_code(cw, address)
    monkeypatch.setattr(sender, "deliver", lambda msg, cfg=None: False)
    owner.post(f"{AUTH}/org/invitations/{first['id']}/resend")
    stranger = anonymous(cw)
    assert stranger.get(f"{AUTH}/invite/{old_code}").status_code == 200                       # the old link is alive: nothing was cancelled
    rows = {i["id"]: i for i in owner.get(f"{AUTH}/org/invitations").json() if i["email"] == address}
    assert len(rows) == 1 and rows[first["id"]]["mail_status"] == "failed" and rows[first["id"]]["status"] == "pending"
    assert cw.db.query(Invitation).filter(Invitation.email == address, Invitation.status == "queued").count() == 0


@needs_pg
def test_m1_a_replacement_waits_in_the_queue_while_the_letter_is_on_its_way_and_the_old_link_works_until_it_has_gone(cw, centre):
    from src.auth import router as auth_router
    from src.auth.ops_schemas import InviteMemberRequestB

    owner = signed_in(cw, centre.owner)
    address = f"queue-{uuid4().hex[:6]}@staff.test"
    first = invite(owner, address).json()
    old_code = invitation_code(cw, address)
    background = BackgroundTasks()
    user = {"org_id": str(centre.org.id), "user_id": str(centre.owner.id), "org_role": "owner", "role": "owner"}
    auth_router.resend_invitation(first["id"], background, user, cw.db)
    cw.db.expire_all()
    states = sorted((i.status, i.mail_status) for i in cw.db.query(Invitation).filter(Invitation.email == address))
    assert states == [("pending", "pending"), ("queued", "pending")]                       # the old one «sending», the new one waits
    stranger = anonymous(cw)
    assert stranger.get(f"{AUTH}/invite/{old_code}").status_code == 200                       # the old link works while the new letter is on its way
    assert len(owner.get(f"{AUTH}/org/invitations").json()) == 1                              # one row, not two
    background.tasks[0].func(*background.tasks[0].args)
    new_code = invitation_code(cw, address)
    assert stranger.get(f"{AUTH}/invite/{old_code}").status_code == 410
    assert stranger.get(f"{AUTH}/invite/{new_code}").status_code == 200


@needs_pg
def test_m1_a_pending_status_that_nobody_finished_shows_as_failed_after_two_minutes(cw, centre, monkeypatch):
    from src.auth import invites as invites_module

    owner = signed_in(cw, centre.owner)
    address = f"lost-{uuid4().hex[:6]}@staff.test"
    invite(owner, address)
    row = cw.db.query(Invitation).filter(Invitation.email == address).one()
    row.mail_status, row.mail_status_at = "pending", row.created_at - timedelta(seconds=invites_module.MAIL_STALE_SECONDS + 5)
    cw.db.commit()
    assert {i["email"]: i for i in owner.get(f"{AUTH}/org/invitations").json()}[address]["mail_status"] == "failed"


@needs_pg
def test_m1_cancelling_the_old_invitation_also_drops_the_waiting_replacement(cw, centre):
    from src.auth import router as auth_router

    owner = signed_in(cw, centre.owner)
    address = f"cancel-{uuid4().hex[:6]}@staff.test"
    first = invite(owner, address).json()
    background = BackgroundTasks()
    user = {"org_id": str(centre.org.id), "user_id": str(centre.owner.id), "org_role": "owner", "role": "owner"}
    auth_router.resend_invitation(first["id"], background, user, cw.db)
    assert owner.delete(f"{AUTH}/org/invitations/{first['id']}").status_code == 200
    background.tasks[0].func(*background.tasks[0].args)
    cw.db.expire_all()
    assert cw.db.query(Invitation).filter(Invitation.email == address, Invitation.status.in_(("pending", "queued"))).count() == 0


# ============================================================================================================ H: no superadmin


@needs_pg
def test_h1_the_role_superadmin_does_not_exist_cabinet_roles_are_three_and_the_team_is_the_console_only(cw, centre):
    from sqlalchemy.exc import IntegrityError

    cw.db.add(User(email=f"sa-{uuid4().hex[:6]}@x.test", password_hash="x", first_name="A", last_name="B", role="superadmin"))
    with pytest.raises(IntegrityError):
        cw.db.commit()
    cw.db.rollback()
    for table in (User.__table__, __import__("src.models.auth", fromlist=["StudentAudit"]).StudentAudit.__table__, __import__("src.models.auth", fromlist=["GroupAudit"]).GroupAudit.__table__):
        sql = " ".join(str(c.sqltext) for c in table.constraints if hasattr(c, "sqltext") and "role" in str(c.sqltext))
        assert "owner" in sql and "teacher" in sql and "superadmin" not in sql, table.name
    # the two routes that only a superadmin could use are gone
    owner = signed_in(cw, centre.owner)
    assert owner.get(f"{AUTH}/users").status_code in (404, 405)
    assert owner.post(f"{AUTH}/admin/approve/{centre.org.id}", json={}).status_code in (404, 405)
    # the console team works as before: only through the console
    staff = cw.make_staff(owner=True, email=f"team-{uuid4().hex[:6]}@cn1.test")
    assert cw.login(cw.new_client(), staff).status_code == 200
    cabinet = cw.new_client(host=HOST).post(f"{AUTH}/login", json={"email": staff.email, "password": PASSWORD})
    assert cabinet.status_code == 401


@needs_pg
def test_h1_no_code_of_the_cabinet_mentions_the_role():
    import pathlib

    offenders = []
    for path in pathlib.Path("src").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for number, line in enumerate(text.splitlines(), 1):
            if "superadmin" in line.lower():
                offenders.append(f"{path}:{number}")
    assert offenders == [], offenders


# ============================================================================================================ inv 5: no secret leaks (LESSONS 51: every path)


@needs_pg
def test_inv5_the_token_of_a_reset_is_in_no_answer_no_log_no_journal_no_list(cw, centre, caplog):
    caplog.set_level(logging.DEBUG)
    owner = signed_in(cw, centre.owner)
    forgot(anonymous(cw), centre.admin.email)
    token_self = link_token(cw, centre.admin.email)
    assert owner.post(f"{AUTH}/org/members/{member_of(cw, centre.teacher).id}/password-link").status_code == 200
    token_staff = link_token(cw, centre.teacher.email)
    seen = [
        owner.get(f"{AUTH}/org/members").text, owner.get(f"{AUTH}/org/members?status=all").text, owner.get(f"{AUTH}/org/invitations").text,
        anonymous(cw).get(f"{LINKS}/{token_self}").text, set_by_link(anonymous(cw), token_self).text, set_by_link(anonymous(cw), token_self).text,
        anonymous(cw).get(f"{LINKS}/{token_staff}").text, set_by_link(anonymous(cw), token_staff, "short").text,
        forgot(anonymous(cw), centre.admin.email).text,
    ]
    logs = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore", "asyncio")))   # the service's log, not the test client's
    for token in (token_self, token_staff):
        assert not [text for text in seen if token in text]
        assert token not in logs and hash_token(token) not in logs
        assert token not in json.dumps([row.snapshot for row in cw.db.query(MemberAudit).all()])
    for address in (centre.admin.email, centre.teacher.email):
        assert address not in logs, address                                           # no whole address in a log line
    stored = {row.token_hash for row in cw.db.query(PasswordLink)}
    assert hash_token(token_self) in stored and token_self not in stored            # the database keeps the hash only


# ============================================================================================================ LESSONS 69: one lock order on EVERY path (K2-2 R3, 3)


def _parallel(maker, jobs, barrier_size=None):
    """Run each ``job(db)`` in its own thread and session at the same moment; returns {name: 'ok' | the error class | the status code}."""
    barrier = threading.Barrier(barrier_size or len(jobs))
    outcome = {}

    def run(name, job):
        db = maker()
        try:
            barrier.wait(timeout=10)
            job(db)
            outcome[name] = "ok"
        except Exception as exc:  # noqa: BLE001
            outcome[name] = getattr(exc, "status_code", type(exc).__name__)
            db.rollback()
        finally:
            db.close()

    threads = [threading.Thread(target=run, args=item) for item in jobs.items()]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    return outcome


@needs_pg
def test_lock_order_every_path_that_changes_links_sessions_or_the_password_of_one_user_at_once(cw, centre):
    """A sign-in, a link that sets the password, a new link, a change of the password in the settings (twice), a block from the console and a removal of the
    employee all touch the same user: no deadlock, nobody is aborted by the database."""
    from src.auth import platform_users_router as console_users
    from src.auth.schemas import LoginRequest

    forgot(anonymous(cw), centre.teacher.email)
    token = link_token(cw, centre.teacher.email)
    maker = sessionmaker(cw.db.get_bind(), autoflush=False)
    scope = {"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("203.0.113.9", 4000), "query_string": b""}
    teacher_id, org_id, member_id = centre.teacher.id, centre.org.id, member_of(cw, centre.teacher).id

    def sign_in(db):
        AuthService(db).login(LoginRequest(email=centre.teacher.email, password=PASSWORD))

    def consume(db):
        from src.auth.password_links_router import set_password

        set_password(token, Request(scope), BackgroundTasks(), {"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD}, db)

    def issue(db):
        links.issue(db, teacher_id, "reset")
        db.commit()

    def change(db):
        AuthService(db).change_password(str(teacher_id), PASSWORD, "changed-in-settings-1")

    def change_again(db):
        AuthService(db).change_password(str(teacher_id), PASSWORD, "changed-in-settings-2")

    def console_block(db):
        user = console_users._get(db, str(teacher_id), lock=True)
        user_sessions.revoke_all(db, user.id, user_sessions.REVOKE_USER_BLOCKED)
        db.commit()

    def remove(db):
        AuthService(db).remove_member(org_id, member_id, actor_id=str(centre.owner.id))

    outcome = _parallel(maker, {"sign_in": sign_in, "consume": consume, "issue": issue, "change": change, "change_again": change_again, "block": console_block, "remove": remove})
    assert outcome["issue"] == "ok" and outcome["block"] == "ok", outcome
    assert not [name for name, result in outcome.items() if result in ("OperationalError", "DBAPIError", "InternalError")], outcome   # nobody was aborted by a deadlock


@needs_pg
def test_lock_order_a_returning_employee_who_accepts_an_invitation_while_links_are_issued_and_the_console_looks(cw, centre):
    from src.auth import platform_users_router as console_users

    owner = signed_in(cw, centre.owner)
    returning = add_member(cw, centre, "teacher", first="Возврат")
    owner.delete(f"{AUTH}/org/members/{member_of(cw, returning).id}")
    invite(owner, returning.email)
    from tests.test_k2_1_pg import invitation_code

    code = invitation_code(cw, returning.email)
    maker = sessionmaker(cw.db.get_bind(), autoflush=False)

    def accept_it(db):
        from src.auth.schemas import AcceptInviteRequest

        AuthService(db).accept_invitation(AcceptInviteRequest(code=code, email=returning.email, password="returning-pa55", first_name="Т", last_name="Т"))

    def issue(db):
        links.issue(db, returning.id, "reset")
        db.commit()

    def console_look(db):
        console_users._get(db, str(returning.id), lock=True)
        user_sessions.revoke_all(db, returning.id, user_sessions.REVOKE_USER_BLOCKED)
        db.commit()

    outcome = _parallel(maker, {"accept": accept_it, "issue": issue, "console": console_look})
    assert outcome["issue"] == "ok" and outcome["console"] == "ok", outcome
    assert outcome["accept"] in ("ok", "ValueError") and not [r for r in outcome.values() if r in ("OperationalError", "DBAPIError", "InternalError")], outcome


def _against_a_holder_of_the_user_lock(cw, user_id, job, *, pause=0.8, autoflush=True):
    """The deterministic form of the deadlock: A (a sign-in: the per-user lock, then an UPDATE of the user row) holds the lock; B runs *job* meanwhile. With the
    order «user row first, lock second» B has updated the row and waits for the lock, A then waits for the row: the database aborts one of them."""
    import time

    from sqlalchemy import text

    maker = sessionmaker(cw.db.get_bind(), autoflush=autoflush)       # (autoflush on: the pending UPDATE of the user row goes out at the first query - the strictest case)
    holding, result = threading.Event(), {}

    def holder():
        db = maker()
        try:
            user_sessions.lock_user(db, user_id)
            holding.set()
            time.sleep(pause)                                                # B starts and reaches its own UPDATE / lock meanwhile
            db.execute(text("UPDATE users SET login_count = coalesce(login_count, 0) + 1 WHERE id = :u"), {"u": user_id})
            db.commit()
            result["holder"] = "ok"
        except Exception as exc:  # noqa: BLE001
            result["holder"] = type(exc).__name__
            db.rollback()
        finally:
            db.close()

    def other():
        db = maker()
        try:
            holding.wait(10)
            job(db)
            result["job"] = "ok"
        except Exception as exc:  # noqa: BLE001
            result["job"] = type(exc).__name__ if not hasattr(exc, "status_code") else f"http {exc.status_code}"
            db.rollback()
        finally:
            db.close()

    threads = [threading.Thread(target=holder), threading.Thread(target=other)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    return result


DEADLOCK_CLASSES = ("OperationalError", "DBAPIError", "InternalError", "DeadlockDetected")


@needs_pg
def test_lock_order_change_password_waits_for_the_lock_before_it_touches_the_row(cw, centre):
    r = _against_a_holder_of_the_user_lock(cw, centre.teacher.id, lambda db: AuthService(db).change_password(str(centre.teacher.id), PASSWORD, "changed-in-settings-1"))
    assert r == {"holder": "ok", "job": "ok"}, r


@needs_pg
def test_lock_order_a_returning_employee_accepting_waits_for_the_lock_before_it_touches_the_row(cw, centre):
    from tests.test_k2_1_pg import invitation_code

    owner = signed_in(cw, centre.owner)
    returning = add_member(cw, centre, "teacher", first="Возврат2")
    owner.delete(f"{AUTH}/org/members/{member_of(cw, returning).id}")
    invite(owner, returning.email)
    code = invitation_code(cw, returning.email)

    def accept_it(db):
        from src.auth.schemas import AcceptInviteRequest

        AuthService(db).accept_invitation(AcceptInviteRequest(code=code, email=returning.email, password="returning-pa55", first_name="Т", last_name="Т"))

    r = _against_a_holder_of_the_user_lock(cw, returning.id, accept_it)
    assert r == {"holder": "ok", "job": "ok"}, r


@needs_pg
def test_lock_order_the_console_block_and_the_removal_of_an_employee_wait_for_the_lock_first(cw, centre):
    from src.auth import platform_users_router as console_users

    def block(db):
        user = console_users._get(db, str(centre.teacher.id), lock=True)
        user_sessions.revoke_all(db, user.id, user_sessions.REVOKE_USER_BLOCKED)
        db.commit()

    r = _against_a_holder_of_the_user_lock(cw, centre.teacher.id, block)
    assert r == {"holder": "ok", "job": "ok"}, r
    other = add_member(cw, centre, "teacher", first="Удаляемый")
    mid = member_of(cw, other).id
    r = _against_a_holder_of_the_user_lock(cw, other.id, lambda db: AuthService(db).remove_member(centre.org.id, mid, actor_id=str(centre.owner.id)))
    assert r == {"holder": "ok", "job": "ok"}, r


# ============================================================================================================ CSRF of the new mutating routes (K2-2 R3, 4)


@needs_pg
def test_csrf_the_new_routes_of_the_cabinet_refuse_a_request_that_another_site_started(cw, centre):
    from src.core.config import get_settings

    own = get_settings().cors_origins[0]
    owner = signed_in(cw, centre.owner)
    mid = member_of(cw, centre.teacher).id
    calls = (
        (f"{AUTH}/org/members/{mid}/password-link", None),
        (f"{AUTH}/org/members/{mid}/email-change", {"email": f"csrf-{uuid4().hex[:6]}@staff.test"}),
    )
    for url, body in calls:
        for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": own, "Sec-Fetch-Site": "cross-site"}):
            r = owner.post(url, json=body, headers=headers)
            assert r.status_code == 403 and r.json()["detail"] == "Запрос выхода должен приходить с сайта ALGO", (url, headers, r.text)
        assert owner.post(url, json=body, headers={"Origin": own}).status_code == 200, url          # the own Origin passes
    assert len(letters_to(cw.outbox, centre.teacher.email)) == 2                                    # nothing went for the refused ones: one link, one notice


# ============================================================================================================ Turnstile: outage and action (K2-2 R3, 5)


@needs_pg
def test_ts1_r3_the_action_of_the_token_must_be_the_one_of_this_form(cw, centre, monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret")
    answers = {"forgot": {"success": True, "hostname": "app.test", "action": "forgot_password"}, "landing": {"success": True, "hostname": "app.test", "action": "demo_request"},
               "none": {"success": True, "hostname": "app.test"}, "testing": {"success": True, "hostname": "example.com", "metadata": {"result_with_testing_key": True}}}
    monkeypatch.setattr(turnstile, "_siteverify", lambda secret_key, token, remote_ip=None: answers[token])
    results = {name: forgot(anonymous(cw), centre.teacher.email, token=name).status_code for name in answers}
    assert results == {"forgot": 200, "landing": 400, "none": 400, "testing": 200}, results   # a token of the landing's form (or without an action) is refused; the Cloudflare TEST key has no action
    assert turnstile.verify("landing", None) is True                                          # a caller that names no action (the demo form) is as before


@needs_pg
def test_ts1_r3_only_a_network_failure_lets_a_request_through_and_the_log_speaks_once_in_ten_minutes(cw, centre, monkeypatch, caplog):
    import httpx

    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret")
    monkeypatch.setattr(turnstile, "_outage_state", {"last_log": None, "skipped": 0})
    clock = {"now": 1000.0}
    monkeypatch.setattr(turnstile.time, "monotonic", lambda: clock["now"])
    caplog.set_level(logging.WARNING)

    def down(secret_key, token, remote_ip=None):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(turnstile, "_siteverify", down)
    assert [turnstile.verify("t", None, action="forgot_password") for _ in range(5)] == [True] * 5
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "unreachable" in r.getMessage()]
    assert len(errors) == 1 and "1 unchecked" in errors[0].getMessage()                       # the first line at once, the other four are only counted
    clock["now"] += 601
    assert turnstile.verify("t", None) is True
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "unreachable" in r.getMessage()]
    assert len(errors) == 2 and "5 unchecked" in errors[1].getMessage()                       # ... and the next line (10 minutes later) carries the number
    # an answer with an error status, a broken body: refused, not let through
    request = httpx.Request("POST", "https://x.invalid")
    for failure in (httpx.HTTPStatusError("500", request=request, response=httpx.Response(500, request=request)), ValueError("not json")):
        def broken(secret_key, token, remote_ip=None, failure=failure):
            raise failure

        monkeypatch.setattr(turnstile, "_siteverify", broken)
        assert turnstile.verify("t", None, action="forgot_password") is False
    r = forgot(anonymous(cw), centre.teacher.email, token="x")
    assert r.status_code == 400                                                                # the page of «Забыли пароль» too
