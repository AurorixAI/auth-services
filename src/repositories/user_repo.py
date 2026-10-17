"""
ALGO V1 — User & Auth Repositories  (Step 11)

Covers: User, OrganizationMember
"""

from __future__ import annotations

from typing import Optional, List
from uuid import UUID

from sqlalchemy import select

from src.models.auth import User, OrganizationMember
from src.repositories.base import BaseRepository


class UserRepo(BaseRepository[User]):
    model = User

    def get_by_email(self, email: str) -> Optional[User]:
        stmt = select(User).where(User.email == email)
        return self.session.scalar(stmt)

    def email_exists(self, email: str) -> bool:
        return self.get_by_email(email) is not None

    def get_active_by_email(self, email: str) -> Optional[User]:
        stmt = select(User).where(User.email == email, User.is_active.is_(True))
        return self.session.scalar(stmt)

    def get_by_id_str(self, user_id: str) -> Optional[User]:
        try:
            return self.get_by_id(UUID(user_id))
        except ValueError:
            return None

    def update_last_login(self, user: User) -> None:
        from sqlalchemy.sql import func

        user.last_login_at = func.now()
        user.login_count = (user.login_count or 0) + 1
        self.session.flush()

    def list_by_org(self, org_id: UUID, role: str = None) -> List[User]:
        stmt = (
            select(User)
            .join(OrganizationMember, OrganizationMember.user_id == User.id)
            .where(
                OrganizationMember.org_id == org_id,
                OrganizationMember.is_active.is_(True),
            )
        )
        if role:
            stmt = stmt.where(OrganizationMember.org_role == role)
        return list(self.session.scalars(stmt))


class OrgMemberRepo(BaseRepository[OrganizationMember]):
    model = OrganizationMember

    def get_membership(
        self, org_id: UUID, user_id: UUID
    ) -> Optional[OrganizationMember]:
        stmt = select(OrganizationMember).where(
            OrganizationMember.org_id == org_id,
            OrganizationMember.user_id == user_id,
        )
        return self.session.scalar(stmt)

    def is_member(self, org_id: UUID, user_id: UUID) -> bool:
        return self.get_membership(org_id, user_id) is not None

    def list_org_members(self, org_id: UUID) -> List[OrganizationMember]:
        stmt = select(OrganizationMember).where(OrganizationMember.org_id == org_id)
        return list(self.session.scalars(stmt))
