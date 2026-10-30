"""Console: centres (metrics, card, block / unblock enforced in the cabinets, limits) and users (search, card, invite, reset, block)."""
import re
from datetime import timedelta
from uuid import uuid4

import pytest

from tests.console_helpers import (  # noqa: F401  (first: it loads the application)
    API, PASSWORD, actions, audit_rows, body_of, cw, letters_to, needs_pg,
)
from src.auth import password_links as links  # noqa: E402
from src.auth.platform_common import utcnow  # noqa: E402
from src.models.auth import Group, GroupAttendance, Organization, OrganizationMember, Student, User, UserSession  # noqa: E402
from src.models.platform import PasswordLink  # noqa: E402

pytestmark = needs_pg
APP = "app.algolab.me"
BLOCKED = "Доступ центра приостановлен. Свяжитесь с ALGO"


def cabinet_login(cw, user, password=PASSWORD):
    client = cw.new_client(APP)
    return client, client.post("/api/v1/auth/login", json={"email": user.email, "password": password})


# ── permissions ──────────────────────────────────────────────────────────────────────────────


def test_centre_rights_are_checked_one_by_one(cw):
    centre = cw.make_centre()
    org_url = f"{API}/orgs/{centre.org.id}"
    blind = cw.signed_in(cw.make_staff(["requests.view"]))
    assert blind.get(f"{API}/orgs").status_code == 403 and blind.get(org_url).status_code == 403
    viewer = cw.signed_in(cw.make_staff(["orgs.view"]))
    assert viewer.get(f"{API}/orgs").status_code == 200 and viewer.get(org_url).status_code == 200
    assert viewer.post(f"{org_url}/block", json={"reason": "нет оплаты"}).status_code == 403
    assert viewer.post(f"{org_url}/unblock", json={}).status_code == 403
    assert viewer.patch(f"{org_url}/limits", json={"max_students": 10}).status_code == 403
    blocker = cw.signed_in(cw.make_staff(["orgs.view", "orgs.block"]))
    assert blocker.patch(f"{org_url}/limits", json={"max_students": 10}).status_code == 403
    assert blocker.post(f"{org_url}/block", json={"reason": "нет оплаты"}).status_code == 200
    manager = cw.signed_in(cw.make_staff(["orgs.manage"]))
    assert manager.patch(f"{org_url}/limits", json={"max_students": 10}).status_code == 200
    assert manager.post(f"{org_url}/unblock", json={}).status_code == 403


# ── list and card ────────────────────────────────────────────────────────────────────────────


def test_the_list_carries_the_health_numbers_from_the_auth_tables(cw):
    tag = uuid4().hex[:8]
    centre = cw.make_centre(f"Метрики Альфа {tag}", members=("owner", "admin", "teacher"), city="Ташкент")
    extra_teacher = cw.make_user(first="Ещё", last="Учитель", role="teacher")
    extra_teacher.default_org_id = centre.org.id
    cw.db.add(OrganizationMember(org_id=centre.org.id, user_id=extra_teacher.id, org_role="teacher"))
    now = utcnow()
    centre.owner.last_login_at = now - timedelta(days=2)
    centre.teacher.last_login_at = now - timedelta(days=20)
    centre.admin.last_login_at = now - timedelta(days=80)
    group = Group(org_id=centre.org.id, name="Группа 1", class_level=9, status="active")
    archived_group = Group(org_id=centre.org.id, name="Старая", class_level=9, status="archived")
    cw.db.add_all([group, archived_group])
    cw.db.flush()
    for i, status in enumerate(("active", "active", "new", "archived")):
        cw.db.add(Student(org_id=centre.org.id, first_name=f"С{i}", last_name="Ученик", class_level=9, status=status))
    other = cw.make_centre(f"Метрики Бета {tag}", members=("owner",), city="Самарканд")
    cw.db.commit()

    j = cw.client.get(f"{API}/orgs", params={"q": f"Метрики Альфа {tag}"}).json()
    assert j["total"] == 1
    row = j["items"][0]
    assert (row["students"], row["teachers"], row["groups"]) == (3, 2, 1)                  # archived students and groups are not counted
    assert (row["active_users_7d"], row["active_users_30d"]) == (1, 2)
    assert row["last_login_at"] == (now - timedelta(days=2)).replace(microsecond=0).isoformat() + "Z"
    assert row["status"] == "active" and row["plan"] is None and row["blocked_at"] is None
    assert row["owner"]["email"] == centre.owner.email and row["max_students"] == 500 and row["max_teachers"] == 50

    alpha, beta = f"Метрики Альфа {tag}", f"Метрики Бета {tag}"
    names = lambda **q: [i["name"] for i in cw.client.get(f"{API}/orgs", params={"q": tag, **q}).json()["items"]]  # noqa: E731
    assert names(sort="students", order="desc") == [alpha, beta]
    assert names(sort="name", order="asc") == [alpha, beta]
    assert names(sort="last_login_at", order="desc")[0] == alpha
    assert names(city="самарканд") == [beta]
    assert set(names(status="active")) == {alpha, beta}
    assert cw.client.get(f"{API}/orgs", params={"sort": "owner"}).status_code == 422
    assert cw.client.get(f"{API}/orgs", params={"status": "weird"}).status_code == 422
    assert cw.client.get(f"{API}/orgs", params={"page_size": 1, "q": tag}).json()["pages"] == 2


