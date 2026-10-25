"""Console: staff, roles and permissions as data; the owner rules; the journal list and its privacy."""
from uuid import uuid4

import pytest

from tests.console_helpers import (  # noqa: F401  (first: it loads the application)
    API, PASSWORD, actions, audit_rows, cw, letters_to, needs_pg,
)
from src.auth import platform_permissions as perms  # noqa: E402
from src.models.auth import User  # noqa: E402
from src.models.platform import PlatformAudit, PlatformRole, PlatformSession, PlatformStaff  # noqa: E402

pytestmark = needs_pg
LONG = "a-long-enough-password-1"


def role_id(cw, name):
    return str(cw.db.query(PlatformRole).filter_by(name=name).one().id)


# ── permissions per role ─────────────────────────────────────────────────────────────────────

ENDPOINTS = {  # right -> (method, path)
    "requests.view": ("get", "/demo-requests"),
    "orgs.view": ("get", "/orgs"),
    "users.view": ("get", "/users"),
    "audit.view": ("get", "/audit"),
}


@pytest.mark.parametrize("granted", ["requests.view", "orgs.view", "users.view", "audit.view"])
def test_every_list_opens_only_with_its_own_right(cw, granted):
    client = cw.signed_in(cw.make_staff([granted]))
    for right, (method, path) in ENDPOINTS.items():
        r = getattr(client, method)(f"{API}{path}")
        assert (r.status_code == 200) == (right == granted), (granted, path, r.status_code)
        if right != granted:
            assert r.json() == {"detail": "Недостаточно прав", "code": "forbidden"}


def test_a_role_without_the_centres_right_is_403_on_the_centres(cw):
    sales = cw.signed_in(cw.make_staff(["requests.view", "requests.manage"]))
    assert sales.get(f"{API}/demo-requests").status_code == 200 and sales.get(f"{API}/orgs").status_code == 403


def test_the_team_is_for_the_owner_only_even_if_a_role_lists_the_right(cw):
    sneaky = cw.make_staff(["team.manage", "orgs.view"])                     # a role row that carries the key by hand
    c = cw.signed_in(sneaky)
    assert c.get(f"{API}/staff").status_code == 403 and c.get(f"{API}/roles").status_code == 403
    assert c.post(f"{API}/roles", json={"name": "x", "permissions": []}).status_code == 403
    assert "team.manage" not in c.get(f"{API}/auth/me").json()["permissions"]
    assert c.get(f"{API}/permissions").status_code == 200                      # the catalogue is for any signed-in member


def test_the_owner_has_every_right_and_the_catalogue_is_served(cw):
    me = cw.client.get(f"{API}/auth/me").json()
    assert me["permissions"] == list(perms.ALL_KEYS) and me["role"]["name"] == "Владелец платформы"
    cat = cw.client.get(f"{API}/permissions").json()["items"]
    assert [x["key"] for x in cat] == list(perms.ALL_KEYS) and all(x["label"] and x["group"] for x in cat)
    assert {x["key"] for x in cat if not x["active"]} >= {"impersonate.view", "content.view", "finance.view"}


# ── roles ────────────────────────────────────────────────────────────────────────────────────


def test_template_roles_exist_and_the_owner_role_is_locked(cw):
    items = cw.client.get(f"{API}/roles").json()["items"]
    names = {r["name"]: r for r in items}
    assert {"Владелец платформы", "Продажи", "Поддержка", "Контент", "Финансы"} <= set(names)
    assert names["Владелец платформы"]["is_system"] is True and names["Владелец платформы"]["permissions"] == list(perms.ALL_KEYS)
    assert names["Продажи"]["is_template"] is True and "orgs.block" not in names["Продажи"]["permissions"]
    owner_role = names["Владелец платформы"]["id"]
    assert cw.client.patch(f"{API}/roles/{owner_role}", json={"name": "Другое"}).json()["code"] == "system_role"
    assert cw.client.delete(f"{API}/roles/{owner_role}").json()["code"] == "system_role"
    assert audit_rows(cw.db, "role.updated") == [] and audit_rows(cw.db, "role.deleted") == []


