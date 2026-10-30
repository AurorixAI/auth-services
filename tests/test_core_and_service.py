"""
Tests for core/security.py, core/exceptions.py, and AuthService (with DB mock)
Supplements test_auth_unit.py — together push coverage ≥70%

Run:
    pytest tests/ -v --cov=src --cov-report=term-missing
"""
import os
from datetime import timedelta, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")


# ═══════════════════════════════════════════════════════════════════════════
# core/security.py — duplicate-free pure functions
# ═══════════════════════════════════════════════════════════════════════════


class TestCoreSecurity:
    """Tests for src/core/security.py (separate from auth/service.py utils)"""

    def setup_method(self):
        from src.core.config import get_settings
        get_settings.cache_clear()

    def test_hash_password_bcrypt(self):
        from src.core.security import hash_password
        h = hash_password("secret123")
        assert h.startswith("$2b$") or h.startswith("$2a$")

    def test_verify_password_correct(self):
        from src.core.security import hash_password, verify_password
        h = hash_password("mypass")
        assert verify_password("mypass", h) is True

    def test_verify_password_wrong(self):
        from src.core.security import hash_password, verify_password
        h = hash_password("mypass")
        assert verify_password("other", h) is False

    def test_create_access_token_type(self):
        from src.core.security import create_access_token, decode_token
        token = create_access_token({"sub": "u1", "role": "teacher"})
        payload = decode_token(token)
        assert payload is not None
        assert payload["type"] == "access"
        assert payload["sub"] == "u1"

    def test_create_refresh_token_type(self):
        from src.core.security import create_refresh_token, decode_token
        token = create_refresh_token({"sub": "u2", "role": "owner"})
        payload = decode_token(token)
        assert payload is not None
        assert payload["type"] == "refresh"

    def test_decode_token_invalid_returns_none(self):
        from src.core.security import decode_token
        assert decode_token("garbage.token.here") is None

    def test_decode_token_empty_returns_none(self):
        from src.core.security import decode_token
        assert decode_token("") is None

    def test_decode_token_tampered_returns_none(self):
        from src.core.security import create_access_token, decode_token
        token = create_access_token({"sub": "u3", "role": "admin"})
        assert decode_token(token[:-8] + "XXXXXXXX") is None

    def test_base_payload_without_extra(self):
        from src.core.security import _base_payload
        p = _base_payload("uid", "teacher")
        assert p == {"sub": "uid", "role": "teacher"}

    def test_base_payload_with_extra(self):
        from src.core.security import _base_payload
        p = _base_payload("uid", "admin", {"org_id": "org-x"})
        assert p["org_id"] == "org-x"


# ═══════════════════════════════════════════════════════════════════════════
# core/exceptions.py
# ═══════════════════════════════════════════════════════════════════════════