def test_the_card_has_members_usage_and_history(cw):
    centre = cw.make_centre(f"Карточка {uuid4().hex[:6]}")
    group = Group(org_id=centre.org.id, name="Г", class_level=9, status="active")
    cw.db.add(group)
    cw.db.flush()
    today = utcnow().date()
    for i, status in enumerate(("completed", "completed", "in_progress", "completed")):
        day = today - timedelta(days=(1 if i < 2 else 40 if i == 3 else 2))
        cw.db.add(GroupAttendance(org_id=centre.org.id, group_id=group.id, lesson_date=day.isoformat(), lesson_id=f"l{i}", status=status))
    cw.db.commit()
    j = cw.client.get(f"{API}/orgs/{centre.org.id}").json()
    assert j["name"].startswith("Карточка ") and j["phone"] == "+998901112233" and j["blocked_reason"] is None
    assert j["usage_30d"] == {"attendance_marks": 2, "payments": 0, "exams": None, "diagnostics": None}
    assert {m["org_role"] for m in j["members"]} == {"owner", "admin", "teacher"} and all(m["email"] for m in j["members"])
    assert all("password" not in str(k).lower() for m in j["members"] for k in m)
    assert j["history"] == []
    assert cw.client.get(f"{API}/orgs/{uuid4()}").status_code == 404 and cw.client.get(f"{API}/orgs/xx").status_code == 404


# ── block / unblock ──────────────────────────────────────────────────────────────────────────


