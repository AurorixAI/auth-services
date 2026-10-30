"""K2-2, E: the change of an employee's e-mail by the owner (E1-E9, invariants 5, 13), the new letters (G), and the cookies of the new flows under the PRODUCTION names."""
import json
import logging
import re
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from src.api import main_v2
from src.auth import password_links as links
from src.mail import letters, render, sender
from src.models.auth import Group, Invitation, MemberAudit, OrganizationMember, User
from src.models.platform import EmailChange

from tests.console_helpers import PASSWORD, advance_clock, body_of, cw, letters_to, needs_pg  # noqa: F401  (cw is a fixture)
from tests.test_k2_1_pg import AUTH, HOST, centre, invite, live_sessions, login, signed_in  # noqa: F401
from tests.test_k2_1_round2_pg import all_set_cookies, prod_names  # noqa: F401
from tests.test_k2_2_reset_pg import add_member, anonymous, forgot, link_token, live_resets, member_of

CHANGE = f"{AUTH}/email-change"


def new_address(tag="new"):
    return f"{tag}-{uuid4().hex[:8]}@staff.test"


def start(client, cw_, member, address):
    return client.post(f"{AUTH}/org/members/{member_of(cw_, member).id}/email-change", json={"email": address})


def confirm_token(cw_, address):
    msgs = letters_to(cw_.outbox, address)
    assert msgs, f"no letter to {address}"
    found = re.findall(r"https://app\.test/confirm-email/([A-Za-z0-9_%-]+)", body_of(msgs[-1]))
    assert len(found) == 1, found
    return found[0]


def server_log(caplog) -> str:
    """What the SERVICE logged (the log of the test client itself, httpx, names every address it calls and is not the service)."""
    return "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore", "asyncio")))


def change_rows(cw_, user):
    cw_.db.expire_all()
    return cw_.db.query(EmailChange).filter(EmailChange.user_id == user.id).order_by(EmailChange.created_at).all()


def current_email(cw_, user):
    cw_.db.expire_all()
    return cw_.db.get(User, user.id).email


# ============================================================================================================ E1-E3: who may


@needs_pg
def test_e1_e3_only_the_owner_asks_for_his_admin_or_teacher_everyone_else_is_refused(cw, centre):
    owner = signed_in(cw, centre.owner)
    admin = signed_in(cw, centre.admin)
    teacher = signed_in(cw, centre.teacher)
    assert start(admin, cw, centre.teacher, new_address()).status_code == 403       # the administrator cannot (the decision of the owner of ALGO, 30.10)
    assert start(teacher, cw, centre.teacher, new_address()).status_code == 403
    assert start(admin, cw, centre.admin, new_address()).status_code == 403
    other = cw.make_centre()
    assert start(owner, cw, other.teacher, new_address()).status_code == 404         # another centre: «no such person»
    assert start(owner, cw, centre.owner, new_address()).status_code == 404          # the owner's own e-mail goes through the team (the console), not here
    removed = add_member(cw, centre, "teacher", first="Удалён")
    owner.delete(f"{AUTH}/org/members/{member_of(cw, removed).id}")
    assert start(owner, cw, removed, new_address()).status_code == 404
    assert owner.post(f"{AUTH}/org/members/{uuid4()}/email-change", json={"email": new_address()}).status_code == 404
    assert owner.post(f"{AUTH}/org/members/not-a-uuid/email-change", json={"email": new_address()}).status_code == 404
    for target in (centre.admin, centre.teacher):
        r = start(owner, cw, target, new_address("ok"))
        assert r.status_code == 200, r.text
        assert set(r.json()) == {"email", "new_email", "expires_at", "message"} and r.json()["email"] == target.email
    two = add_member(cw, centre, "teacher", first="Два")
    cw.db.add(OrganizationMember(org_id=other.org.id, user_id=two.id, org_role="teacher"))
    cw.db.commit()
    assert start(owner, cw, two, new_address()).status_code == 404                   # a person of two centres: one centre cannot move his e-mail