class TestExceptions:
    def test_algo_exception_message_and_code(self):
        from src.core.exceptions import AlgoException
        e = AlgoException("something broke", "ERR_CODE")
        assert e.message == "something broke"
        assert e.code == "ERR_CODE"
        assert str(e) == "something broke"

    def test_algo_exception_default_code(self):
        from src.core.exceptions import AlgoException
        e = AlgoException("oops")
        assert e.code == "INTERNAL_ERROR"

    def test_algo_exception_repr(self):
        from src.core.exceptions import AlgoException
        e = AlgoException("fail", "MY_CODE")
        assert "MY_CODE" in repr(e)
        assert "fail" in repr(e)

    def test_not_found_without_id(self):
        from src.core.exceptions import NotFoundError
        e = NotFoundError("Student")
        assert "Student" in e.message
        assert e.code == "NOT_FOUND"

    def test_not_found_with_id(self):
        from src.core.exceptions import NotFoundError
        e = NotFoundError("Group", "group-123")
        assert "group-123" in e.message
        assert e.code == "NOT_FOUND"

    def test_conflict_error(self):
        from src.core.exceptions import ConflictError
        e = ConflictError("Already exists")
        assert e.code == "CONFLICT"
        assert "Already exists" in e.message

    def test_forbidden_error_default(self):
        from src.core.exceptions import ForbiddenError
        e = ForbiddenError()
        assert e.code == "FORBIDDEN"
        assert "denied" in e.message.lower()

    def test_forbidden_error_custom(self):
        from src.core.exceptions import ForbiddenError
        e = ForbiddenError("No access to org")
        assert "No access to org" in e.message

    def test_unauthorized_error(self):
        from src.core.exceptions import UnauthorizedError
        e = UnauthorizedError()
        assert e.code == "UNAUTHORIZED"

    def test_validation_error(self):
        from src.core.exceptions import ValidationError
        e = ValidationError("Phone invalid")
        assert e.code == "VALIDATION_ERROR"
        assert "Phone invalid" in e.message

    def test_service_unavailable_error(self):
        from src.core.exceptions import ServiceUnavailableError
        e = ServiceUnavailableError("Redis")
        assert e.code == "SERVICE_UNAVAILABLE"
        assert "Redis" in e.message

    def test_rate_limit_error(self):
        from src.core.exceptions import RateLimitError
        e = RateLimitError()
        assert e.code == "RATE_LIMIT_EXCEEDED"

    def test_all_exceptions_inherit_algo(self):
        from src.core.exceptions import (
            AlgoException, NotFoundError, ConflictError, ForbiddenError,
            UnauthorizedError, ValidationError, ServiceUnavailableError, RateLimitError,
        )
        for cls in (NotFoundError, ConflictError, ForbiddenError,
                    UnauthorizedError, ValidationError, ServiceUnavailableError, RateLimitError):
            assert issubclass(cls, AlgoException)

    def test_exception_status_map_has_all_types(self):
        from src.core.exceptions import EXCEPTION_STATUS_MAP, AlgoException
        assert len(EXCEPTION_STATUS_MAP) == 8
        from src.core.exceptions import InvalidCredentialsError
        assert EXCEPTION_STATUS_MAP[InvalidCredentialsError] == 401
        for exc_type in EXCEPTION_STATUS_MAP:
            assert issubclass(exc_type, AlgoException)

    def test_not_found_maps_to_404(self):
        from src.core.exceptions import EXCEPTION_STATUS_MAP, NotFoundError
        assert EXCEPTION_STATUS_MAP[NotFoundError] == 404

    def test_forbidden_maps_to_403(self):
        from src.core.exceptions import EXCEPTION_STATUS_MAP, ForbiddenError
        assert EXCEPTION_STATUS_MAP[ForbiddenError] == 403

    def test_rate_limit_maps_to_429(self):
        from src.core.exceptions import EXCEPTION_STATUS_MAP, RateLimitError
        assert EXCEPTION_STATUS_MAP[RateLimitError] == 429


# ═══════════════════════════════════════════════════════════════════════════
# auth/dependencies.py — JWT extraction from request
# ═══════════════════════════════════════════════════════════════════════════


class TestAuthDependencies:
    """Tests for JWT extraction without a running server"""

    def _make_request(self, token: str = None, cookie: str = None) -> MagicMock:
        request = MagicMock()
        request.cookies.get = lambda name, default=None: cookie if name == "algo_auth_token" else default
        request.client = MagicMock()
        request.client.host = "127.0.0.1"
        return request

    def _make_credentials(self, token: str) -> MagicMock:
        creds = MagicMock()
        creds.credentials = token
        return creds

    def test_valid_bearer_token_returns_payload(self):
        from src.auth.service import create_access_token
        from src.auth.dependencies import get_current_user_payload
        token = create_access_token("user-1", "teacher", "org-1", "teacher")

        request = self._make_request()
        creds = self._make_credentials(token)
        payload = get_current_user_payload(request, creds)
        assert payload["sub"] == "user-1"
        assert payload["role"] == "teacher"

    def test_valid_cookie_token_returns_payload(self):
        from src.auth.service import create_access_token
        from src.auth.dependencies import get_current_user_payload
        token = create_access_token("user-2", "owner", "org-2", "owner")

        request = self._make_request(cookie=token)
        payload = get_current_user_payload(request, None)
        assert payload["sub"] == "user-2"

    def test_no_token_raises_401(self):
        from src.auth.dependencies import get_current_user_payload
        from fastapi import HTTPException
        request = self._make_request()
        with pytest.raises(HTTPException) as exc:
            get_current_user_payload(request, None)
        assert exc.value.status_code == 401

    def test_invalid_token_raises_401(self):
        from src.auth.dependencies import get_current_user_payload
        from fastapi import HTTPException
        request = self._make_request()
        creds = self._make_credentials("bad.token.here")
        with pytest.raises(HTTPException) as exc:
            get_current_user_payload(request, creds)
        assert exc.value.status_code == 401

    def test_cookie_takes_priority_over_header(self):
        """Cookie token should be used if both cookie and header are present"""
        from src.auth.service import create_access_token
        from src.auth.dependencies import get_current_user_payload
        cookie_token = create_access_token("cookie-user", "teacher")
        header_token = create_access_token("header-user", "admin")

        request = self._make_request(cookie=cookie_token)
        creds = self._make_credentials(header_token)
        payload = get_current_user_payload(request, creds)
        assert payload["sub"] == "cookie-user"


