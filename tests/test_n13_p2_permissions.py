"""N13 P2: who may touch money, and that money never leaks into other payloads (PostgreSQL)."""
import json
from datetime import date

import pytest

from src.auth.schemas import GroupResponse, StudentResponse, UserResponse
from src.models.auth import Organization, StudentGroup, StudentPayment

from tests.pg_world import (  # noqa: F401  (fixtures)
    business_today, make_client, make_member, make_user, needs_pg, pg_session, world,
)

pytestmark = needs_pg

API = "/api/v1/auth"
SECRET_AMOUNT = 7654321           # recognisable in any JSON
SECRET_FEE = 8765432


@pytest.fixture
def money(world):
    """A fee and a payment in the first group; ids for the parametrised URLs."""
    m = world.db.query(StudentGroup).filter_by(student_id=world.students[0].id, group_id=world.group.id).one()
    r = make_client(world, user=world.owner).put(f"{API}/org/memberships/{m.id}/fee", json={"monthly_fee": SECRET_FEE})
    assert r.status_code == 200, r.text
    r = make_client(world, user=world.owner).post(
        f"{API}/org/memberships/{m.id}/payments",
        json={"amount": SECRET_AMOUNT, "paid_on": business_today().isoformat()},
    )
    assert r.status_code == 201, r.text
    world.membership_id = str(m.id)
    world.payment_id = r.json()["payment"]["id"]
    return world


def allow_admin(w, flag=True):
    w.db.query(Organization).filter_by(id=w.org.id).update({"settings": {"payments": {"admin_can_manage": flag}}})
    w.db.commit()


# (method, path, body, success status, owner_only)
def endpoints(w):
    today = business_today().isoformat()
    m, p = w.membership_id, w.payment_id
    return [
        ("GET", f"{API}/org/settings/payments", None, 200, True),
        ("PUT", f"{API}/org/settings/payments", {"expiring_days": 4}, 200, True),
        ("GET", f"{API}/org/payments/summary", None, 200, False),
        ("GET", f"{API}/org/payments/status", None, 200, False),
        ("GET", f"{API}/org/students/{w.students[0].id}/memberships", None, 200, False),
        ("PUT", f"{API}/org/memberships/{m}/fee", {"monthly_fee": 1}, 200, False),
        ("GET", f"{API}/org/memberships/{m}/payments", None, 200, False),
        ("POST", f"{API}/org/memberships/{m}/payments", {"amount": 5, "paid_on": today}, 201, False),
        ("POST", f"{API}/org/payments/{p}/replace", {"amount": 6, "paid_on": today}, 201, False),
        ("POST", f"{API}/org/payments/{p}/void", {"reason": "проверка прав"}, 200, False),
    ]


IDS = ["settings-get", "settings-put", "summary", "status", "memberships", "fee", "payments-list",
       "payment-create", "payment-replace", "payment-void"]


def call(client, method, path, body):
    return client.request(method, path, json=body) if body is not None else client.request(method, path)


@pytest.mark.parametrize("index", range(len(IDS)), ids=IDS)
class TestEveryMoneyEndpoint:
    def spec(self, money, index):
        return endpoints(money)[index]

    def test_teacher_is_always_403(self, money, index):
        method, path, body, _, _ = self.spec(money, index)
        allow_admin(money)  # even with the admin switch on
        assert call(make_client(money, user=money.teacher), method, path, body).status_code == 403

    def test_admin_is_403_while_the_switch_is_off(self, money, index):
        method, path, body, _, _ = self.spec(money, index)
        assert call(make_client(money, user=money.admin), method, path, body).status_code == 403
        allow_admin(money, False)
        assert call(make_client(money, user=money.admin), method, path, body).status_code == 403
        allow_admin(money, "true")     # only a real boolean true counts
        assert call(make_client(money, user=money.admin), method, path, body).status_code == 403

    def test_admin_with_the_switch_on(self, money, index):
        method, path, body, ok, owner_only = self.spec(money, index)
        allow_admin(money)
        r = call(make_client(money, user=money.admin), method, path, body)
        assert r.status_code == (403 if owner_only else ok), r.text   # settings stay owner-only

    def test_owner_is_always_allowed(self, money, index):
        method, path, body, ok, _ = self.spec(money, index)
        assert call(make_client(money, user=money.owner), method, path, body).status_code == ok
        allow_admin(money, False)       # and the switch does not matter to the owner
        assert call(make_client(money, user=money.owner), method, path, body).status_code in (ok, 409)

    def test_no_organisation_is_403(self, money, index):
        method, path, body, _, _ = self.spec(money, index)
        superadmin = make_user(money.db, "superadmin")
        money.db.commit()
        client = make_client(money, user=superadmin, org=None, role="superadmin")
        r = call(client, method, path, body)
        assert r.status_code == 403
        # a member of nothing at all is refused as well
        loner = make_user(money.db, "admin")
        money.db.commit()
        assert call(make_client(money, user=loner, org=None), method, path, body).status_code == 403

    def test_admin_of_another_organisation_cannot_use_the_switch_of_this_one(self, money, index):
        method, path, body, _, _ = self.spec(money, index)
        allow_admin(money)
        foreign = make_client(money, user=money.foreign_admin, org="other")
        assert call(foreign, method, path, body).status_code in (403, 404)