@needs_pg
def test_e_a_console_account_in_a_centre_is_not_reachable(cw, centre):
    staff = cw.make_staff(owner=True, email=f"console-{uuid4().hex[:6]}@cn1.test")
    cw.db.add(OrganizationMember(org_id=centre.org.id, user_id=staff.user.id, org_role="teacher"))
    cw.db.commit()
    assert start(signed_in(cw, centre.owner), cw, staff.user, new_address()).status_code == 404


# ============================================================================================================ E5-E7: the whole chain


@needs_pg
def test_e4_e7_the_chain_old_login_until_the_click_then_the_new_one_with_the_same_user_and_everything_else_ended(cw, centre):
    group = Group(org_id=centre.org.id, name="Группа Е", teacher_id=centre.teacher.id)
    cw.db.add(group)
    cw.db.commit()
    owner = signed_in(cw, centre.owner)
    device, _ = login(cw, centre.teacher)
    old = centre.teacher.email
    user_id = centre.teacher.id
    new = new_address()
    r = start(owner, cw, centre.teacher, new)
    assert r.status_code == 200
    token = confirm_token(cw, new)
    assert token not in r.text and "confirm-email" not in r.text
    notice = body_of(letters_to(cw.outbox, old)[-1])                                  # the OLD address is told: the new one masked
    assert f"{new[0]}***@staff.test" in notice and new not in notice and centre.org.name in notice
    confirm_letter = body_of(letters_to(cw.outbox, new)[-1])
    assert centre.org.name in confirm_letter and "до " in confirm_letter
    # E5: until the click the old address is the login, the new one is nothing
    assert device.get(f"{AUTH}/me").status_code == 200
    assert login(cw, centre.teacher)[1].status_code == 200
    stranger = cw.new_client(host=HOST)
    assert stranger.post(f"{AUTH}/login", json={"email": new, "password": PASSWORD}).status_code == 401
    assert current_email(cw, centre.teacher) == old
    # the page: a GET spends nothing, twice
    page = anonymous(cw).get(f"{CHANGE}/{token}")
    again = anonymous(cw).get(f"{CHANGE}/{token}")
    assert page.status_code == again.status_code == 200
    assert page.json()["new_email"] == new and page.json()["old_email_masked"] == f"{old[0]}***@{old.split('@')[1]}" and old not in page.text
    # the click
    old_link, _ = links.issue(cw.db, user_id, "reset")                                 # a live link that went to the OLD address
    cw.db.commit()
    assert len(live_resets(cw, centre.teacher)) == 1
    done = anonymous(cw).post(f"{CHANGE}/{token}")
    assert done.status_code == 200, done.text
    assert current_email(cw, centre.teacher) == new
    assert cw.db.get(User, user_id).id == user_id and cw.db.get(Group, group.id).teacher_id == user_id    # E4: the same person, his groups stay
    assert device.get(f"{AUTH}/me").status_code == 401 and device.post(f"{AUTH}/refresh", json={}).status_code == 401   # E7: every session ended
    assert stranger.post(f"{AUTH}/login", json={"email": old, "password": PASSWORD}).status_code == 401              # only the new address logs in now
    assert stranger.post(f"{AUTH}/login", json={"email": new, "password": PASSWORD}).status_code == 200
    assert not live_resets(cw, centre.teacher) and anonymous(cw).get(f"{AUTH}/password-links/{old_link}").status_code == 410   # the live links of the old address are over
    assert anonymous(cw).get(f"{CHANGE}/{token}").json()["code"] == "link_used"
    assert anonymous(cw).post(f"{CHANGE}/{token}").status_code == 410
    done_letter = [m for m in letters_to(cw.outbox, new) if m["Subject"] == "Почта в ALGO изменена"]
    assert len(done_letter) == 1 and "прежним паролем" in body_of(done_letter[0])
    actions = [(a.action, str(a.actor_id)) for a in cw.db.query(MemberAudit).filter(MemberAudit.user_id == user_id).order_by(MemberAudit.created_at)]
    assert ("email_change_started", str(centre.owner.id)) in actions and ("email_changed", str(centre.owner.id)) in actions     # inv 13: who -> whom -> when


