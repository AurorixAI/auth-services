"""
ALGO V1 — Organization & Invitation Repositories  (Step 11)
"""

from __future__ import annotations

from typing import List, Optional
from uuid import UUID

from sqlalchemy import select

from src.models.auth import Invitation, Organization
from src.repositories.base import BaseRepository


class OrgRepo(BaseRepository[Organization]):
    model = Organization

    def get_by_name(self, name: str) -> Optional[Organization]:
        stmt = select(Organization).where(Organization.name == name)
        return self.session.scalar(stmt)

    def list_pending(self) -> List[Organization]:
        stmt = select(Organization).where(Organization.status == "pending")
        return list(self.session.scalars(stmt))

    def list_active(self) -> List[Organization]:
        stmt = select(Organization).where(Organization.status == "active")
        return list(self.session.scalars(stmt))

    def approve(self, org: Organization, approved_by: UUID) -> Organization:
        from sqlalchemy.sql import func

        org.status = "active"
        org.approved_by = approved_by
        org.approved_at = func.now()
        self.session.flush()
        return org

    def reject(self, org: Organization, reason: str) -> Organization:
        org.status = "suspended"
        org.rejection_reason = reason
        self.session.flush()
        return org


class InvitationRepo(BaseRepository[Invitation]):
    model = Invitation

    def get_by_code(self, code: str) -> Optional[Invitation]:
        """The pending invitation of the *plain* code of a letter's link: only its SHA-256 is stored (K2-1)."""
        import hashlib

        stmt = select(Invitation).where(
            Invitation.code_hash == hashlib.sha256(code.encode()).hexdigest(), Invitation.status == "pending"
        )
        return self.session.scalar(stmt)

    def list_by_org(self, org_id: UUID) -> List[Invitation]:
        stmt = select(Invitation).where(Invitation.org_id == org_id)
        return list(self.session.scalars(stmt))

    def mark_accepted(self, invite: Invitation, accepted_by_id: UUID) -> Invitation:
        from sqlalchemy.sql import func

        invite.status = "accepted"
        invite.accepted_at = func.now()
        invite.accepted_by = accepted_by_id
        self.session.flush()
        return invite