def test_create_edit_and_delete_a_role_with_a_journal_row_each(cw):
    r = cw.client.post(f"{API}/roles", json={"name": "Аналитики", "permissions": ["orgs.view", "audit.view", "orgs.view"]})
    assert r.status_code == 201 and r.json()["permissions"] == ["orgs.view", "audit.view"] and r.json()["is_template"] is False
    rid = r.json()["id"]
    assert cw.client.post(f"{API}/roles", json={"name": "аналитики", "permissions": []}).json()["code"] == "name_taken"
    r = cw.client.patch(f"{API}/roles/{rid}", json={"permissions": ["orgs.view", "orgs.block"]})
    assert r.status_code == 200 and r.json()["permissions"] == ["orgs.view", "orgs.block"]
    assert cw.client.patch(f"{API}/roles/{rid}", json={"name": "Продажи"}).json()["code"] == "name_taken"
    assert cw.client.patch(f"{API}/roles/{rid}", json={"permissions": ["orgs.view", "orgs.block"]}).status_code == 200   # no change: no row
    assert cw.client.delete(f"{API}/roles/{rid}").json() == {"ok": True}
    assert cw.client.delete(f"{API}/roles/{rid}").status_code == 404
    assert [(a.action, a.details.get("name") or a.details.get("changes", {}).get("permissions", {}).get("to")) for a in audit_rows(cw.db, target_type="role")] == [
        ("role.created", "Аналитики"), ("role.updated", ["orgs.view", "orgs.block"]), ("role.deleted", "Аналитики"),
    ]


def test_role_validation(cw):
    for body in ({"name": "", "permissions": []}, {"name": "x", "permissions": ["no.such"]}, {"name": "x", "permissions": "orgs.view"},
                 {"name": "x" * 81, "permissions": []}, {"permissions": []}, {"name": "x"}):
        assert cw.client.post(f"{API}/roles", json=body).status_code == 422, body
    r = cw.client.post(f"{API}/roles", json={"name": "Тайный", "permissions": ["team.manage"]})
    assert r.status_code == 422 and r.json()["code"] == "owner_only_permission"


def test_a_role_in_use_cannot_be_deleted_and_a_change_of_rights_applies_at_once(cw):
    role = cw.client.post(f"{API}/roles", json={"name": "Менеджеры", "permissions": ["orgs.view"]}).json()
    staff = cw.client.post(f"{API}/staff", json={"email": f"m-{uuid4().hex[:6]}@cn1.test", "first_name": "Мария", "last_name": "М", "role_id": role["id"], "password": LONG})
    assert staff.status_code == 201
    assert cw.client.delete(f"{API}/roles/{role['id']}").json()["code"] == "role_in_use"
    c = cw.new_client()
    assert c.post(f"{API}/auth/login", json={"email": staff.json()["email"], "password": LONG}).status_code == 200
    c.post(f"{API}/auth/change-password", json={"current_password": LONG, "new_password": "my-own-new-password-2"})
    assert c.get(f"{API}/orgs").status_code == 200 and c.get(f"{API}/users").status_code == 403
    cw.client.patch(f"{API}/roles/{role['id']}", json={"permissions": ["users.view"]})
    assert c.get(f"{API}/orgs").status_code == 403 and c.get(f"{API}/users").status_code == 200


# ── staff ────────────────────────────────────────────────────────────────────────────────────


def test_the_owner_creates_a_staff_member_who_must_change_the_password(cw):
    mail = f"anna-{uuid4().hex[:8]}@cn1.test"
    body = {"email": mail.replace("anna", "Anna").upper().replace("@CN1.TEST", "@CN1.test"), "first_name": "Анна", "last_name": "Продажова", "role_id": role_id(cw, "Продажи"), "password": LONG}
    r = cw.client.post(f"{API}/staff", json=body)
    assert r.status_code == 201
    j = r.json()
    assert j["email"] == mail and j["role"]["name"] == "Продажи" and j["is_owner"] is False and j["must_change_password"] is True
    assert "password" not in str(j).lower().replace("must_change_password", "")
    user = cw.db.query(User).filter_by(email=mail).one()
    assert user.password_hash != LONG and user.role == "teacher" and user.default_org_id is None
    rows = audit_rows(cw.db, "staff.created", target_id=str(user.id))
    assert len(rows) == 1 and LONG not in str(rows[0].details) and rows[0].details["role"] == "Продажи"

    c = cw.new_client()
    login = c.post(f"{API}/auth/login", json={"email": mail, "password": LONG})
    assert login.status_code == 200 and login.json()["must_change_password"] is True and login.json()["permissions"] == ["requests.view", "requests.manage", "orgs.view"]
    assert c.get(f"{API}/demo-requests").json()["code"] == "must_change_password"
    # the cabinets refuse the same account
    assert cw.new_client("app.algolab.me").post("/api/v1/auth/login", json={"email": mail, "password": LONG}).status_code == 401


