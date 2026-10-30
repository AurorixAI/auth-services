"""Nits N1 (#138): GET /auth/me says whether the user may see money, so the client does not probe the payments summary (PostgreSQL)."""
import pytest

from src.auth.schemas import UserResponse

from tests.pg_world import make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_payments_service_pg import API, set_settings


def me(w, **who):
    r = make_client(w, **who).get(f"{API}/me")
    assert r.status_code == 200, r.text
    return r.json()


def test_the_field_is_optional_in_the_schema_so_older_producers_and_clients_keep_working():
    fields = UserResponse.model_fields
    assert fields["payments_access"].is_required() is False
    assert fields["payments_access"].default is None


@needs_pg
def test_owner_always_has_payments_access(world):
    assert me(world, user=world.owner)["payments_access"] is True
    set_settings(world, admin_can_manage=False)            # the switch is about administrators only
    assert me(world, user=world.owner)["payments_access"] is True


@needs_pg
def test_admin_has_it_only_after_the_owner_allowed_it(world):
    assert me(world, user=world.admin)["payments_access"] is False            # no payments block at all
    set_settings(world, admin_can_manage=False)
    assert me(world, user=world.admin)["payments_access"] is False
    set_settings(world, admin_can_manage=True)
    assert me(world, user=world.admin)["payments_access"] is True
    set_settings(world, admin_can_manage=False)            # the owner took it back in the middle of the session: the next profile says so
    assert me(world, user=world.admin)["payments_access"] is False


@needs_pg
def test_admin_flag_must_be_exactly_true(world):
    set_settings(world, admin_can_manage="yes")
    assert me(world, user=world.admin)["payments_access"] is False


@needs_pg
def test_teacher_never_has_it_even_when_admins_do(world):
    set_settings(world, admin_can_manage=True)
    assert me(world, user=world.teacher)["payments_access"] is False
    assert me(world, user=world.teacher2)["payments_access"] is False


@needs_pg
def test_nobody_without_a_centre_has_it(world):
    # a token without an organisation (a student account, a platform account)
    assert me(world, user=world.owner, org=None)["payments_access"] is False
    assert me(world, user=world.admin, org=None, role="teacher")["payments_access"] is False


@needs_pg
def test_the_right_is_per_organisation(world):
    set_settings(world, admin_can_manage=True)             # the first centre allows it; the foreign admin sits in another one
    assert me(world, user=world.foreign_admin, org="other")["payments_access"] is False
    assert me(world, user=world.admin)["payments_access"] is True


@needs_pg
def test_it_agrees_with_the_payments_endpoints(world):
    """The profile and the summary endpoint are one rule: the client may skip the probe only if the probe would be refused."""
    for flag in (False, True):
        set_settings(world, admin_can_manage=flag)
        for user in (world.owner, world.admin, world.teacher):
            client = make_client(world, user=user)
            granted = client.get(f"{API}/org/payments/summary").status_code == 200
            assert client.get(f"{API}/me").json()["payments_access"] is granted, (user.role, flag)


@needs_pg
def test_profile_fields_stay_and_put_me_carries_the_field_too(world):
    body = me(world, user=world.owner)
    for key in ("id", "email", "full_name", "role", "org_role", "org_name", "must_change_password"):
        assert key in body
    r = make_client(world, user=world.admin).put(f"{API}/me", json={"first_name": "Рустам"})
    assert r.status_code == 200, r.text
    assert r.json()["first_name"] == "Рустам"
    assert r.json()["payments_access"] is False