def test_a_blocked_centre_is_closed_for_owner_admin_and_teacher_and_open_again_after_unblock(cw):
    tag = uuid4().hex[:8]
    centre = cw.make_centre(f"Блок {tag}")
    inside = {role: cabinet_login(cw, getattr(centre, role))[0] for role in ("owner", "admin", "teacher")}
    assert all(c.get("/api/v1/auth/me").status_code == 200 for c in inside.values())
    outsider = cw.make_centre(f"Чужой {tag}")
    outsider_client, _ = cabinet_login(cw, outsider.owner)

    r = cw.client.post(f"{API}/orgs/{centre.org.id}/block", json={"reason": "Неоплата счёта"})
    assert r.status_code == 200 and r.json()["status"] == "blocked" and r.json()["blocked_at"].endswith("Z")
    for role, client in inside.items():                                                       # every request of a signed-in member
        denied = client.get("/api/v1/auth/me")
        assert denied.status_code == 403 and denied.json()["detail"] == BLOCKED, role
        assert client.get("/api/v1/auth/org").status_code == 403
    for role in ("owner", "admin", "teacher"):                                                # a new sign-in
        client, r = cabinet_login(cw, getattr(centre, role))
        assert r.status_code == 403 and r.json()["detail"] == BLOCKED, role
    assert outsider_client.get("/api/v1/auth/me").status_code == 200                           # another centre is untouched

    card = cw.client.get(f"{API}/orgs/{centre.org.id}").json()
    assert card["status"] == "blocked" and card["blocked_reason"] == "Неоплата счёта"
    assert cw.client.get(f"{API}/orgs", params={"status": "blocked", "q": tag}).json()["total"] == 1
    assert cw.client.get(f"{API}/orgs", params={"status": "active", "q": tag}).json()["total"] == 1       # only the other centre

    r = cw.client.post(f"{API}/orgs/{centre.org.id}/unblock", json={"reason": "Оплатили"})
    assert r.status_code == 200 and r.json()["status"] == "active" and r.json()["blocked_at"] is None
    assert all(c.get("/api/v1/auth/me").status_code == 200 for c in inside.values())
    assert cabinet_login(cw, centre.owner)[1].status_code == 200

    rows = audit_rows(cw.db, target_type="org", target_id=str(centre.org.id))
    assert [r.action for r in rows] == ["org.blocked", "org.unblocked"]
    assert rows[0].reason == "Неоплата счёта" and rows[0].actor_user_id == cw.owner.user.id and rows[0].ip
    assert [h["action"] for h in reversed(card["history"])] == ["org.blocked"]


def test_block_needs_a_reason_and_is_not_repeated(cw):
    centre = cw.make_centre()
    url = f"{API}/orgs/{centre.org.id}"
    assert cw.client.post(f"{url}/block", json={}).status_code == 422
    assert cw.client.post(f"{url}/block", json={"reason": "ab"}).status_code == 422
    assert cw.client.post(f"{url}/unblock", json={}).json()["code"] == "not_blocked"
    assert cw.client.post(f"{url}/block", json={"reason": "Нарушение условий"}).status_code == 200
    again = cw.client.post(f"{url}/block", json={"reason": "Нарушение условий"})
    assert again.status_code == 409 and again.json()["code"] == "already_blocked"
    assert len(audit_rows(cw.db, "org.blocked", target_id=str(centre.org.id))) == 1


# ── limits ───────────────────────────────────────────────────────────────────────────────────


def test_limits_change_and_warn_when_below_the_current_numbers(cw):
    centre = cw.make_centre(f"Лимиты {uuid4().hex[:6]}")
    for i in range(4):
        cw.db.add(Student(org_id=centre.org.id, first_name=f"Л{i}", last_name="У", class_level=9, status="active"))
    cw.db.commit()
    url = f"{API}/orgs/{centre.org.id}/limits"
    r = cw.client.patch(url, json={"max_students": 100, "max_teachers": 7})
    assert r.status_code == 200 and r.json()["warnings"] == [] and (r.json()["org"]["max_students"], r.json()["org"]["max_teachers"]) == (100, 7)
    r = cw.client.patch(url, json={"max_students": 3})
    assert r.status_code == 200 and r.json()["org"]["max_students"] == 3
    assert r.json()["warnings"] == ["Лимит учеников (3) ниже текущего числа учеников (4)"]
    for bad in ({}, {"max_students": 0}, {"max_students": "много"}, {"max_teachers": 10**6}):
        assert cw.client.patch(url, json=bad).status_code == 422, bad
    rows = audit_rows(cw.db, "org.limits_changed", target_id=str(centre.org.id))
    assert len(rows) == 2 and rows[1].details["before"]["max_students"] == 100 and rows[1].details["after"]["max_students"] == 3
    assert rows[1].details["warnings"]


# ── users ────────────────────────────────────────────────────────────────────────────────────