@needs_pg
def test_e7_the_invitation_that_was_sent_to_the_old_address_is_not_touched(cw, centre):
    owner = signed_in(cw, centre.owner)
    pending_for = new_address("old-invite")
    invite(owner, pending_for)
    row_before = cw.db.query(Invitation).filter(Invitation.email == pending_for).one().status
    start(owner, cw, centre.teacher, new_address())
    confirm = anonymous(cw).post(f"{CHANGE}/{confirm_token(cw, change_rows(cw, centre.teacher)[0].new_email)}")
    assert confirm.status_code == 200
    cw.db.expire_all()
    assert cw.db.query(Invitation).filter(Invitation.email == pending_for).one().status == row_before == "pending"


# ============================================================================================================ E8, E9


@needs_pg
def test_e8_a_taken_address_in_any_case_is_409_without_saying_whose(cw, centre):
    owner = signed_in(cw, centre.owner)
    stranger = cw.make_centre()
    for taken in (stranger.teacher.email, stranger.teacher.email.upper(), centre.admin.email):
        r = start(owner, cw, centre.teacher, taken)
        assert r.status_code == 409 and r.json() == {"detail": "Этот адрес уже используется", "code": "email_taken"}
        assert stranger.org.name not in r.text and "Teacher" not in r.text
    local, _, domain = centre.admin.email.partition("@")
    assert start(owner, cw, centre.teacher, f"{local}+x@{domain}").status_code == 200  # a plus-address is another address
    assert not [r for r in change_rows(cw, centre.teacher) if r.new_email == stranger.teacher.email]


@needs_pg
def test_e8_an_address_taken_between_the_asking_and_the_click_is_409_and_changes_nothing(cw, centre):
    owner = signed_in(cw, centre.owner)
    new = new_address()
    start(owner, cw, centre.teacher, new)
    token = confirm_token(cw, new)
    squatter = cw.make_user(email=new, role="teacher")                                # somebody registered the address meanwhile
    cw.db.commit()
    r = anonymous(cw).post(f"{CHANGE}/{token}")
    assert r.status_code == 409 and r.json()["code"] == "email_taken"
    assert current_email(cw, centre.teacher) == centre.teacher.email and squatter.email == new
    assert login(cw, centre.teacher)[1].status_code == 200


@needs_pg
def test_e9_a_change_that_nobody_confirmed_within_24_hours_changes_nothing(cw, centre, monkeypatch):
    owner = signed_in(cw, centre.owner)
    new = new_address()
    start(owner, cw, centre.teacher, new)
    token = confirm_token(cw, new)
    row = change_rows(cw, centre.teacher)[0]
    assert timedelta(hours=23, minutes=59) < row.expires_at - row.created_at <= timedelta(hours=24)
    advance_clock(monkeypatch, 24 * 3600 + 60)
    page = anonymous(cw).get(f"{CHANGE}/{token}")
    assert page.status_code == 410 and page.json()["code"] == "link_expired"
    assert anonymous(cw).post(f"{CHANGE}/{token}").status_code == 410
    assert current_email(cw, centre.teacher) == centre.teacher.email
    assert login(cw, centre.teacher)[1].status_code == 200


@needs_pg
def test_e_a_new_request_ends_the_earlier_one_and_there_is_one_live_row(cw, centre):
    owner = signed_in(cw, centre.owner)
    first, second = new_address("a"), new_address("b")
    start(owner, cw, centre.teacher, first)
    t1 = confirm_token(cw, first)
    start(owner, cw, centre.teacher, second)
    t2 = confirm_token(cw, second)
    gone = anonymous(cw).get(f"{CHANGE}/{t1}")
    assert gone.status_code == 410 and gone.json()["code"] == "link_revoked"
    assert len([r for r in change_rows(cw, centre.teacher) if r.revoked_at is None and r.used_at is None]) == 1
    assert anonymous(cw).post(f"{CHANGE}/{t2}").status_code == 200 and current_email(cw, centre.teacher) == second