def test_staff_creation_validation(cw):
    base = {"email": f"s-{uuid4().hex[:6]}@cn1.test", "first_name": "А", "last_name": "Б", "role_id": role_id(cw, "Контент"), "password": LONG}
    for over, field in (({"email": "nope"}, "email"), ({"password": "short"}, "password"), ({"first_name": ""}, "first_name"),
                        ({"role_id": str(uuid4())}, "role_id"), ({"role_id": "x"}, "role_id"), ({"password": base["email"]}, "password")):
        r = cw.client.post(f"{API}/staff", json={**base, **over})
        assert r.status_code == 422 and field in r.json()["fields"], over
    cabinet = cw.make_centre()
    r = cw.client.post(f"{API}/staff", json={**base, "email": cabinet.owner.email})
    assert r.status_code == 409 and r.json()["code"] == "email_in_use"
    assert cw.client.post(f"{API}/staff", json=base).status_code == 201
    assert cw.client.post(f"{API}/staff", json=base).json()["code"] == "email_in_use"


def test_the_owner_role_makes_an_owner_and_a_second_owner_may_be_demoted_but_not_the_last(cw):
    r = cw.client.post(f"{API}/staff", json={"email": f"o2-{uuid4().hex[:6]}@cn1.test", "first_name": "Второй", "last_name": "Владелец",
                                              "role_id": role_id(cw, "Продажи"), "password": LONG, "is_owner": True})
    assert r.status_code == 201 and r.json()["is_owner"] is True and r.json()["role"]["name"] == "Владелец платформы"
    second = r.json()["user_id"]
    r = cw.client.patch(f"{API}/staff/{second}", json={"role_id": role_id(cw, "Продажи")})
    assert r.status_code == 200 and r.json()["is_owner"] is False                                 # two owners: one may demote the other
    r = cw.client.patch(f"{API}/staff/{second}", json={"is_owner": True})
    assert r.status_code == 200 and r.json()["is_owner"] is True
    # the second owner tries to demote the first: allowed (the second stays); the first then cannot be demoted by himself
    c2 = cw.new_client()
    c2.post(f"{API}/auth/login", json={"email": r.json()["email"], "password": LONG})
    c2.post(f"{API}/auth/change-password", json={"current_password": LONG, "new_password": "second-owner-long-password"})
    demote_last = c2.patch(f"{API}/staff/{cw.owner.user.id}", json={"role_id": role_id(cw, "Контент")})
    assert demote_last.status_code == 200
    # now the second owner is the only one
    only = c2.patch(f"{API}/staff/{second}", json={"role_id": role_id(cw, "Контент")})
    assert only.status_code == 409 and only.json()["code"] == "self_change"
    cw.db.expire_all()
    staff = cw.db.query(PlatformStaff).filter_by(user_id=cw.owner.user.id).one()
    assert staff.is_owner is False


def test_an_owner_cannot_change_his_own_role_or_switch_himself_off(cw):
    me = str(cw.owner.user.id)
    for body in ({"is_active": False}, {"role_id": role_id(cw, "Контент")}, {"is_owner": False}):
        r = cw.client.patch(f"{API}/staff/{me}", json=body)
        assert r.status_code == 409 and r.json()["code"] == "self_change", body
    assert cw.client.patch(f"{API}/staff/{me}", json={"first_name": "Переименован"}).status_code == 200      # a name is not a right
    cw.db.expire_all()
    staff = cw.db.query(PlatformStaff).filter_by(user_id=cw.owner.user.id).one()
    assert staff.is_owner and staff.is_active


def test_a_second_owner_may_be_demoted_by_the_first_but_the_first_then_stays_alone(cw):
    r = cw.client.post(f"{API}/staff", json={"email": f"o2-{uuid4().hex[:6]}@cn1.test", "first_name": "Второй", "last_name": "Владелец",
                                              "role_id": role_id(cw, "Продажи"), "password": LONG, "is_owner": True})
    assert r.status_code == 201 and r.json()["is_owner"] is True and r.json()["role"]["name"] == "Владелец платформы"
    second = r.json()["user_id"]
    r = cw.client.patch(f"{API}/staff/{second}", json={"role_id": role_id(cw, "Продажи")})
    assert r.status_code == 200 and r.json()["is_owner"] is False and r.json()["role"]["name"] == "Продажи"
    assert cw.client.patch(f"{API}/staff/{second}", json={"is_owner": True}).json()["role"]["name"] == "Владелец платформы"
    assert cw.client.patch(f"{API}/staff/{second}", json={"is_active": False}).status_code == 200
    from src.auth import platform_team_router as team

    assert team._other_active_owners(cw.db, cw.owner.user.id) == 0          # the first owner is the only active one now
    assert team._other_active_owners(cw.db, second) == 1


