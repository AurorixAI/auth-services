"""
Tests for repositories (base + user/org/student/group) (core/dependencies.py is gone: K2-1, the access checks all go through the live session)
Brings coverage toward ≥70%

Run:
    pytest tests/ -v --cov=src --cov-report=term-missing
"""
import os
from unittest.mock import MagicMock, patch, PropertyMock
from uuid import UUID, uuid4

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-ok")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-must-be-at-least-32")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mock_session():
    """Return a MagicMock that mimics SQLAlchemy Session"""
    session = MagicMock()
    # session.scalars() → .all() chain
    session.scalars.return_value = iter([])
    return session


# ═══════════════════════════════════════════════════════════════════════════
# BaseRepository
# ═══════════════════════════════════════════════════════════════════════════


class TestBaseRepository:
    def _make_repo(self):
        from src.repositories.base import BaseRepository
        from src.models.auth import User
        session = _mock_session()

        class UserTestRepo(BaseRepository):
            model = User

        return UserTestRepo(session), session

    def test_init_stores_session(self):
        repo, session = self._make_repo()
        assert repo.session is session

    def test_get_by_id_calls_session_get(self):
        repo, session = self._make_repo()
        fake = MagicMock()
        session.get.return_value = fake
        result = repo.get_by_id("pk-1")
        session.get.assert_called_once()
        assert result is fake

    def test_get_by_id_returns_none_when_not_found(self):
        repo, session = self._make_repo()
        session.get.return_value = None
        assert repo.get_by_id("missing") is None

    def test_get_by_id_or_raise_returns_object(self):
        repo, session = self._make_repo()
        fake = MagicMock()
        session.get.return_value = fake
        result = repo.get_by_id_or_raise("pk-1")
        assert result is fake

    def test_get_by_id_or_raise_raises_not_found(self):
        from src.core.exceptions import NotFoundError
        repo, session = self._make_repo()
        session.get.return_value = None
        with pytest.raises(NotFoundError):
            repo.get_by_id_or_raise("missing-pk")

    def test_get_by_id_or_raise_uses_label(self):
        from src.core.exceptions import NotFoundError
        repo, session = self._make_repo()
        session.get.return_value = None
        with pytest.raises(NotFoundError) as exc:
            repo.get_by_id_or_raise("pk", label="MyEntity")
        assert "MyEntity" in exc.value.message

    def test_list_all_calls_scalars(self):
        repo, session = self._make_repo()
        fake_list = [MagicMock(), MagicMock()]
        session.scalars.return_value = iter(fake_list)
        result = repo.list_all(limit=10, offset=0)
        assert session.scalars.called
        assert result == fake_list

    def test_exists_true_when_found(self):
        repo, session = self._make_repo()
        session.get.return_value = MagicMock()
        assert repo.exists("pk") is True

    def test_exists_false_when_not_found(self):
        repo, session = self._make_repo()
        session.get.return_value = None
        assert repo.exists("pk") is False

    def test_add_calls_session_add_and_flush(self):
        repo, session = self._make_repo()
        obj = MagicMock()
        result = repo.add(obj)
        session.add.assert_called_once_with(obj)
        session.flush.assert_called()
        assert result is obj

    def test_delete_calls_session_delete_and_flush(self):
        repo, session = self._make_repo()
        obj = MagicMock()
        repo.delete(obj)
        session.delete.assert_called_once_with(obj)
        session.flush.assert_called()

    def test_delete_by_id_returns_true_when_found(self):
        repo, session = self._make_repo()
        obj = MagicMock()
        session.get.return_value = obj
        result = repo.delete_by_id("pk")
        assert result is True
        session.delete.assert_called_once_with(obj)

    def test_delete_by_id_returns_false_when_not_found(self):
        repo, session = self._make_repo()
        session.get.return_value = None
        result = repo.delete_by_id("missing")
        assert result is False
        session.delete.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# UserRepo
# ═══════════════════════════════════════════════════════════════════════════