@needs_pg
def test_e_the_address_must_be_an_address_and_a_different_one_and_the_asking_is_limited(cw, centre):
    owner = signed_in(cw, centre.owner)
    for bad in ("", "not-an-address", "a@b", "x" * 300 + "@y.test", 5, None):
        r = owner.post(f"{AUTH}/org/members/{member_of(cw, centre.teacher).id}/email-change", json={"email": bad})
        assert r.status_code == 422, bad
    same = start(owner, cw, centre.teacher, centre.teacher.email.upper())
    assert same.status_code == 422 and "уже текущая" in json.dumps(same.json(), ensure_ascii=False)
    codes = [start(owner, cw, centre.teacher, new_address()).status_code for _ in range(6)]
    assert codes == [200, 200, 200, 200, 200, 429]                                    # five tries an hour for one employee


@needs_pg
def test_e_the_employee_removed_before_the_click_cannot_change_anything(cw, centre):
    owner = signed_in(cw, centre.owner)
    new = new_address()
    start(owner, cw, centre.teacher, new)
    token = confirm_token(cw, new)
    assert owner.delete(f"{AUTH}/org/members/{member_of(cw, centre.teacher).id}").status_code == 200
    r = anonymous(cw).post(f"{CHANGE}/{token}")
    assert r.status_code == 403 and r.json()["code"] == "access_closed"
    assert current_email(cw, centre.teacher) == centre.teacher.email


@needs_pg
def test_e_the_profile_route_takes_no_e_mail_and_changes_nothing_with_one(cw, centre):
    teacher, _ = login(cw, centre.teacher)
    r = teacher.put(f"{AUTH}/me", json={"email": new_address(), "first_name": "Новое"})
    assert r.status_code == 200 and r.json()["email"] == centre.teacher.email and r.json()["first_name"] == "Новое"
    assert current_email(cw, centre.teacher) == centre.teacher.email


# ============================================================================================================ inv 5: no secret leaks


@needs_pg
def test_inv5_the_token_of_the_change_is_in_no_answer_no_log_no_journal_and_the_database_keeps_a_hash(cw, centre, caplog):
    caplog.set_level(logging.DEBUG)
    owner = signed_in(cw, centre.owner)
    new = new_address()
    r = start(owner, cw, centre.teacher, new)
    token = confirm_token(cw, new)
    seen = [r.text, owner.get(f"{AUTH}/org/members").text]
    seen.append(anonymous(cw).get(f"{CHANGE}/{token}").text)
    seen.append(anonymous(cw).post(f"{CHANGE}/{token}").text)
    seen.append(anonymous(cw).get(f"{CHANGE}/{token}").text)
    assert not [text for text in seen if token in text]
    logs = server_log(caplog)
    assert token not in logs and new not in logs and centre.teacher.email not in logs
    row = change_rows(cw, centre.teacher)[0]
    assert row.token_hash == links.hash_token(token) and token not in json.dumps([r_.snapshot for r_ in cw.db.query(MemberAudit).all()])
    for audit in cw.db.query(MemberAudit).filter(MemberAudit.action.in_(("email_change_started", "email_changed"))):
        assert new not in json.dumps(audit.snapshot) and centre.teacher.email not in json.dumps(audit.snapshot)


# ============================================================================================================ cookies of the new flows under the production names


@needs_pg
def test_cookies_the_link_that_sets_a_password_signs_in_under_the_production_names_without_a_lifetime(cw, centre, prod_names):
    forgot(anonymous(cw), centre.teacher.email)
    token = link_token(cw, centre.teacher.email)
    https = TestClient(main_v2.app, base_url="https://app.test")
    r = https.post(f"{AUTH}/password-links/{token}", json={"password": "brand-new-pa55", "password_confirm": "brand-new-pa55"})
    assert r.status_code == 200, r.text
    jar = all_set_cookies(r)
    for name in ("__Host-algo_auth_token", "__Host-algo_refresh_token"):
        morsel = jar[(name, "/")]
        assert morsel["secure"] is True and morsel["httponly"] is True and not morsel["domain"] and not morsel["max-age"] and not morsel["expires"]
    assert https.get(f"{AUTH}/me").status_code == 200
    assert not [k for k, m in jar.items() if "token" in json.dumps(r.json())]
    # and after the e-mail has changed the same browser is signed out
    new = new_address()
    owner = TestClient(main_v2.app, base_url="https://app.test")
    assert owner.post(f"{AUTH}/login", json={"email": centre.owner.email, "password": PASSWORD}).status_code == 200
    assert start(owner, cw, centre.teacher, new).status_code == 200
    assert anonymous(cw).post(f"{CHANGE}/{confirm_token(cw, new)}").status_code == 200
    assert https.get(f"{AUTH}/me").status_code == 401


