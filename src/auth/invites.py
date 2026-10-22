"""Invitations of staff (LOGIC-B O10, SPEC 1 "O10" and 4.6).

* the e-mail is mandatory at creation (422 ``EMAIL_REQUIRED``), normalised ``lower().strip()``; only the owner invites an admin (403);
* a new invitation to the same e-mail cancels the previous pending one (and a partial unique index guarantees one pending per centre and e-mail);
* expired invitations become ``expired`` when they are read (list, info, accept): ``UPDATE ... WHERE status = 'pending' AND expires_at <= now``;
* accepting is atomic (the invitation row is locked ``FOR UPDATE``): one code makes at most one account / reactivation;
* the e-mail of the account is the e-mail of the invitation (a different one sent = 400 ``INVITE_EMAIL_MISMATCH``);
* an e-mail of a REMOVED member of this centre reactivates the same ``user.id`` (O2): the password is the new one, old sessions are revoked, the name is
  not overwritten, the groups do not come back; an active member = 409 ``ALREADY_MEMBER``; an e-mail of another centre = 409 ``EMAIL_IN_OTHER_ORG``
  (until the "one person, several centres" package).
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from . import business_clock as bc
from . import user_sessions
from .models import Invitation, MemberAudit, Organization, OrganizationMember, User
from .ops_rules import ERROR_CATALOGUE, OpsError, conflict, forbidden, unprocessable

logger = logging.getLogger(__name__)

INVITE_TTL_DAYS = 7
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

NOT_FOUND = "Приглашение не найдено или уже использовано"
EXPIRED = "Приглашение истекло"


def normalize_email(value: Optional[str]) -> Optional[str]:
    value = (value or "").lower().strip()
    return value or None


class Invites:
    def __init__(self, svc) -> None:
        self.svc = svc
        self.db = svc.db

    # --------------------------------------------------------------------------------------------- helpers
    def expire_due(self, *, org_id: Any = None, code: Optional[str] = None) -> int:
        q = self.db.query(Invitation).filter(Invitation.status == "pending", Invitation.expires_at <= bc.utc_now())
        if org_id is not None:
            q = q.filter(Invitation.org_id == org_id)
        if code is not None:
            q = q.filter(Invitation.code == code)
        count = q.update({Invitation.status: "expired"}, synchronize_session=False)
        return count

    def _member_of_org(self, org_id: Any, email: str) -> Optional[Any]:
        return (
            self.db.query(OrganizationMember, User)
            .join(User, User.id == OrganizationMember.user_id)
            .filter(OrganizationMember.org_id == org_id, func.lower(User.email) == email)
            .first()
        )

    def is_returning(self, org_id: Any, email: Optional[str]) -> bool:
        """The e-mail belongs to a member of this centre who was removed."""
        if not email:
            return False
        row = self._member_of_org(org_id, email)
        return bool(row and not row[0].is_active)

    @staticmethod
    def _out(invite: Invitation, *, returning: bool, aware: bool = False) -> Dict[str, Any]:
        d = invite.to_dict()
        d["is_returning"] = returning
        if aware and invite.expires_at is not None:
            d["expires_at"] = invite.expires_at.replace(tzinfo=timezone.utc).isoformat()
        return d

    # ------------------------------------------------------------------------------------------- create
    def create(self, org_id: Any, creator_id: Any, request, *, creator_role: Optional[str]) -> Dict[str, Any]:
        email = normalize_email(request.email)
        if email is None:
            raise unprocessable("EMAIL_REQUIRED", ERROR_CATALOGUE["EMAIL_REQUIRED"][1])
        if not _EMAIL.match(email):
            raise unprocessable("EMAIL_INVALID", ERROR_CATALOGUE["EMAIL_INVALID"][1])
        if request.org_role == "admin" and creator_role != "owner":
            raise forbidden("ONLY_OWNER_INVITES_ADMIN", ERROR_CATALOGUE["ONLY_OWNER_INVITES_ADMIN"][1])
        row = self._member_of_org(org_id, email)
        if row is not None and row[0].is_active:
            raise conflict("ALREADY_MEMBER", ERROR_CATALOGUE["ALREADY_MEMBER"][1])
        from .service import INVITE_CODE_LENGTH, generate_code

        for attempt in (1, 2):
            # the pending invitation of the same e-mail is cancelled (locked first: two creators in a row do not both see "none")
            self.db.query(Invitation).filter(
                Invitation.org_id == org_id, func.lower(Invitation.email) == email, Invitation.status == "pending"
            ).with_for_update().all()
            self.db.query(Invitation).filter(
                Invitation.org_id == org_id, func.lower(Invitation.email) == email, Invitation.status == "pending"
            ).update({Invitation.status: "cancelled"}, synchronize_session=False)
            invite = Invitation(
                org_id=org_id, invited_by=creator_id, email=email, org_role=request.org_role,
                code=generate_code(INVITE_CODE_LENGTH, digits_only=False), expires_at=bc.utc_now() + timedelta(days=INVITE_TTL_DAYS),
                status="pending", created_at=bc.utc_now(),
            )
            self.db.add(invite)
            try:
                self.db.commit()
                break
            except IntegrityError:
                self.db.rollback()
                if attempt == 2:
                    raise
        logger.info("Invitation created: %s for role=%s in org=%s", invite.code, request.org_role, org_id)
        out = self._out(invite, returning=bool(row and not row[0].is_active), aware=True)
        out["created_at"] = None
        return out

    # --------------------------------------------------------------------------------------------- list
    def list(self, org_id: Any) -> list:
        if self.expire_due(org_id=org_id):
            self.db.commit()
        invites = (
            self.db.query(Invitation).filter(Invitation.org_id == org_id).order_by(Invitation.created_at.desc()).limit(100).all()
        )
        emails = {normalize_email(i.email) for i in invites if i.email}
        removed = set()
        if emails:
            for (email,) in (
                self.db.query(func.lower(User.email))
                .join(OrganizationMember, OrganizationMember.user_id == User.id)
                .filter(OrganizationMember.org_id == org_id, OrganizationMember.is_active.isnot(True), func.lower(User.email).in_(emails))
                .all()
            ):
                removed.add(email)
        return [self._out(i, returning=normalize_email(i.email) in removed) for i in invites]

    # ---------------------------------------------------------------------------------------------- info
    def info(self, code: str) -> Dict[str, Any]:
        if self.expire_due(code=code):
            self.db.commit()
        invite = self.db.query(Invitation).filter(Invitation.code == code).first()
        if invite is None or invite.status not in ("pending", "expired"):
            raise ValueError(NOT_FOUND)
        if invite.status == "expired":
            raise ValueError(EXPIRED)
        org = self.db.query(Organization).filter(Organization.id == invite.org_id).first()
        return {
            "org_name": org.name if org else "",
            "org_role": invite.org_role,
            "email": invite.email,
            "is_returning": self.is_returning(invite.org_id, normalize_email(invite.email)),
        }

    # -------------------------------------------------------------------------------------------- accept
    def accept(self, request):
        from .schemas import AuthResponse  # noqa: F401  (typing only)
        from .service import hash_password

        invite = self.db.query(Invitation).filter(Invitation.code == request.code).with_for_update().populate_existing().first()
        if invite is None or invite.status not in ("pending", "expired"):
            self.db.rollback()
            raise ValueError(NOT_FOUND)
        if invite.status == "pending" and invite.expires_at is not None and invite.expires_at <= bc.utc_now():
            invite.status = "expired"
            self.db.commit()
            raise ValueError(EXPIRED)
        if invite.status == "expired":
            self.db.rollback()
            raise ValueError(EXPIRED)
        email = normalize_email(invite.email) or normalize_email(getattr(request, "email", None))
        if email is None:
            self.db.rollback()
            raise ValueError("В приглашении не указан email")
        sent = normalize_email(getattr(request, "email", None))
        if sent is not None and invite.email and sent != normalize_email(invite.email):
            self.db.rollback()
            raise OpsError(400, "INVITE_EMAIL_MISMATCH", ERROR_CATALOGUE["INVITE_EMAIL_MISMATCH"][1])
        org = self.db.query(Organization).filter(Organization.id == invite.org_id).first()
        if not org:
            self.db.rollback()
            raise ValueError("Организация не найдена")
        existing = self.db.query(User).filter(func.lower(User.email) == email).with_for_update().first()
        now = bc.utc_now()
        if existing is not None:
            own = (
                self.db.query(OrganizationMember).filter(OrganizationMember.org_id == org.id, OrganizationMember.user_id == existing.id)
                .with_for_update().first()
            )
            if own is not None and own.is_active:
                self.db.rollback()
                raise conflict("ALREADY_MEMBER", ERROR_CATALOGUE["ALREADY_MEMBER"][1])
            if own is None:
                other = self.db.query(OrganizationMember.id).filter(OrganizationMember.user_id == existing.id).first()
                self.db.rollback()
                if other is not None:
                    raise conflict("EMAIL_IN_OTHER_ORG", ERROR_CATALOGUE["EMAIL_IN_OTHER_ORG"][1])
                raise ValueError("Пользователь с таким email уже существует")
            # the same person comes back: the same user.id, a new password, the sessions of the past are over, the name is kept
            previous_role = own.org_role
            existing.password_hash = hash_password(request.password)
            existing.is_active = True
            existing.must_change_password = False
            existing.role = invite.org_role
            own.is_active = True
            own.org_role = invite.org_role
            user_sessions.revoke_all(self.db, existing.id, user_sessions.REVOKE_USER_BLOCKED)
            self.db.add(
                MemberAudit(
                    org_id=org.id, member_id=own.id, user_id=existing.id, actor_id=invite.invited_by, action="reactivated_by_invite",
                    snapshot={"invitation_id": str(invite.id), "org_role": invite.org_role, "previous_role": previous_role}, created_at=now,
                )
            )
            user = existing
        else:
            if not (request.first_name or "").strip() or not (request.last_name or "").strip():
                self.db.rollback()
                raise ValueError("Укажите имя и фамилию")
            user = User(
                email=email, password_hash=hash_password(request.password), first_name=request.first_name, last_name=request.last_name,
                role=invite.org_role, default_org_id=org.id,
            )
            self.db.add(user)
            self.db.flush()
            self.db.add(OrganizationMember(org_id=org.id, user_id=user.id, org_role=invite.org_role))
        invite.status = "accepted"
        invite.accepted_by = user.id
        invite.accepted_at = now
        tokens = self.svc._create_tokens(user)
        self.db.commit()
        logger.info("Invite %s accepted by %s", invite.code, user.email)
        from .schemas import AuthResponse

        return AuthResponse(user=self.svc._build_user_response(user), tokens=tokens, org=self.svc._build_org_response(org))