def test_users_search_filters_and_no_staff_in_the_list(cw):
    tag = uuid4().hex[:8]
    name = f"Пользователи Центр {tag}"
    centre = cw.make_centre(name)
    centre.owner.last_login_at = utcnow()
    cw.db.commit()
    get = lambda **q: cw.client.get(f"{API}/users", params=q).json()  # noqa: E731
    by_email = get(q=centre.owner.email)
    assert by_email["total"] == 1 and by_email["items"][0]["org"]["name"] == name
    item = by_email["items"][0]
    assert item["role"] == "owner" and item["org"]["org_role"] == "owner" and item["status"] == "active" and item["blocked"] is False
    assert get(q=tag)["total"] == 3 and get(q=tag, role="teacher")["total"] == 1
    assert get(q=tag, status="never_logged_in")["total"] == 2
    assert get(org_id=str(centre.org.id))["total"] == 3
    assert get(q="+998901112233", org_id=str(centre.org.id))["total"] == 3                    # the phone of the centre
    assert get(q=cw.owner.email)["total"] == 0                                                 # console staff are not cabinet users
    assert cw.client.get(f"{API}/users/{cw.owner.user.id}").status_code == 404
    assert cw.client.get(f"{API}/users", params={"status": "weird"}).status_code == 422
    assert [i["email"] for i in get(q=tag, sort="email", order="asc")["items"]] == sorted(i["email"] for i in get(q=tag)["items"])


def test_the_user_card_never_shows_a_password(cw):
    centre = cw.make_centre(f"Карточка Пользователя {uuid4().hex[:6]}")
    token, link = links.issue(cw.db, centre.owner.id, "centre_invite")
    cw.db.commit()
    r = cw.client.get(f"{API}/users/{centre.owner.id}")
    j = r.json()
    assert r.status_code == 200 and j["email"] == centre.owner.email and j["memberships"][0]["org_name"].startswith("Карточка Пользователя")
    assert j["pending_link"]["purpose"] == "centre_invite" and j["block"] is None and j["active_sessions"] == 0
    flat = str(j).lower()
    assert "hash" not in flat and "$2b$" not in flat and token not in flat and "password" not in flat.replace("pending_link", "")


def test_user_rights(cw):
    centre = cw.make_centre()
    uid = centre.teacher.id
    blind = cw.signed_in(cw.make_staff(["orgs.view"]))
    assert blind.get(f"{API}/users").status_code == 403
    viewer = cw.signed_in(cw.make_staff(["users.view"]))
    assert viewer.get(f"{API}/users/{uid}").status_code == 200
    for path, body in (("send-reset", None), ("resend-invite", None), ("block", {"reason": "тест"}), ("unblock", {})):
        assert viewer.post(f"{API}/users/{uid}/{path}", json=body or {}).status_code == 403, path
    manager = cw.signed_in(cw.make_staff(["users.manage"]))
    assert manager.post(f"{API}/users/{uid}/send-reset").status_code == 200
    assert manager.post(f"{API}/users/{uid}/block", json={"reason": "тест"}).status_code == 403


def test_resend_invite_for_an_owner_who_never_signed_in(cw):
    centre = cw.make_centre(f"Приглашение {uuid4().hex[:6]}", members=("owner", "teacher"))
    assert centre.org.name.startswith("Приглашение")
    old_token, old = links.issue(cw.db, centre.owner.id, "centre_invite")
    centre.owner.last_login_at = None
    cw.db.commit()
    r = cw.client.post(f"{API}/users/{centre.owner.id}/resend-invite")
    assert r.status_code == 200 and r.json()["email_sent"] is True
    mail = letters_to(cw.outbox, centre.owner.email)[-1]
    new_token = re.search(r"/set-password/([\w-]+)", body_of(mail)).group(1)
    assert new_token != old_token and centre.org.name in body_of(mail)
    assert cw.new_client(APP).get(f"/api/v1/auth/password-links/{old_token}").json()["code"] == "link_revoked"     # the new link burns the old one
    assert cw.new_client(APP).get(f"/api/v1/auth/password-links/{new_token}").status_code == 200
    rows = audit_rows(cw.db, "user.invite_resent", target_id=str(centre.owner.id))
    assert len(rows) == 1 and new_token not in str(rows[0].details)

    assert cw.client.post(f"{API}/users/{centre.teacher.id}/resend-invite").json()["code"] == "not_owner"
    centre.owner.last_login_at = utcnow()
    cw.db.commit()
    assert cw.client.post(f"{API}/users/{centre.owner.id}/resend-invite").json()["code"] == "already_active"
    assert cw.client.post(f"{API}/users/{uuid4()}/resend-invite").status_code == 404


