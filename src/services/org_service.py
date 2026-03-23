"""
ALGO V1 — Organization Service  (Step 13)

Extracted from the monolithic src/auth/service.py.
"""

from __future__ import annotations

from typing import List, Optional
from uuid import UUID

from src.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from src.core.security import hash_password
from src.models.auth import Organization, User
from src.repositories.org_repo import InvitationRepo, OrgRepo
from src.repositories.user_repo import UserRepo, OrgMemberRepo


class OrgService:
    def __init__(
        self,
        org_repo: OrgRepo,
        user_repo: UserRepo,
        org_member_repo: OrgMemberRepo,
        invitation_repo: InvitationRepo,
    ) -> None:
        self.org_repo = org_repo
        self.user_repo = user_repo
        self.org_member_repo = org_member_repo
        self.invitation_repo = invitation_repo

    def register_org(
        self,
        *,
        name: str,
        org_type: str,
        city: str = None,
        email: str = None,
        phone: str = None,
        owner_first_name: str,
        owner_last_name: str,
        owner_email: str,
        owner_password: str,
    ) -> dict:
        """
        Register a new organization with an owner account.

        The org starts in 'pending' status awaiting superadmin approval.
        """
        if self.user_repo.email_exists(owner_email):
            raise ConflictError(f"Email '{owner_email}' is already registered")

        # Create owner user
        owner = User(
            email=owner_email,
            password_hash=hash_password(owner_password),
            first_name=owner_first_name,
            last_name=owner_last_name,
            role="owner",
        )
        self.user_repo.add(owner)

        # Create org, link owner
        org = Organization(
            name=name,
            org_type=org_type,
            city=city,
            email=email,
            phone=phone,
            owner_id=owner.id,
            status="pending",
        )
        self.org_repo.add(org)

        # Set owner's default org
        owner.default_org_id = org.id
        self.user_repo.session.flush()

        return {"org": org.to_dict(), "user": owner.to_dict()}

    def approve_org(self, org_id: UUID, approved_by: UUID) -> dict:
        org = self.org_repo.get_by_id_or_raise(org_id, "Organization")
        if org.status == "active":
            raise ConflictError("Organization is already active")
        return self.org_repo.approve(org, approved_by).to_dict()

    def reject_org(self, org_id: UUID, reason: str) -> dict:
        org = self.org_repo.get_by_id_or_raise(org_id, "Organization")
        return self.org_repo.reject(org, reason).to_dict()

    def list_pending(self) -> List[dict]:
        return [o.to_dict() for o in self.org_repo.list_pending()]

    def get_org(self, org_id: UUID) -> dict:
        return self.org_repo.get_by_id_or_raise(org_id, "Organization").to_dict()

    def accept_invitation(self, code: str, first_name: str, last_name: str, password: str) -> dict:
        """Create a new user account via an invitation code."""
        invite = self.invitation_repo.get_by_code(code)
        if not invite:
            raise NotFoundError("Invitation", code)

        from datetime import datetime, timezone
        if invite.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
            raise ForbiddenError("Invitation has expired")

        if invite.email and self.user_repo.email_exists(invite.email):
            raise ConflictError(f"Email '{invite.email}' is already registered")

        user = User(
            email=invite.email or f"user_{code}@pending.local",
            password_hash=hash_password(password),
            first_name=first_name,
            last_name=last_name,
            role=invite.org_role,
            default_org_id=invite.org_id,
        )
        self.user_repo.add(user)
        self.invitation_repo.mark_accepted(invite, user.id)

        from src.models.auth import OrganizationMember
        member = OrganizationMember(
            org_id=invite.org_id,
            user_id=user.id,
            org_role=invite.org_role,
        )
        self.org_member_repo.add(member)

        return {"user": user.to_dict()}