class TestUserRepo:
    def _make_repo(self):
        from src.repositories.user_repo import UserRepo
        session = _mock_session()
        return UserRepo(session), session

    def test_get_by_email_calls_scalar(self):
        repo, session = self._make_repo()
        fake_user = MagicMock()
        session.scalar.return_value = fake_user
        result = repo.get_by_email("test@example.com")
        assert session.scalar.called
        assert result is fake_user

    def test_get_by_email_returns_none(self):
        repo, session = self._make_repo()
        session.scalar.return_value = None
        assert repo.get_by_email("nobody@example.com") is None

    def test_email_exists_true(self):
        repo, session = self._make_repo()
        session.scalar.return_value = MagicMock()
        assert repo.email_exists("exists@example.com") is True

    def test_email_exists_false(self):
        repo, session = self._make_repo()
        session.scalar.return_value = None
        assert repo.email_exists("nope@example.com") is False

    def test_get_active_by_email_returns_user(self):
        repo, session = self._make_repo()
        fake_user = MagicMock()
        session.scalar.return_value = fake_user
        result = repo.get_active_by_email("active@example.com")
        assert result is fake_user

    def test_get_by_id_str_valid_uuid(self):
        repo, session = self._make_repo()
        uid = str(uuid4())
        fake_user = MagicMock()
        session.get.return_value = fake_user
        result = repo.get_by_id_str(uid)
        assert result is fake_user

    def test_get_by_id_str_invalid_uuid_returns_none(self):
        repo, session = self._make_repo()
        result = repo.get_by_id_str("not-a-uuid")
        assert result is None

    def test_update_last_login_flushes(self):
        repo, session = self._make_repo()
        user = MagicMock()
        user.login_count = 5
        repo.update_last_login(user)
        session.flush.assert_called()
        assert user.login_count == 6

    def test_list_by_org_calls_scalars(self):
        repo, session = self._make_repo()
        fake_users = [MagicMock(), MagicMock()]
        session.scalars.return_value = iter(fake_users)
        result = repo.list_by_org(uuid4())
        assert session.scalars.called
        assert result == fake_users

    def test_list_by_org_with_role(self):
        repo, session = self._make_repo()
        session.scalars.return_value = iter([])
        result = repo.list_by_org(uuid4(), role="teacher")
        assert session.scalars.called
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════
# OrgMemberRepo
# ═══════════════════════════════════════════════════════════════════════════


class TestOrgMemberRepo:
    def _make_repo(self):
        from src.repositories.user_repo import OrgMemberRepo
        session = _mock_session()
        return OrgMemberRepo(session), session

    def test_get_membership_found(self):
        repo, session = self._make_repo()
        fake_member = MagicMock()
        session.scalar.return_value = fake_member
        result = repo.get_membership(uuid4(), uuid4())
        assert result is fake_member

    def test_get_membership_not_found(self):
        repo, session = self._make_repo()
        session.scalar.return_value = None
        result = repo.get_membership(uuid4(), uuid4())
        assert result is None

    def test_is_member_true(self):
        repo, session = self._make_repo()
        session.scalar.return_value = MagicMock()
        assert repo.is_member(uuid4(), uuid4()) is True

    def test_is_member_false(self):
        repo, session = self._make_repo()
        session.scalar.return_value = None
        assert repo.is_member(uuid4(), uuid4()) is False

    def test_list_org_members_returns_list(self):
        repo, session = self._make_repo()
        fake_members = [MagicMock()]
        session.scalars.return_value = iter(fake_members)
        result = repo.list_org_members(uuid4())
        assert result == fake_members


# ═══════════════════════════════════════════════════════════════════════════
# GroupRepo + StudentGroupRepo
# ═══════════════════════════════════════════════════════════════════════════


class TestGroupRepo:
    def _make_repo(self):
        from src.repositories.group_repo import GroupRepo
        session = _mock_session()
        return GroupRepo(session), session

    def test_list_by_org_returns_groups(self):
        repo, session = self._make_repo()
        fake_groups = [MagicMock(), MagicMock()]
        session.scalars.return_value = iter(fake_groups)
        result = repo.list_by_org(uuid4())
        assert session.scalars.called
        assert result == fake_groups

    def test_list_by_org_with_status_filter(self):
        repo, session = self._make_repo()
        session.scalars.return_value = iter([])
        result = repo.list_by_org(uuid4(), status="inactive")
        assert result == []

    def test_list_by_teacher_returns_groups(self):
        repo, session = self._make_repo()
        fake_groups = [MagicMock()]
        session.scalars.return_value = iter(fake_groups)
        result = repo.list_by_teacher(uuid4())
        assert result == fake_groups

    def test_count_students_returns_int(self):
        repo, session = self._make_repo()
        members = [MagicMock(), MagicMock(), MagicMock()]
        session.scalars.return_value = iter(members)
        count = repo.count_students(uuid4())
        assert count == 3

    def test_count_students_empty_group(self):
        repo, session = self._make_repo()
        session.scalars.return_value = iter([])
        count = repo.count_students(uuid4())
        assert count == 0