def test_send_reset_makes_a_one_hour_link_and_one_journal_row(cw):
    centre = cw.make_centre()
    r = cw.client.post(f"{API}/users/{centre.admin.id}/send-reset")
    assert r.status_code == 200
    assert len(audit_rows(cw.db, "user.reset_sent", target_id=str(centre.admin.id))) == 1
    assert cw.client.get(f"{API}/users/{centre.admin.id}").json()["pending_link"]["purpose"] == "reset"
    second = cw.client.post(f"{API}/users/{centre.admin.id}/send-reset")
    assert second.status_code == 200
    assert cw.db.query(PasswordLink).filter(PasswordLink.user_id == centre.admin.id, PasswordLink.revoked_at.is_(None)).count() == 1


def test_block_closes_the_sign_in_and_the_open_sessions_and_unblock_reopens(cw):
    centre = cw.make_centre()
    client, login = cabinet_login(cw, centre.teacher)
    refresh = client.cookies.get("algo_refresh_token")
    assert client.get("/api/v1/auth/me").status_code == 200
    friend, _ = cabinet_login(cw, centre.admin)

    r = cw.client.post(f"{API}/users/{centre.teacher.id}/block", json={"reason": "Увольнение"})
    assert r.status_code == 200 and r.json()["status"] == "blocked" and r.json()["blocked"] is True
    assert client.get("/api/v1/auth/me").status_code == 401                                    # the open session is closed
    stale = cw.new_client(APP)
    stale.cookies.set("algo_refresh_token", refresh)
    assert stale.post("/api/v1/auth/refresh", json={}).status_code == 401
    closed = cabinet_login(cw, centre.teacher)[1]
    assert closed.status_code == 403 and "заблокирован" in closed.json()["detail"]             # the sign-in is closed (K2-2, F: the RIGHT password is told why)
    assert cabinet_login(cw, centre.teacher, password="wrong-password")[1].status_code == 401   # a wrong one is told nothing
    assert friend.get("/api/v1/auth/me").status_code == 200                                    # a colleague is untouched
    cw.db.expire_all()
    assert cw.db.query(UserSession).filter(UserSession.user_id == centre.teacher.id, UserSession.revoked_at.is_(None)).count() == 0
    assert cw.db.query(UserSession).filter(UserSession.user_id == centre.teacher.id, UserSession.revoke_reason == "user_blocked").count() >= 1
    card = cw.client.get(f"{API}/users/{centre.teacher.id}").json()
    assert card["block"]["reason"] == "Увольнение" and card["status"] == "blocked"
    assert cw.client.post(f"{API}/users/{centre.teacher.id}/block", json={"reason": "ещё раз"}).json()["code"] == "already_blocked"
    assert cw.client.post(f"{API}/users/{centre.teacher.id}/send-reset").json()["code"] == "user_blocked"
    assert cw.client.post(f"{API}/users/{centre.teacher.id}/block", json={}).status_code == 422

    r = cw.client.post(f"{API}/users/{centre.teacher.id}/unblock", json={})
    assert r.status_code == 200 and r.json()["status"] != "blocked"
    assert cabinet_login(cw, centre.teacher)[1].status_code == 200
    assert [r.action for r in audit_rows(cw.db, target_type="user", target_id=str(centre.teacher.id))] == ["user.blocked", "user.unblocked"]


def test_unblock_does_not_reactivate_a_user_the_centre_switched_off(cw):
    centre = cw.make_centre()
    centre.teacher.is_active = False
    cw.db.commit()
    r = cw.client.post(f"{API}/users/{centre.teacher.id}/unblock", json={})
    assert r.status_code == 409 and r.json()["code"] == "not_blocked"
    cw.db.expire_all()
    assert cw.db.get(User, centre.teacher.id).is_active is False