def test_two_owners_demoting_each_other_at_once_leave_one_owner(cw):
    """The last-owner rule holds under a race: both requests pass the sign-in check, the owner lock serves them one after the other."""
    import threading

    from sqlalchemy.orm import sessionmaker

    from src.api import main_v2
    from src.core.database import get_db

    other = cw.make_staff(owner=True)
    a, b = cw.client, cw.signed_in(other)
    factory = sessionmaker(cw.engine, autoflush=False)

    def per_request_session():
        s = factory()
        try:
            yield s
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    main_v2.app.dependency_overrides[get_db] = per_request_session
    gate = threading.Barrier(2)
    results = {}

    def demote(name, client, target):
        gate.wait()
        results[name] = client.patch(f"{API}/staff/{target}", json={"role_id": role_id(cw, "Контент")})

    threads = [threading.Thread(target=demote, args=("a", a, other.user.id)), threading.Thread(target=demote, args=("b", b, cw.owner.user.id))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    codes = sorted(r.status_code for r in results.values())
    assert codes.count(200) == 1, [(k, r.status_code, r.text) for k, r in results.items()]      # exactly one wins
    cw.db.expire_all()
    owners = cw.db.query(PlatformStaff).filter(PlatformStaff.is_owner.is_(True), PlatformStaff.is_active.is_(True)).count()
    assert owners == 1


def test_staff_update_and_switch_off_revokes_the_sessions(cw):
    member = cw.make_staff(["orgs.view"])
    c = cw.signed_in(member)
    assert c.get(f"{API}/orgs").status_code == 200
    r = cw.client.patch(f"{API}/staff/{member.user.id}", json={"role_id": role_id(cw, "Контент"), "first_name": "Новое"})
    assert r.status_code == 200 and r.json()["role"]["name"] == "Контент" and r.json()["first_name"] == "Новое"
    assert c.get(f"{API}/orgs").status_code == 403
    off = cw.client.patch(f"{API}/staff/{member.user.id}", json={"is_active": False})
    assert off.status_code == 200 and off.json()["is_active"] is False
    cw.db.expire_all()
    assert cw.db.query(PlatformSession).filter(PlatformSession.user_id == member.user.id, PlatformSession.revoked_at.is_(None)).count() == 0
    assert cw.login(cw.new_client(), member).json()["code"] == "staff_inactive"
    assert cw.client.patch(f"{API}/staff/{member.user.id}", json={"is_active": True}).json()["is_active"] is True
    assert cw.login(cw.new_client(), member).status_code == 200
    rows = audit_rows(cw.db, "staff.updated", target_id=str(member.user.id))
    assert len(rows) == 3 and rows[0].details["changes"]["role"]["to"] == "Контент"
    assert cw.client.patch(f"{API}/staff/{member.user.id}", json={}).status_code == 422
    assert cw.client.patch(f"{API}/staff/{uuid4()}", json={"is_active": True}).status_code == 404


def test_reset_of_a_staff_password_forces_a_change_and_ends_the_sessions(cw):
    member = cw.make_staff(["orgs.view"])
    c = cw.signed_in(member)
    r = cw.client.post(f"{API}/staff/{member.user.id}/reset-password", json={"password": LONG})
    assert r.status_code == 200
    assert c.get(f"{API}/orgs").status_code == 401
    again = cw.login(cw.new_client(), member, LONG)
    assert again.status_code == 200 and again.json()["must_change_password"] is True
    assert cw.login(cw.new_client(), member).status_code == 401
    assert cw.client.post(f"{API}/staff/{member.user.id}/reset-password", json={"password": "short"}).status_code == 422
    assert cw.client.post(f"{API}/staff/{cw.owner.user.id}/reset-password", json={"password": LONG}).json()["code"] == "self_change"
    rows = audit_rows(cw.db, "staff.password_reset", target_id=str(member.user.id))
    assert len(rows) == 1 and LONG not in str(rows[0].details)
    # a locked member is unlocked by the reset
    for _ in range(5):
        cw.new_client().post(f"{API}/auth/login", json={"email": member.email, "password": "bad-bad-bad-bad"}, headers={"X-Real-IP": f"192.0.2.{_ + 40}"})
    cw.client.post(f"{API}/staff/{member.user.id}/reset-password", json={"password": LONG})
    cw.db.expire_all()
    assert cw.db.query(PlatformStaff).filter_by(user_id=member.user.id).one().locked_until is None


# ── the journal ──────────────────────────────────────────────────────────────────────────────


def test_the_journal_lists_filters_and_paginates(cw):
    tag = uuid4().hex[:8]
    centre = cw.make_centre(f"Журнал {tag}")
    cw.client.post(f"{API}/orgs/{centre.org.id}/block", json={"reason": "Нарушение условий"})
    cw.client.post(f"{API}/orgs/{centre.org.id}/unblock", json={})
    cw.client.patch(f"{API}/orgs/{centre.org.id}/limits", json={"max_students": 77})
    j = cw.client.get(f"{API}/audit").json()
    assert j["total"] >= 4 and j["items"][0]["created_at"] >= j["items"][-1]["created_at"]
    row = next(i for i in j["items"] if i["action"] == "org.blocked")
    assert row["label"] == "Центр заблокирован" and row["target_type"] == "org" and row["target_label"] == f"Журнал {tag}"
    assert row["actor"]["email"] == cw.owner.email and row["reason"] == "Нарушение условий" and row["ip"] is not None
    only = cw.client.get(f"{API}/audit", params={"action": "org.blocked,org.unblocked", "target_id": str(centre.org.id)}).json()
    assert [i["action"] for i in only["items"]] == ["org.unblocked", "org.blocked"]
    assert cw.client.get(f"{API}/audit", params={"actor_id": str(cw.owner.user.id), "target_type": "org"}).json()["total"] == 3
    assert cw.client.get(f"{API}/audit", params={"q": "Нарушение"}).json()["total"] == 1
    assert cw.client.get(f"{API}/audit", params={"page_size": 2, "page": 2}).json()["page"] == 2
    assert cw.client.get(f"{API}/audit", params={"action": "made.up"}).status_code == 422
    acts = cw.client.get(f"{API}/audit/actions").json()["items"]
    assert {"action": "org.blocked", "label": "Центр заблокирован"} in acts


def test_secrets_never_reach_the_journal(cw):
    from src.auth import platform_audit as audit

    cw.db.rollback()
    entry = audit.record(cw.db, actor_id=None, action="staff.created", details={"password": "hunter2hunter2", "nested": {"token": "abc", "ok": 1}, "new_password_hash": "x"})
    assert entry.details == {"password": "[скрыто]", "nested": {"token": "[скрыто]", "ok": 1}, "new_password_hash": "[скрыто]"}
    with pytest.raises(ValueError):
        audit.record(cw.db, actor_id=None, action="not.an.action")
    cw.db.rollback()


def test_every_write_of_the_console_leaves_exactly_one_row(cw):
    centre = cw.make_centre()
    before = cw.db.query(PlatformAudit).count()
    steps = [
        ("post", f"{API}/orgs/{centre.org.id}/block", {"reason": "Тестовая блокировка"}, "org.blocked"),
        ("post", f"{API}/orgs/{centre.org.id}/unblock", {}, "org.unblocked"),
        ("patch", f"{API}/orgs/{centre.org.id}/limits", {"max_teachers": 9}, "org.limits_changed"),
        ("post", f"{API}/users/{centre.teacher.id}/send-reset", {}, "user.reset_sent"),
        ("post", f"{API}/users/{centre.teacher.id}/block", {"reason": "Тест"}, "user.blocked"),
        ("post", f"{API}/users/{centre.teacher.id}/unblock", {}, "user.unblocked"),
        ("post", f"{API}/roles", {"name": f"Р{uuid4().hex[:4]}", "permissions": []}, "role.created"),
    ]
    for method, url, body, action in steps:
        n = cw.db.query(PlatformAudit).count()
        r = getattr(cw.client, method)(url, json=body)
        assert r.status_code in (200, 201), (url, r.text)
        cw.db.expire_all()
        assert cw.db.query(PlatformAudit).count() == n + 1, url
        assert cw.db.query(PlatformAudit).order_by(PlatformAudit.created_at.desc()).first().action == action
    # and a refused write leaves nothing
    n = cw.db.query(PlatformAudit).count()
    cw.client.post(f"{API}/orgs/{centre.org.id}/unblock", json={})
    cw.client.post(f"{API}/roles", json={"name": "", "permissions": []})
    cw.db.expire_all()
    assert cw.db.query(PlatformAudit).count() == n and before >= 0