class TestStudentGroupRepo:
    def _make_repo(self):
        from src.repositories.group_repo import StudentGroupRepo
        session = _mock_session()
        return StudentGroupRepo(session), session

    def test_get_membership_found(self):
        repo, session = self._make_repo()
        fake = MagicMock()
        session.scalar.return_value = fake
        result = repo.get_membership(uuid4(), uuid4())
        assert result is fake

    def test_get_membership_not_found(self):
        repo, session = self._make_repo()
        session.scalar.return_value = None
        result = repo.get_membership(uuid4(), uuid4())
        assert result is None

    def test_list_active_students(self):
        repo, session = self._make_repo()
        fake_members = [MagicMock()]
        session.scalars.return_value = iter(fake_members)
        result = repo.list_active_students(uuid4())
        assert result == fake_members

    def test_remove_student_when_not_member(self):
        repo, session = self._make_repo()
        session.scalar.return_value = None
        result = repo.remove_student(uuid4(), uuid4())
        assert result is False
        session.flush.assert_not_called()

    def test_remove_student_when_member(self):
        from datetime import datetime

        from src.auth.membership_periods import open_period
        from src.models.auth import StudentGroup

        repo, session = self._make_repo()
        membership = StudentGroup(student_id=uuid4(), group_id=uuid4())
        open_period(membership, datetime(2026, 9, 1, 8, 0))
        session.scalar.return_value = membership
        result = repo.remove_student(membership.student_id, membership.group_id)
        assert result is True
        # the membership AND its open period are closed by the same instant
        assert membership.is_active is False and membership.left_at is not None
        assert [(p.joined_at, p.left_at) for p in membership.periods] == [(datetime(2026, 9, 1, 8, 0), membership.left_at)]
        session.flush.assert_called()


# ═══════════════════════════════════════════════════════════════════════════
# OrgRepo + InvitationRepo
# ═══════════════════════════════════════════════════════════════════════════


class TestOrgRepo:
    def _make_repo(self):
        from src.repositories.org_repo import OrgRepo
        session = _mock_session()
        return OrgRepo(session), session

    def test_get_by_name_found(self):
        repo, session = self._make_repo()
        fake_org = MagicMock()
        session.scalar.return_value = fake_org
        result = repo.get_by_name("Test Org")
        assert result is fake_org

    def test_get_by_name_not_found(self):
        repo, session = self._make_repo()
        session.scalar.return_value = None
        result = repo.get_by_name("Unknown Org")
        assert result is None

    def test_list_pending_returns_list(self):
        repo, session = self._make_repo()
        fake_orgs = [MagicMock(), MagicMock()]
        session.scalars.return_value = iter(fake_orgs)
        result = repo.list_pending()
        assert result == fake_orgs

    def test_list_active_returns_list(self):
        repo, session = self._make_repo()
        fake_orgs = [MagicMock()]
        session.scalars.return_value = iter(fake_orgs)
        result = repo.list_active()
        assert result == fake_orgs

    def test_approve_sets_status_active(self):
        repo, session = self._make_repo()
        org = MagicMock()
        approver_id = uuid4()
        result = repo.approve(org, approver_id)
        assert org.status == "active"
        assert org.approved_by == approver_id
        session.flush.assert_called()
        assert result is org

    def test_reject_sets_status_suspended(self):
        repo, session = self._make_repo()
        org = MagicMock()
        result = repo.reject(org, "Policy violation")
        assert org.status == "suspended"
        assert org.rejection_reason == "Policy violation"
        session.flush.assert_called()
        assert result is org


class TestInvitationRepo:
    def _make_repo(self):
        from src.repositories.org_repo import InvitationRepo
        session = _mock_session()
        return InvitationRepo(session), session

    def test_get_by_code_found(self):
        repo, session = self._make_repo()
        fake_invite = MagicMock()
        session.scalar.return_value = fake_invite
        result = repo.get_by_code("ABC123")
        assert result is fake_invite

    def test_get_by_code_not_found(self):
        repo, session = self._make_repo()
        session.scalar.return_value = None
        result = repo.get_by_code("EXPIRED")
        assert result is None

    def test_list_by_org(self):
        repo, session = self._make_repo()
        fake_invites = [MagicMock(), MagicMock()]
        session.scalars.return_value = iter(fake_invites)
        result = repo.list_by_org(uuid4())
        assert result == fake_invites

    def test_mark_accepted(self):
        repo, session = self._make_repo()
        invite = MagicMock()
        uid = uuid4()
        result = repo.mark_accepted(invite, uid)
        assert invite.status == "accepted"
        assert invite.accepted_by == uid
        session.flush.assert_called()
        assert result is invite