# ═══════════════════════════════════════════════════════════════════════════
# auth/service.py — AuthService with mocked DB
# ═══════════════════════════════════════════════════════════════════════════


class TestAuthServiceWithMock:
    """Test AuthService logic without a real DB — mock SQLAlchemy Session"""

    def _make_service(self):
        from src.auth.service import AuthService
        db = MagicMock()
        return AuthService(db), db

    def test_service_instantiates(self):
        service, db = self._make_service()
        assert service.db is db

    def test_login_user_not_found_raises(self):
        from src.auth.service import AuthService
        from src.auth.schemas import LoginRequest
        service, db = self._make_service()
        db.query.return_value.filter.return_value.first.return_value = None

        with pytest.raises(Exception):  # ValueError or HTTP exception
            service.login(LoginRequest(email="x@y.com", password="pass"), request=MagicMock())

    def test_login_wrong_password_raises(self):
        from src.auth.service import AuthService, hash_password
        from src.auth.schemas import LoginRequest
        service, db = self._make_service()

        fake_user = MagicMock()
        fake_user.password_hash = hash_password("correct_password")
        fake_user.is_active = True
        db.query.return_value.filter.return_value.first.return_value = fake_user

        with pytest.raises(Exception):
            service.login(LoginRequest(email="x@y.com", password="wrong_password"), request=MagicMock())

    def test_generate_diagnostic_invite_creates_code(self):
        """generate_code should produce a 6-digit code"""
        from src.auth.service import generate_code
        code = generate_code(6)
        assert len(code) == 6
        assert code.isdigit()


class TestGroupCreationPermissions:
    """The server, not the client, owns the teacher assignment rule."""

    def _make_service(self, memberships):
        from src.auth.service import AuthService
        from src.auth.models import OrganizationMember, User

        db = MagicMock()
        membership_results = iter(memberships)
        created = []

        def query(model, *args):
            query_mock = MagicMock()
            if model is OrganizationMember:
                query_mock.filter.return_value.first.side_effect = lambda: next(membership_results)
                query_mock.filter.return_value.with_for_update.return_value.first.side_effect = lambda: next(membership_results)          # the membership is read FOR SHARE
            elif model is User:
                teacher = MagicMock()
                teacher.first_name = "Иван"
                teacher.last_name = "Иванов"
                query_mock.filter.return_value.first.return_value = teacher
            else:
                query_mock.filter.return_value.scalar.return_value = 0
            return query_mock

        def add(value):
            created.append(value)

        def flush():
            from src.auth.models import Group
            from uuid import uuid4

            group = next(item for item in created if isinstance(item, Group))
            if group.id is None:
                group.id = uuid4()

        db.query.side_effect = query
        db.add.side_effect = add
        db.flush.side_effect = flush
        return AuthService(db), db, created

    @staticmethod
    def _membership(role="teacher", active=True):
        membership = MagicMock()
        membership.org_role = role
        membership.is_active = active
        return membership

    def test_teacher_creates_group_only_for_themself_and_audit_is_written(self):
        from uuid import uuid4
        from src.auth.models import Group, GroupAudit
        from src.auth.schemas import GroupCreateRequest

        actor_id = uuid4()
        service, db, created = self._make_service([self._membership("teacher")])
        response = service.create_group(
            str(uuid4()),
            GroupCreateRequest(name="Алгебра-9", class_level=9),
            actor_id=str(actor_id),
            is_org_admin=False,
            actor_role="teacher",
        )

        group = next(item for item in created if isinstance(item, Group))
        audit = next(item for item in created if isinstance(item, GroupAudit))
        assert group.teacher_id == actor_id
        assert audit.actor_id == actor_id
        assert audit.assigned_teacher_id == actor_id
        assert audit.actor_role == "teacher"
        assert audit.action == "created"
        assert audit.snapshot["name"] == "Алгебра-9"
        assert response.teacher_id == str(actor_id)
        db.commit.assert_called_once()

    def test_teacher_cannot_forge_assignment_to_another_teacher(self):
        from uuid import uuid4
        from src.auth.schemas import GroupCreateRequest

        actor_id = uuid4()
        service, db, created = self._make_service([self._membership("teacher")])

        with pytest.raises(PermissionError, match="только для себя"):
            service.create_group(
                str(uuid4()),
                GroupCreateRequest(name="Чужая группа", teacher_id=str(uuid4())),
                actor_id=str(actor_id),
                is_org_admin=False,
                actor_role="teacher",
            )

        assert created == []
        db.commit.assert_not_called()

    def test_manager_can_assign_only_an_active_teacher_of_the_same_org(self):
        from uuid import uuid4
        from src.auth.models import Group, GroupAudit
        from src.auth.schemas import GroupCreateRequest

        actor_id = uuid4()
        assigned_teacher_id = uuid4()
        service, db, created = self._make_service([
            self._membership("admin"),
            self._membership("teacher"),
        ])
        service.create_group(
            str(uuid4()),
            GroupCreateRequest(name="Геометрия-10", teacher_id=str(assigned_teacher_id)),
            actor_id=str(actor_id),
            is_org_admin=True,
            actor_role="admin",
        )

        group = next(item for item in created if isinstance(item, Group))
        audit = next(item for item in created if isinstance(item, GroupAudit))
        assert group.teacher_id == assigned_teacher_id
        assert audit.actor_id == actor_id
        assert audit.assigned_teacher_id == assigned_teacher_id
        assert audit.actor_role == "admin"
        db.commit.assert_called_once()

    def test_manager_cannot_assign_a_group_to_an_inactive_or_foreign_member(self):
        from uuid import uuid4
        from src.auth.schemas import GroupCreateRequest

        service, db, created = self._make_service([
            self._membership("owner"),
            None,
        ])

        with pytest.raises(ValueError, match="активного преподавателя"):
            service.create_group(
                str(uuid4()),
                GroupCreateRequest(name="Некорректное назначение", teacher_id=str(uuid4())),
                actor_id=str(uuid4()),
                is_org_admin=True,
                actor_role="owner",
            )

        assert created == []
        db.commit.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# AuthService — extended coverage: internal helpers + CRUD methods
