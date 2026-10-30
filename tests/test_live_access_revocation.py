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


def _resolve(*, token_role="admin", user_role="admin", member_role="admin", member=True, org=True, user=True, blocked=False):
    from src.auth.dependencies import get_current_active_user_payload

    user_id = uuid4()
    org_id = uuid4()
    payload = {
        "sub": str(user_id),
        "role": token_role,
        "org_id": str(org_id),
        "org_role": token_role,
        "type": "access",
        "sid": str(uuid4()),  # K2-1 (A5): the session check reads the database through ``db.execute`` (a MagicMock here: "alive")
    }
    current_user = (
        SimpleNamespace(id=user_id, role=user_role, is_active=True)
        if user
        else None
    )
    current_member = SimpleNamespace(org_role=member_role) if member else None
    current_org = SimpleNamespace(id=org_id, status="active", blocked_at=("2026-10-25" if blocked else None)) if org else None
    db = MagicMock()
    db.query.side_effect = [
        _query_with(current_user),
        _query_with(current_member),
        _query_with(current_org),
    ]
    # This unit isolates the ROLE / MEMBERSHIP logic: the session check (K2-1, A5) is stated alive here and is exercised against a real database in the
    # ``_pg`` tests below (revoked, expired, someone else's session, no ``sid``).
    with patch("src.auth.user_sessions.is_alive", return_value=True):
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


def test_an_organisation_blocked_from_the_console_loses_access_at_once():
    with pytest.raises(HTTPException) as exc:
        _resolve(blocked=True)

    assert exc.value.status_code == 403
    assert exc.value.detail == "Доступ центра приостановлен. Свяжитесь с ALGO"


def test_suspended_organisation_loses_access_before_jwt_expiry():
    with pytest.raises(HTTPException) as exc:
        _resolve(org=False)

    assert exc.value.status_code == 403
    assert exc.value.detail == "Центр не активен. Обратитесь в ALGO"


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


# ---- the same dependency against a REAL database: the session named by ``sid`` decides (K2-1, A5) ----

from datetime import timedelta  # noqa: E402

from tests.pg_world import make_member, make_org, make_user, needs_pg, pg_session  # noqa: E402,F401  (fixtures)


def _live_world(db):
    from src.auth import user_sessions
    from src.auth.dependencies import get_current_active_user_payload

    org = make_org(db)
    user = make_user(db, "teacher")
    user.default_org_id = org.id
    make_member(db, org, user, "teacher")
    sid = uuid4()
    user_sessions.issue(db, user.id, f"hash-{sid}", timedelta(days=14), remember=True, session_id=sid)
    db.commit()

    def resolve(session_id=sid, who=user):
        payload = {"sub": str(who.id), "role": "teacher", "org_id": str(org.id), "org_role": "teacher", "type": "access"}
        if session_id is not None:
            payload["sid"] = str(session_id)
        return get_current_active_user_payload(payload=payload, db=db)

    return SimpleNamespace(db=db, org=org, user=user, sid=sid, resolve=resolve)


@needs_pg
def test_pg_a_live_session_passes_and_keeps_the_org_and_role(pg_session):
    w = _live_world(pg_session)
    resolved = w.resolve()
    assert resolved["org_id"] == str(w.org.id) and resolved["org_role"] == "teacher"


@needs_pg
def test_pg_a_revoked_expired_foreign_or_missing_session_is_a_401(pg_session):
    from src.auth import user_sessions
    from src.models.auth import UserSession

    w = _live_world(pg_session)
    other = make_user(pg_session, "teacher")
    pg_session.commit()
    for label, kwargs in (("no sid", {"session_id": None}), ("unknown sid", {"session_id": uuid4()}), ("another user's session", {"who": other})):
        with pytest.raises(HTTPException) as exc:
            w.resolve(**kwargs)
        assert exc.value.status_code == 401, label
    row = pg_session.get(UserSession, w.sid)
    row.expires_at = user_sessions._now() - timedelta(seconds=1)
    pg_session.commit()
    with pytest.raises(HTTPException) as exc:
        w.resolve()
    assert exc.value.status_code == 401                                      # expired
    row.expires_at = user_sessions._now() + timedelta(days=1)
    pg_session.commit()
    assert w.resolve()["org_id"]
    user_sessions.revoke_by_id(pg_session, w.user.id, w.sid)
    pg_session.commit()
    with pytest.raises(HTTPException) as exc:
        w.resolve()
    assert exc.value.status_code == 401                                      # revoked: refused at once, no cache


@needs_pg
def test_pg_the_membership_and_the_centre_are_still_checked_live_after_the_session(pg_session):
    from src.models.auth import Organization, OrganizationMember

    w = _live_world(pg_session)
    member = pg_session.query(OrganizationMember).filter_by(user_id=w.user.id).one()
    member.is_active = False
    pg_session.commit()
    with pytest.raises(HTTPException) as exc:
        w.resolve()
    assert exc.value.status_code == 403
    member.is_active = True
    pg_session.get(Organization, w.org.id).blocked_at = user_sessions_now()
    pg_session.commit()
    with pytest.raises(HTTPException) as exc:
        w.resolve()
    assert exc.value.status_code == 403 and "приостановлен" in exc.value.detail


def user_sessions_now():
    from src.auth import user_sessions

    return user_sessions._now()


@needs_pg
def test_pg_the_session_check_is_exactly_one_read_of_user_sessions_per_request(pg_session):
    """Counted, not asserted from the source: the statements the dependency sends that touch ``user_sessions``."""
    from sqlalchemy import event

    w = _live_world(pg_session)
    seen = []

    def count(conn, cursor, statement, parameters, context, executemany):
        if "user_sessions" in statement:
            seen.append(statement.split()[0].upper())

    engine = pg_session.get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        for _ in range(3):
            w.resolve()
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert seen == ["SELECT"] * 3
