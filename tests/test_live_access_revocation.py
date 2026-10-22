"""Regression coverage for live staff access checks.

The signed access token is deliberately not the authority for tenant role or
membership: these tests protect the auth ``/me`` contract consumed by exam and
diagnostic services on every protected request.
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException


os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")


def _query_with(value):
    query = MagicMock()
    query.filter.return_value.first.return_value = value
    return query


def _resolve(*, token_role="admin", user_role="admin", member_role="admin", member=True, org=True, user=True):
    from src.auth.dependencies import get_current_active_user_payload

    user_id = uuid4()
    org_id = uuid4()
    payload = {
        "sub": str(user_id),
        "role": token_role,
        "org_id": str(org_id),
        "org_role": token_role,
        "type": "access",
    }
    current_user = (
        SimpleNamespace(id=user_id, role=user_role, is_active=True)
        if user
        else None
    )
    current_member = SimpleNamespace(org_role=member_role) if member else None
    current_org = SimpleNamespace(id=org_id, status="active") if org else None
    db = MagicMock()
    db.query.side_effect = [
        _query_with(current_user),
        _query_with(current_member),
        _query_with(current_org),
    ]
    return get_current_active_user_payload(payload=payload, db=db), db


def test_live_membership_role_overrides_stale_jwt_claim():
    resolved, _ = _resolve(token_role="admin", user_role="teacher", member_role="teacher")

    assert resolved["role"] == "teacher"
    assert resolved["org_role"] == "teacher"


def test_removed_member_loses_access_before_jwt_expiry():
    with pytest.raises(HTTPException) as exc:
        _resolve(member=False)

    assert exc.value.status_code == 403
    assert exc.value.detail == "Доступ к организации отозван"


def test_disabled_account_loses_access_before_jwt_expiry():
    with pytest.raises(HTTPException) as exc:
        _resolve(user=False)

    assert exc.value.status_code == 401
    assert exc.value.detail == "Учётная запись отключена или не найдена"


def test_suspended_organisation_loses_access_before_jwt_expiry():
    with pytest.raises(HTTPException) as exc:
        _resolve(org=False)

    assert exc.value.status_code == 403
    assert exc.value.detail == "Организация недоступна"


def test_org_checker_does_not_trust_a_global_admin_role_for_tenant_access():
    from src.auth.dependencies import require_org_admin

    stale_global_admin = {
        "sub": str(uuid4()),
        "role": "admin",
        "org_id": str(uuid4()),
        "org_role": "teacher",
    }

    with pytest.raises(HTTPException) as exc:
        require_org_admin(payload=stale_global_admin)

    assert exc.value.status_code == 403


def test_only_one_logout_route_is_registered():
    from src.auth.router import router

    logout_routes = [
        route
        for route in router.routes
        if getattr(route, "path", None) == "/auth/logout" and "POST" in route.methods
    ]
    assert len(logout_routes) == 1


def test_removing_member_revokes_its_refresh_token():
    from src.auth.service import AuthService
    from src.models.auth import OrganizationMember, User

    db = MagicMock()
    member = SimpleNamespace(id=uuid4(), org_id=uuid4(), org_role="teacher", user_id=uuid4(), is_active=True)
    user = SimpleNamespace(id=uuid4(), is_active=True)

    def locked(value):
        query = _query_with(value)
        chain = query.filter.return_value.with_for_update.return_value  # LOGIC-B: the member / user rows are locked
        chain.first.return_value = value
        chain.populate_existing.return_value.first.return_value = value
        return query

    def query(model):
        if model is OrganizationMember:
            return locked(member)
        if model is User:
            return locked(user)
        raise AssertionError(f"Unexpected model: {model}")

    db.query.side_effect = query
    with patch("src.auth.service.user_sessions.revoke_all") as revoke_all, \
            patch("src.auth.staff_lifecycle.StaffLifecycle._lock_groups_of", return_value=[]), \
            patch("src.auth.staff_lifecycle.StaffLifecycle._in_progress", return_value=[]), \
            patch("src.auth.staff_lifecycle.StaffLifecycle._audit"), \
            patch("src.auth.lesson_teachers.LessonTeachers.release_substitutions_of", return_value=0), \
            patch("src.auth.lesson_substitutions.on_member_removed", return_value=(0, 0)):
        out = AuthService(db).remove_member(str(member.org_id), str(member.id))
    assert out == {"message": "Сотрудник удалён", "releasedSubstitutions": 0}
    assert member.is_active is False
    assert user.is_active is False
    revoke_all.assert_called_once_with(db, user.id, "user_blocked")  # every session of the removed user ends
    db.commit.assert_called_once()