# ═══════════════════════════════════════════════════════════════════════════


class TestAuthServiceExtended:
    """Extended tests covering _get_user_org_info, _create_tokens, login,
    refresh_tokens, logout, get_user_by_id, change_password, update_profile."""

    def _make_service(self):
        from src.auth.service import AuthService
        db = MagicMock()
        return AuthService(db), db

    def _mock_user(self, *, is_active=True, role="teacher", default_org_id=None):
        from uuid import uuid4
        user = MagicMock()
        user.id = uuid4()
        user.role = role
        user.is_active = is_active
        user.default_org_id = default_org_id
        user.login_count = 0
        user.refresh_token_hash = None
        return user

    # ─── _get_user_org_info ─────────────────────────────────────────────

    def test_get_user_org_info_no_org(self):
        service, db = self._make_service()
        user = self._mock_user(default_org_id=None)
        org_id, org_role, org_name = service._get_user_org_info(user)
        assert org_id is None
        assert org_role is None
        assert org_name is None

    def test_get_user_org_info_with_org_and_member(self):
        from uuid import uuid4
        service, db = self._make_service()
        oid = uuid4()
        user = self._mock_user(default_org_id=oid)
        member = MagicMock()
        member.org_role = "admin"
        org = MagicMock()
        org.name = "Test Org"
        # two .query calls: first for member, second for org
        db.query.return_value.filter.return_value.first.side_effect = [member, org]
        org_id, org_role, org_name = service._get_user_org_info(user)
        assert org_id == str(oid)
        assert org_role == "admin"
        assert org_name == "Test Org"

    def test_get_user_org_info_member_none(self):
        from uuid import uuid4
        service, db = self._make_service()
        oid = uuid4()
        user = self._mock_user(default_org_id=oid)
        org = MagicMock()
        org.name = "Orphan Org"
        db.query.return_value.filter.return_value.first.side_effect = [None, org]
        org_id, org_role, org_name = service._get_user_org_info(user)
        assert org_role is None
        assert org_name == "Orphan Org"

    def test_get_user_org_info_org_none(self):
        from uuid import uuid4
        service, db = self._make_service()
        oid = uuid4()
        user = self._mock_user(default_org_id=oid)
        member = MagicMock()
        member.org_role = "teacher"
        db.query.return_value.filter.return_value.first.side_effect = [member, None]
        org_id, org_role, org_name = service._get_user_org_info(user)
        assert org_role == "teacher"
        assert org_name is None

    # ─── _build_org_response ────────────────────────────────────────────

    def test_build_org_response(self):
        from datetime import datetime, timezone
        service, db = self._make_service()
        org = MagicMock()
        org.id = "org-1"
        org.name = "My Org"
        org.org_type = "center"
        org.city = "Almaty"
        org.phone = "+77001234567"
        org.email = "org@test.kz"
        org.status = "active"
        org.created_at = datetime.now(timezone.utc)
        result = service._build_org_response(org)
        assert result.name == "My Org"
        assert result.city == "Almaty"

    def test_build_org_response_no_created_at(self):
        service, db = self._make_service()
        org = MagicMock()
        org.id = "org-2"
        org.name = "Org"
        org.org_type = None
        org.city = None
        org.phone = None
        org.email = "e@o.kz"
        org.status = None
        org.created_at = None
        result = service._build_org_response(org)
        assert result.created_at is None

    # ─── logout ─────────────────────────────────────────────────────────

    def test_logout_from_an_access_token_alone_revokes_no_session(self):
        """#101: an access token names the user, not the device - signing out "all" would sign out the other
        devices. The session of a device is revoked through its refresh token (logout_by_refresh_token)."""
        from uuid import uuid4
        service, db = self._make_service()
        service.logout(str(uuid4()))
        db.execute.assert_not_called()
        db.commit.assert_not_called()

    def test_logout_no_user_is_noop(self):
        service, db = self._make_service()
        db.query.return_value.filter.return_value.first.return_value = None
        service.logout("nonexistent-id")  # must not raise
        db.commit.assert_not_called()

    # ─── get_user_by_id ─────────────────────────────────────────────────

    def test_get_user_by_id_found(self):
        service, db = self._make_service()
        user = self._mock_user()
        db.query.return_value.filter.return_value.first.return_value = user
        result = service.get_user_by_id("some-id")
        assert result is user

    def test_get_user_by_id_not_found(self):
        service, db = self._make_service()
        db.query.return_value.filter.return_value.first.return_value = None
        result = service.get_user_by_id("missing")
        assert result is None

    # ─── change_password ────────────────────────────────────────────────

    def test_change_password_user_not_found_raises(self):
        service, db = self._make_service()
        db.query.return_value.filter.return_value.first.return_value = None
        with pytest.raises(ValueError, match="не найден"):
            service.change_password("uid", "old", "new_pass_valid")

    def test_change_password_wrong_old_password_raises(self):
        from src.auth.service import hash_password
        service, db = self._make_service()
        user = self._mock_user()
        user.password_hash = hash_password("correct")
        db.query.return_value.filter.return_value.first.return_value = user
        with pytest.raises(ValueError, match="текущий пароль"):
            service.change_password("uid", "wrong", "new_pass_valid")

    def test_change_password_success(self):
        from src.auth.service import hash_password, verify_password
        service, db = self._make_service()
        user = self._mock_user()
        user.password_hash = hash_password("old_pass")
        db.query.return_value.filter.return_value.first.return_value = user
        with patch("src.auth.service.user_sessions.revoke_all") as revoke_all:
            result = service.change_password("uid", "old_pass", "new_pass_valid")
        assert result is True
        assert verify_password("new_pass_valid", user.password_hash)
        revoke_all.assert_called_once_with(db, user.id, "password_changed", except_id=None)  # every OTHER device signs in again (K2-1 M1)
        db.commit.assert_called()

    # ─── update_profile ─────────────────────────────────────────────────

    def test_update_profile_user_not_found_raises(self):
        service, db = self._make_service()
        db.query.return_value.filter.return_value.first.return_value = None
        with pytest.raises(ValueError, match="не найден"):
            service.update_profile("uid", first_name="Alex")

    def test_update_profile_success(self):
        service, db = self._make_service()
        user = self._mock_user()
        user.first_name = "Old"
        db.query.return_value.filter.return_value.first.return_value = user
        result = service.update_profile("uid", first_name="New", last_name=None)
        assert result is user
        assert user.first_name == "New"
        db.commit.assert_called()

    # ─── login failure paths ─────────────────────────────────────────────

    def test_login_user_not_found_gets_the_one_credentials_answer(self):
        from src.auth.schemas import LoginRequest
        from src.core.exceptions import InvalidCredentialsError
        service, db = self._make_service()
        db.query.return_value.filter.return_value.first.return_value = None
        with pytest.raises(InvalidCredentialsError, match="Неверная почта или пароль") as exc:
            service.login(LoginRequest(email="x@y.com", password="anypass"))
        assert exc.value.code == "INVALID_CREDENTIALS"

    def test_login_inactive_user_gets_the_same_answer_even_with_the_right_password(self):
        from src.auth.service import hash_password
        from src.auth.schemas import LoginRequest
        from src.core.exceptions import InvalidCredentialsError
        service, db = self._make_service()
        user = self._mock_user(is_active=False)
        user.password_hash = hash_password("anypass")
        db.query.return_value.filter.return_value.first.return_value = user
        with pytest.raises(InvalidCredentialsError, match="Неверная почта или пароль") as exc:
            service.login(LoginRequest(email="x@y.com", password="anypass"))
        assert exc.value.code == "INVALID_CREDENTIALS"

    def test_login_wrong_password_raises(self):
        from src.auth.service import hash_password
        from src.auth.schemas import LoginRequest
        from src.core.exceptions import InvalidCredentialsError
        service, db = self._make_service()
        user = self._mock_user(is_active=True)
        user.password_hash = hash_password("correct_pw")
        db.query.return_value.filter.return_value.first.return_value = user
        with pytest.raises(InvalidCredentialsError, match="Неверная почта или пароль") as exc:
            service.login(LoginRequest(email="x@y.com", password="wrong_pw"))
        assert exc.value.code == "INVALID_CREDENTIALS"

    def test_login_does_the_same_bcrypt_work_for_unknown_inactive_and_wrong_password(self, monkeypatch):
        """No timing oracle: a password is verified in all three cases (a dummy hash when there is no user)."""
        from src.auth import service as service_module
        from src.auth.schemas import LoginRequest
        from src.auth.service import hash_password
        from src.core.exceptions import InvalidCredentialsError

        verified = []
        monkeypatch.setattr(
            service_module, "verify_password", lambda plain, hashed: verified.append(hashed) or False
        )
        real = hash_password("pw")
        cases = {
            "unknown": None,
            "inactive": self._mock_user(is_active=False),
            "wrong": self._mock_user(is_active=True),
        }
        cases["inactive"].password_hash = real
        cases["wrong"].password_hash = real
        for name, user in cases.items():
            service, db = self._make_service()
            db.query.return_value.filter.return_value.first.return_value = user
            with pytest.raises(InvalidCredentialsError):
                service.login(LoginRequest(email="x@y.com", password="anypass"))
        assert verified == [service_module._DUMMY_PASSWORD_HASH, real, real]

    def test_the_dummy_hash_is_a_real_bcrypt_hash_of_the_current_cost(self):
        from src.auth import service as service_module

        dummy = service_module._DUMMY_PASSWORD_HASH
        fresh = service_module.hash_password("anything")
        assert dummy[:7] == fresh[:7] == "$2b$12$" and len(dummy) == len(fresh) == 60
        assert service_module.verify_password("not-the-password", dummy) is False

    # ─── refresh_tokens failure paths ───────────────────────────────────

    def test_refresh_tokens_invalid_token_raises(self):
        service, db = self._make_service()
        with pytest.raises(ValueError, match="[Нн]евалидн"):
            service.refresh_tokens("not.a.valid.jwt")

    def test_refresh_tokens_user_not_found_raises(self):
        from src.auth.service import create_refresh_token
        service, db = self._make_service()
        token = create_refresh_token("user-1")
        db.query.return_value.filter.return_value.first.return_value = None
        with pytest.raises(ValueError):
            service.refresh_tokens(token)

    def test_refresh_tokens_hash_mismatch_raises(self):
        from src.auth.service import create_refresh_token, hash_token
        service, db = self._make_service()
        token = create_refresh_token("user-1")
        user = self._mock_user(is_active=True)
        db.query.return_value.filter.return_value.first.return_value = user
        with patch("src.auth.platform_blocks.ensure_login_allowed"), patch("src.auth.service.user_sessions.claim", return_value=None) as claim:
            with pytest.raises(ValueError, match="[Оо]тозван"):
                service.refresh_tokens(token)
        assert claim.call_args.args[1:] == (user.id, hash_token(token))  # looked up by the hash of the token
        db.commit.assert_called()  # a replay revokes its session; that must survive the refusal

    # ─── generate_code ──────────────────────────────────────────────────

    def test_generate_code_digits_only(self):
        from src.auth.service import generate_code
        code = generate_code(8, digits_only=True)
        assert len(code) == 8
        assert code.isdigit()

    def test_generate_code_alphanumeric(self):
        from src.auth.service import generate_code
        codes = [generate_code(10, digits_only=False) for _ in range(20)]
        assert all(len(c) == 10 for c in codes)
        # With 10-char alphanumeric codes, statistically at least one has a letter
        has_letter = any(any(ch.isalpha() for ch in c) for c in codes)
        assert has_letter

    # ─── hash_token ─────────────────────────────────────────────────────

    def test_hash_token_sha256(self):
        import hashlib
        from src.auth.service import hash_token
        t = "my-token"
        expected = hashlib.sha256(t.encode()).hexdigest()
        assert hash_token(t) == expected

    def test_hash_token_deterministic(self):
        from src.auth.service import hash_token
        assert hash_token("abc") == hash_token("abc")
        assert hash_token("abc") != hash_token("xyz")

    # ─── database.py context manager ───────────────────────────────────

    def test_db_session_commit_on_success(self):
        from unittest.mock import patch, MagicMock
        from src.core.database import db_session
        mock_session = MagicMock()
        with patch("src.core.database.SessionLocal", return_value=mock_session):
            with db_session() as db:
                assert db is mock_session
        mock_session.commit.assert_called_once()
        mock_session.close.assert_called_once()

    def test_db_session_rollback_on_exception(self):
        from unittest.mock import patch, MagicMock
        from src.core.database import db_session
        mock_session = MagicMock()
        with patch("src.core.database.SessionLocal", return_value=mock_session):
            with pytest.raises(RuntimeError):
                with db_session() as db:
                    raise RuntimeError("boom")
        mock_session.rollback.assert_called_once()
        mock_session.close.assert_called_once()

    def test_get_db_yields_session_and_closes(self):
        from unittest.mock import patch, MagicMock
        from src.core.database import get_db
        mock_session = MagicMock()
        with patch("src.core.database.SessionLocal", return_value=mock_session):
            gen = get_db()
            db = next(gen)
            assert db is mock_session
            try:
                next(gen)
            except StopIteration:
                pass
        mock_session.close.assert_called_once()

    def test_get_db_rollback_on_exception(self):
        from unittest.mock import patch, MagicMock
        from src.core.database import get_db
        mock_session = MagicMock()
        with patch("src.core.database.SessionLocal", return_value=mock_session):
            gen = get_db()
            next(gen)
            try:
                gen.throw(ValueError("db error"))
            except ValueError:
                pass
        mock_session.rollback.assert_called_once()
        mock_session.close.assert_called_once()

    # ─── _create_tokens (no org) ─────────────────────────────────────────

    def test_create_tokens_no_org(self):
        from src.auth.service import TokenPair, hash_token
        service, db = self._make_service()
        user = self._mock_user(default_org_id=None, role="teacher")
        with patch("src.auth.service.user_sessions.issue") as issue:
            result = service._create_tokens(user)
        assert isinstance(result, TokenPair)
        assert result.access_token
        assert result.refresh_token
        # a NEW session row for the hash of the refresh token (the legacy users column is not written any more)
        issue.assert_called_once()
        assert issue.call_args.args[:3] == (db, user.id, hash_token(result.refresh_token))
        assert user.refresh_token_hash is None

    # ─── _build_user_response ────────────────────────────────────────────

    def _user_dict(self, user):
        """Return a dict valid for UserResponse Pydantic model."""
        return {
            "id": str(user.id),
            "email": "test@test.kz",
            "first_name": "Test",
            "last_name": "User",
            "full_name": "Test User",
            "role": user.role,
            "default_org_id": None,
            "language": "ru",
            "is_active": True,
            "is_verified": False,
            "created_at": None,
        }

    def test_build_user_response_no_org(self):
        service, db = self._make_service()
        user = self._mock_user(default_org_id=None, role="teacher")
        user.to_dict.return_value = self._user_dict(user)
        result = service._build_user_response(user)
        assert result.email == "test@test.kz"
        assert result.org_role is None
        assert result.org_name is None

    def test_build_user_response_with_org(self):
        from uuid import uuid4
        service, db = self._make_service()
        oid = uuid4()
        user = self._mock_user(default_org_id=oid, role="admin")
        d = self._user_dict(user)
        user.to_dict.return_value = d
        member = MagicMock()
        member.org_role = "admin"
        org = MagicMock()
        org.name = "My Academy"
        db.query.return_value.filter.return_value.first.side_effect = [member, org]
        result = service._build_user_response(user)
        assert result.org_role == "admin"
        assert result.org_name == "My Academy"

    # ─── login success path ──────────────────────────────────────────────

    def test_login_success_no_org(self):
        from src.auth.service import hash_password, AuthResponse
        from src.auth.schemas import LoginRequest
        service, db = self._make_service()
        user = self._mock_user(is_active=True, role="teacher", default_org_id=None)
        user.password_hash = hash_password("good_password")
        user.to_dict.return_value = self._user_dict(user)
        db.query.return_value.filter.return_value.first.return_value = user
        with patch("src.auth.platform_blocks.ensure_login_allowed"):  # the console account / blocked centre check has its own tests
            result = service.login(LoginRequest(email="u@t.kz", password="good_password"))
        assert isinstance(result, AuthResponse)
        assert result.tokens.access_token
        assert user.login_count == 1
        db.commit.assert_called()

    # ─── refresh_tokens success path ────────────────────────────────────

    def test_refresh_tokens_success(self):
        from src.auth.service import create_refresh_token, hash_token, TokenPair
        service, db = self._make_service()
        token = create_refresh_token("user-abc")
        user = self._mock_user(is_active=True, role="teacher", default_org_id=None)
        db.query.return_value.filter.return_value.first.return_value = user
        claimed = MagicMock(graced=False)
        with patch("src.auth.platform_blocks.ensure_login_allowed"), patch("src.auth.service.user_sessions.claim", return_value=claimed):
            with patch("src.auth.service.user_sessions.rotate") as rotate:
                result = service.refresh_tokens(token)
        assert isinstance(result, TokenPair)
        assert result.access_token
        assert result.refresh_token != token
        # the session of THIS token gets the new token; nothing is written to the users row
        assert rotate.call_args.args[1:3] == (claimed, hash_token(result.refresh_token))
        assert user.refresh_token_hash is None
        db.commit.assert_called()