# ============================================================================================================ G: the new letters


def test_letters_every_new_letter_renders_in_both_forms_names_the_sender_and_never_says_do_not_reply(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://app.test")
    monkeypatch.setenv("MAIL_REPLY_TO", "support@algolab.me")
    when = datetime(2026, 10, 30, 9, 0)
    made = {
        "staff": letters.render_password_reset(
            greeting="Анна", token="T0KEN", email="a@x.uz", kind="staff", expires_at=when, centre_name="Центр «Альфа»", initiator_name="Олег Петров", initiator_role="owner"
        ),
        "staff admin": letters.render_password_reset(greeting="Анна", token="T0KEN", kind="staff", centre_name="Альфа", initiator_name="Ира Смирнова", initiator_role="admin"),
        "self": letters.render_password_reset(greeting="Анна", token="T0KEN", email="a@x.uz", kind="self", expires_at=when, requested_at=when),
        "console": letters.render_password_reset(greeting="Анна", token="T0KEN"),
        "changed": letters.render_password_changed(greeting="Анна", changed_at=when),
        "confirm": letters.render_email_change_confirm(greeting="Анна", centre_name="Альфа", token="T0KEN", expires_at=when),
        "notice": letters.render_email_change_notice(greeting="Анна", centre_name="Альфа", new_email_masked="n***@x.uz"),
        "done": letters.render_email_changed(greeting="Анна", new_email="new@x.uz"),
    }
    for name, rendered in made.items():
        for part in (rendered.text, rendered.html):
            assert "не отвечайте" not in part.lower() and "noreply" not in part.lower() and "{{" not in part, name
        assert rendered.subject and "\n" not in rendered.subject
    assert "Владелец центра «Центр «Альфа»» (Олег Петров) отправил вам ссылку" in made["staff"].text and "Пароль не изменится" in made["staff"].text
    assert "Администратор центра «Альфа» (Ира Смирнова)" in made["staff admin"].text
    assert "на странице входа" in made["self"].text and "14:00" in made["self"].text and "30 октября" in made["self"].text
    assert "запрошена смена пароля в ALGO" in made["console"].text
    assert "support@algolab.me" in made["changed"].text and "http" not in made["changed"].text.replace("https://algolab.me", "")
    assert "https://app.test/confirm-email/T0KEN" in made["confirm"].text and "https://app.test/confirm-email/T0KEN" in made["confirm"].html
    assert "n***@x.uz" in made["notice"].text and "new@x.uz" in made["done"].text
    for kind in ("staff", "self", "console"):
        assert "https://app.test/set-password/T0KEN" in made[kind].text


def test_letters_the_subjects_of_the_new_letters_survive_the_encoding(monkeypatch):
    monkeypatch.setenv("MAIL_REPLY_TO", "support@algolab.me")
    for name in ("password_changed", "email_change_confirm", "email_change_notice", "email_changed"):
        rendered = render.render(name, {"greeting": "А", "changed_at": None, "expires_at": None, "centre_name": "Ц", "new_email_masked": "n***@x.uz", "new_email": "n@x.uz", "support_email": "s@x.uz", "cta_url": "https://x"})
        msg = sender.build_message("a@x.uz", rendered)
        assert str(msg["Subject"]) == rendered.subject and msg["Reply-To"] == "support@algolab.me" and "List-Unsubscribe" not in msg