# ═══════════════════════════════════════════════════════════════════════════
# StudentRepo + DiagnosticInviteRepo + StudentDiagnosticResultRepo
# ═══════════════════════════════════════════════════════════════════════════


class TestStudentRepo:
    def _make_repo(self):
        from src.repositories.student_repo import StudentRepo
        session = _mock_session()
        return StudentRepo(session), session

    def test_list_by_org_returns_students(self):
        repo, session = self._make_repo()
        fake_students = [MagicMock(), MagicMock()]
        session.scalars.return_value = iter(fake_students)
        result = repo.list_by_org(uuid4())
        assert result == fake_students

    def test_list_by_org_with_status_filter(self):
        repo, session = self._make_repo()
        session.scalars.return_value = iter([])
        result = repo.list_by_org(uuid4(), status="active")
        assert result == []

    def test_list_by_group(self):
        repo, session = self._make_repo()
        fake_students = [MagicMock()]
        session.scalars.return_value = iter(fake_students)
        result = repo.list_by_group(uuid4())
        assert result == fake_students

    def test_count_by_org(self):
        repo, session = self._make_repo()
        students = [MagicMock(), MagicMock(), MagicMock()]
        session.scalars.return_value = iter(students)
        count = repo.count_by_org(uuid4())
        assert count == 3

    def test_set_status_updates_student(self):
        repo, session = self._make_repo()
        student = MagicMock()
        result = repo.set_status(student, "inactive")
        assert student.status == "inactive"
        session.flush.assert_called()
        assert result is student


class TestDiagnosticInviteRepo:
    def _make_repo(self):
        from src.repositories.student_repo import DiagnosticInviteRepo
        session = _mock_session()
        return DiagnosticInviteRepo(session), session

    def test_get_by_code_found(self):
        repo, session = self._make_repo()
        fake = MagicMock()
        session.scalar.return_value = fake
        result = repo.get_by_code("CODE1")
        assert result is fake

    def test_get_by_code_not_found(self):
        repo, session = self._make_repo()
        session.scalar.return_value = None
        result = repo.get_by_code("NONE")
        assert result is None

    def test_get_active_by_student_found(self):
        repo, session = self._make_repo()
        fake = MagicMock()
        session.scalar.return_value = fake
        result = repo.get_active_by_student(uuid4())
        assert result is fake

    def test_get_active_by_student_none(self):
        repo, session = self._make_repo()
        session.scalar.return_value = None
        result = repo.get_active_by_student(uuid4())
        assert result is None

    def test_list_by_org(self):
        repo, session = self._make_repo()
        fake_invites = [MagicMock()]
        session.scalars.return_value = iter(fake_invites)
        result = repo.list_by_org(uuid4())
        assert result == fake_invites


class TestStudentDiagnosticResultRepo:
    def _make_repo(self):
        from src.repositories.student_repo import StudentDiagnosticResultRepo
        session = _mock_session()
        return StudentDiagnosticResultRepo(session), session

    def test_get_latest_for_student_found(self):
        repo, session = self._make_repo()
        fake = MagicMock()
        session.scalar.return_value = fake
        result = repo.get_latest_for_student(uuid4())
        assert result is fake

    def test_get_latest_for_student_none(self):
        repo, session = self._make_repo()
        session.scalar.return_value = None
        result = repo.get_latest_for_student(uuid4())
        assert result is None

    def test_list_by_student(self):
        repo, session = self._make_repo()
        fake_results = [MagicMock(), MagicMock()]
        session.scalars.return_value = iter(fake_results)
        result = repo.list_by_student(uuid4())
        assert result == fake_results

    def test_list_by_org(self):
        repo, session = self._make_repo()
        fake_results = [MagicMock()]
        session.scalars.return_value = iter(fake_results)
        result = repo.list_by_org(uuid4())
        assert result == fake_results