def test_admin_with_the_switch_can_do_the_whole_flow_and_is_recorded(money):
    allow_admin(money)
    admin = make_client(money, user=money.admin)
    r = admin.post(f"{API}/org/memberships/{money.membership_id}/payments",
                   json={"amount": 100, "paid_on": business_today().isoformat(), "valid_to": date.today().replace(year=date.today().year + 1).isoformat()})
    assert r.status_code == 201 and r.json()["payment"]["created_by"] == str(money.admin.id)


def test_monthly_fee_when_adding_a_student_needs_the_right(money):
    from tests.pg_world import make_group
    target = make_group(money.db, money.org, money.teacher2, "Вторая")
    money.db.commit()
    url = f"{API}/org/groups/{target.id}/students/{money.students[1].id}?mode=add"
    assert make_client(money, user=money.admin).post(url, json={"monthly_fee": 1000}).status_code == 403
    assert make_client(money, user=money.teacher).post(url, json={"monthly_fee": 1000}).status_code == 403
    allow_admin(money)
    assert make_client(money, user=money.admin).post(url, json={"monthly_fee": 1000}).status_code == 200


# ---- money never leaks ---------------------------------------------------------------

MONEY_WORDS = ("fee", "amount", "payment", "paid", "price", "salary", "currency", "money", "balance", "debt")


@pytest.mark.parametrize("model", [StudentResponse, GroupResponse, UserResponse])
def test_response_schemas_have_no_money_fields(model):
    leaked = [name for name in model.model_fields if any(word in name.lower() for word in MONEY_WORDS)]
    assert leaked == []


def test_no_money_in_student_profile_group_roster_lists_or_me(money):
    second = money.students[1]
    urls = [
        f"{API}/org/students",
        f"{API}/org/students/{money.students[0].id}",
        f"{API}/org/students/{money.students[0].id}/profile",
        f"{API}/org/groups",
        f"{API}/org/groups/{money.group.id}",
        f"{API}/org/groups/{money.group.id}/stats",
        f"{API}/org/groups/{money.group.id}/attendance?date_from=2026-01-01&date_to=2026-01-31",
        f"{API}/me",
        f"{API}/org",
    ]
    for user in (money.owner, money.admin, money.teacher):
        client = make_client(money, user=user)
        for url in urls:
            r = client.get(url)
            if user is money.owner and "attendance" not in url:
                assert r.status_code == 200, (url, r.status_code, r.text[:200])   # the check below must really run
            if r.status_code != 200:
                assert r.status_code in (403, 404, 422), (url, r.status_code)
                continue
            text = json.dumps(r.json(), ensure_ascii=False)
            assert str(SECRET_AMOUNT) not in text and str(SECRET_FEE) not in text, (user.role, url)
            assert "monthly_fee" not in text and '"amount"' not in text, (user.role, url)
    # enrolment responses carry no amounts either, even for the owner who set one
    from tests.pg_world import make_group
    target = make_group(money.db, money.org, money.teacher2, "Вторая")
    money.db.commit()
    r = make_client(money, user=money.owner).post(
        f"{API}/org/groups/{target.id}/students/{second.id}?mode=add", json={"monthly_fee": SECRET_FEE}
    )
    assert r.status_code == 200 and str(SECRET_FEE) not in r.text and "monthly_fee" not in r.text