# ═══════════════════════════════════════════════════════════════════════════
# auth/schemas.py — validator coverage
# ═══════════════════════════════════════════════════════════════════════════


class TestSchemaValidators:
    """Hit the validator raise-branches in auth/schemas.py"""

    def test_org_application_invalid_org_type(self):
        from src.auth.schemas import OrgApplicationRequest
        with pytest.raises(Exception):
            OrgApplicationRequest(
                org_name="Test",
                org_type="invalid_type",
                email="owner@t.kz",
                password="password1",
                first_name="A",
                last_name="B",
            )

    def test_org_application_invalid_email_format(self):
        from src.auth.schemas import OrgApplicationRequest
        with pytest.raises(Exception):
            OrgApplicationRequest(
                org_name="Test",
                org_type="center",
                email="notanemail",
                password="password1",
                first_name="A",
                last_name="B",
            )

    def test_org_application_valid_normalizes_email(self):
        from src.auth.schemas import OrgApplicationRequest
        req = OrgApplicationRequest(
            org_name="My Academy",
            org_type="center",
            email="  Owner@Test.KZ  ",
            password="secret123",
            first_name="Ali",
            last_name="Bek",
        )
        assert req.email == "owner@test.kz"

    def test_invite_member_invalid_role(self):
        from src.auth.schemas import InviteMemberRequest
        with pytest.raises(Exception):
            InviteMemberRequest(org_role="student")

    def test_invite_member_valid_role(self):
        from src.auth.schemas import InviteMemberRequest
        req = InviteMemberRequest(org_role="admin")
        assert req.org_role == "admin"

    def test_accept_invite_normalizes_email(self):
        from src.auth.schemas import AcceptInviteRequest
        req = AcceptInviteRequest(
            code="ABCD1234",
            email="  UPPER@EXAMPLE.COM  ",
            password="pass123",
            first_name="A",
            last_name="B",
        )
        assert req.email == "upper@example.com"

    def test_student_update_invalid_status(self):
        from src.auth.schemas import StudentUpdateRequest
        with pytest.raises(Exception):
            StudentUpdateRequest(status="unknown_status")

    def test_student_update_valid_status(self):
        from src.auth.schemas import StudentUpdateRequest
        req = StudentUpdateRequest(status="active")
        assert req.status == "active"

    def test_student_update_none_status_is_ok(self):
        from src.auth.schemas import StudentUpdateRequest
        req = StudentUpdateRequest(status=None)
        assert req.status is None

    def test_diagnostic_create_invalid_type(self):
        from src.auth.schemas import DiagnosticCreateRequest
        with pytest.raises(Exception):
            DiagnosticCreateRequest(
                student_id="s1",
                class_level=5,
                diagnostic_type="wrong_type",
            )

    def test_diagnostic_create_valid_type(self):
        from src.auth.schemas import DiagnosticCreateRequest
        req = DiagnosticCreateRequest(
            student_id="s1",
            class_level=5,
            diagnostic_type="initial",
        )
        assert req.diagnostic_type == "initial"


# ═══════════════════════════════════════════════════════════════════════════
# src/api/database.py — get_db generator
# ═══════════════════════════════════════════════════════════════════════════


class TestApiDatabase:
    def test_api_get_db_yields_and_closes(self):
        from unittest.mock import patch, MagicMock
        from src.api.database import get_db
        mock_session = MagicMock()
        with patch("src.api.database.SessionLocal", return_value=mock_session):
            gen = get_db()
            db = next(gen)
            assert db is mock_session
            try:
                next(gen)
            except StopIteration:
                pass
        mock_session.close.assert_called_once()
