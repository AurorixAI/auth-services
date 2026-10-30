"""Invitations of staff (LOGIC-B O10, SPEC 1 "O10" and 4.6).

* the e-mail is mandatory at creation (422 ``EMAIL_REQUIRED``), normalised ``lower().strip()``; only the owner invites an admin (403);
* a new invitation to the same e-mail cancels the previous pending one (and a partial unique index guarantees one pending per centre and e-mail);
* expired invitations become ``expired`` when they are read (list, info, accept): ``UPDATE ... WHERE status = 'pending' AND expires_at <= now``;
* accepting is atomic (the invitation row is locked ``FOR UPDATE``): one code makes at most one account / reactivation;
* the e-mail of the account is the e-mail of the invitation (a different one sent = 400 ``INVITE_EMAIL_MISMATCH``);
* K2-2 (G2): the letter is sent IN THE BACKGROUND (the answer does not wait for SMTP) and ``mail_status`` follows it: ``pending`` -> ``sent`` | ``failed``.
  A replacement (a second invitation to the same address) is first ``queued``: the older pending invitation keeps working until the new letter has gone,
  then the older one is cancelled and the new one becomes ``pending`` (K2-1 m4, kept under the background).
* K2-1 (A1): the invitation goes to the invited person BY LETTER (``mail.letters.send_staff_invite``) and nowhere else: the code is the secret of the
  link in that letter and is never returned by an API, never listed, never logged (log lines carry the invitation id); ``GET /auth/invite/{code}`` shows the
  centre, the role and a MASKED e-mail; accepting by the link proves the mailbox (``is_verified``); «send again» (``resend``) = a new code by letter,
  the old one answers 410 ``link_revoked``;
* an e-mail of a REMOVED member of this centre reactivates the same ``user.id`` (O2): the password is the new one, old sessions are revoked, the name is
  not overwritten, the groups do not come back; an active member = 409 ``ALREADY_MEMBER``; an e-mail of another centre = 409 ``EMAIL_IN_OTHER_ORG``
  (until the "one person, several centres" package).
"""
from __future__ import annotations

import logging
import os
import re
from datetime import timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from . import business_clock as bc
from . import password_rules, user_sessions
from .models import Invitation, MemberAudit, Organization, OrganizationMember, User
from .ops_rules import ERROR_CATALOGUE, OpsError, conflict, forbidden, unprocessable
from .platform_errors import PlatformError

logger = logging.getLogger(__name__)

INVITE_TTL_DAYS = 7
QUEUED_MAX_MINUTES = 10    # a replacement still «queued» after this long is closed by ``expire_due``
MAIL_STALE_SECONDS = 120   # a letter that is still «pending» after this long was lost (the process stopped): the list shows it as «failed»
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def _hash_code(code: str) -> str:
    """SHA-256 of the code of the letter's link: the database keeps only this, so a dump or a backup holds no live link (K2-1, n3)."""
    from .service import hash_token

    return hash_token(code)


INVITE_MAIL_PER_PAIR = 10          # letters per hour to one mailbox from ONE centre
INVITE_MAIL_PER_EMAIL_DAY = 20     # ... and to one mailbox from ALL centres together, per day
INVITE_MAIL_PER_ORG = 100          # ... and letters of one centre per hour
TOO_MANY_TEXT = "Слишком много приглашений на этот адрес. Повторите попытку позже."

NOT_FOUND = "Приглашение не найдено или уже использовано"
EXPIRED = "Приглашение истекло"
REVOKED_TEXT = "Это приглашение заменено новым или отозвано. Используйте ссылку из последнего письма или попросите отправить приглашение снова."


def revoked_error() -> PlatformError:
    return PlatformError(410, REVOKED_TEXT, "link_revoked")


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
            q = q.filter(Invitation.code_hash == _hash_code(code))
        count = q.update({Invitation.status: "expired"}, synchronize_session=False)
        # a replacement that has waited for its letter for more than 10 minutes was lost (the process stopped between the answer and the delivery): it is closed
        # without a trace, the older invitation (marked «failed» by the list after 2 minutes) stays (K2-2 R3, 6)
        stuck = self.db.query(Invitation).filter(Invitation.status == "queued", Invitation.created_at <= bc.utc_now() - timedelta(minutes=QUEUED_MAX_MINUTES))
        if org_id is not None:
            stuck = stuck.filter(Invitation.org_id == org_id)
        count += stuck.delete(synchronize_session=False)
        return count

    def _lock_address(self, org_id: Any, email: str) -> None:
        """Serialise the creators and the background deliveries of ONE address of ONE centre (a transaction-level lock): two replacements that finish at the
        same time then promote one after the other, each cancelling the one before, instead of meeting on the unique index of the pending invitation."""
        if self.db.get_bind().dialect.name != "postgresql":
            return
        from sqlalchemy import text

        self.db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"invite:{org_id}:{email}"})

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
        stale = invite.mail_status_at is not None and (bc.utc_now() - invite.mail_status_at).total_seconds() > MAIL_STALE_SECONDS
        d["mail_status"] = "failed" if (invite.mail_status == "pending" and stale) else (invite.mail_status or "sent")
        if aware and invite.expires_at is not None:
            d["expires_at"] = invite.expires_at.replace(tzinfo=timezone.utc).isoformat()
        return d

    # ------------------------------------------------------------------------------------------- create
    def _throttle(self, org_id: Any, email: str) -> None:
        """429 once a mailbox or a centre has spent its budget of invitation letters. Counted AFTER the validation and the role check (a refused request
        spends nothing). The pair (centre, mailbox) has its own hourly budget, so another centre cannot shut a mailbox out for this one; the mailbox has
        a daily budget over all centres (a letter to a stranger is the abuse to stop)."""
        import hashlib

        from src.api.rate_limit import hit_window

        digest = hashlib.sha256(email.encode("utf-8")).hexdigest()[:32]  # no address in a Redis key
        for key, limit, window in (
            (f"rl:invite_mail_pair:{org_id}:{digest}", INVITE_MAIL_PER_PAIR, 3600),
            (f"rl:invite_mail_email_day:{digest}", INVITE_MAIL_PER_EMAIL_DAY, 86400),
            (f"rl:invite_mail_org:{org_id}", INVITE_MAIL_PER_ORG, 3600),
        ):
            count, left = hit_window(key, window)
            if count > limit:
                raise PlatformError(429, TOO_MANY_TEXT, "rate_limited", retry_after=left)

    def create(
        self, org_id: Any, creator_id: Any, request, *, creator_role: Optional[str], background=None
    ) -> Dict[str, Any]:
        """Create (or replace) the invitation; the letter goes in the BACKGROUND (``background`` = FastAPI ``BackgroundTasks``; without one the letter is
        sent right here, for a direct call of the service). The answer does not wait for SMTP: ``mail_status: "pending"``.

        A first invitation is ``pending`` at once. A replacement (the address has a pending invitation) is ``queued``: the older invitation keeps working
        and shows «sending» until the new letter has gone; then the older one is cancelled and the new one becomes ``pending`` (K2-1 m4). If the letter
        does not go, the replacement is dropped, the older invitation stays and says ``failed``; a first invitation whose letter did not go stays,
        marked ``failed``, so «Отправить ещё раз» from the list can send it again."""
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
        self._throttle(org_id, email)
        from .service import INVITE_CODE_LENGTH, generate_code

        for attempt in (1, 2):
            raw = generate_code(INVITE_CODE_LENGTH, digits_only=False)
            self._lock_address(org_id, normalize_email(email))
            # lock the pending invitations of this e-mail first (two creators in a row do not both see "none"; an accept in flight holds the row)
            previous = (
                self.db.query(Invitation)
                .filter(Invitation.org_id == org_id, func.lower(Invitation.email) == email, Invitation.status == "pending")
                .with_for_update()
                .all()
            )
            row = self._member_of_org(org_id, email)             # read again UNDER the lock: an accept that won the race has made him a member
            if row is not None and row[0].is_active:
                self.db.rollback()
                raise conflict("ALREADY_MEMBER", ERROR_CATALOGUE["ALREADY_MEMBER"][1])
            now = bc.utc_now()
            for p in previous:                                     # the older invitation keeps working; it shows «sending» until the new letter has gone
                p.mail_status, p.mail_status_at = "pending", now
            invite = Invitation(
                org_id=org_id, invited_by=creator_id, email=email, org_role=request.org_role, code_hash=_hash_code(raw),
                expires_at=now + timedelta(days=INVITE_TTL_DAYS), status="queued" if previous else "pending", created_at=now,
                mail_status="pending", mail_status_at=now,
            )
            self.db.add(invite)
            try:
                self.db.flush()
            except IntegrityError:
                self.db.rollback()
                if attempt == 2:
                    raise
                continue
            self.db.commit()
            break
        logger.info("Invitation created: id=%s role=%s org=%s", invite.id, request.org_role, org_id)
        if os.getenv("APP_ENV") == "development":  # the e2e harness of a development stack has no mailbox: it asks for the code (dev_router)
            from .dev_router import remember_code

            remember_code(email, raw)
        answer = self._sent(invite)
        invite_id = str(invite.id)
        if background is not None:
            background.add_task(deliver_in_background, invite_id, raw)
        else:
            self.deliver(invite_id, raw)
            self.db.expire_all()
            answer = self._sent(self.db.query(Invitation).filter(Invitation.id == invite.id).one())
        return answer

    def _letter_args(self, invite: Invitation, code: str) -> dict:
        """What the letter needs, as plain values (read inside a short transaction that is closed BEFORE the mail server is called)."""
        org = self.db.query(Organization).filter(Organization.id == invite.org_id).first()
        return {"to": invite.email, "centre_name": org.name if org else "", "org_role": invite.org_role, "code": code, "expires_at": invite.expires_at}

    @staticmethod
    def _send_letter(letter: dict, invite_id: Any) -> bool:
        """The letter with the link (the plain code exists only here and in the letter). Needs no database. A failure never breaks anything."""
        from src.mail import letters  # late: the mail package imports src.auth modules

        try:
            return bool(letters.send_staff_invite(letter["to"], **{k: v for k, v in letter.items() if k != "to"}))
        except Exception as exc:  # noqa: BLE001
            logger.error("Invitation letter failed: invitation=%s %s", invite_id, type(exc).__name__)
            return False

    def deliver(self, invite_id: str, code: str) -> bool:
        """The background half of ``create``: send the letter (no lock held while SMTP works), then record the outcome. Returns whether the letter went."""
        invite = self.db.query(Invitation).filter(Invitation.id == invite_id).first()
        if invite is None or invite.status not in ("pending", "queued"):
            return False                                           # cancelled / accepted meanwhile: nothing to send
        letter = self._letter_args(invite, code)
        org_id, email, my_id = invite.org_id, normalize_email(invite.email), invite.id
        self.db.rollback()                                         # the transaction ends and the connection goes back to the pool BEFORE the mail server is called (K2-2 R3, 7)
        sent = self._send_letter(letter, my_id)                    # no transaction, no connection, no lock while SMTP works (up to the 15 s time-out per step)
        # a new short transaction records the outcome:
        self._lock_address(org_id, email)
        mine = self.db.query(Invitation).filter(Invitation.id == my_id).with_for_update().populate_existing().first()
        if mine is None or mine.status not in ("pending", "queued"):
            self.db.rollback()
            return sent
        others = (
            self.db.query(Invitation)
            .filter(Invitation.org_id == org_id, func.lower(Invitation.email) == email, Invitation.status == "pending", Invitation.id != my_id)
            .with_for_update()
            .all()
        )
        now = bc.utc_now()
        if sent:
            row = self._member_of_org(org_id, email)
            if row is not None and row[0].is_active:               # he joined by the older link meanwhile: the new link has nothing to do
                mine.status, mine.mail_status = "cancelled", "sent"
            else:
                for o in others:
                    o.status = "cancelled"                         # the new letter has gone: now the older link may die (K2-1 m4)
                self.db.flush()                                    # the older one is cancelled BEFORE this one becomes pending (the unique index of pending is checked per statement)
                mine.status, mine.mail_status = "pending", "sent"
            mine.mail_status_at = now
        else:
            if mine.status == "queued":                            # a replacement that did not go is dropped without a trace; the older invitation stays, marked failed
                for o in others:
                    o.mail_status, o.mail_status_at = "failed", now
                self.db.delete(mine)
            else:
                mine.mail_status, mine.mail_status_at = "failed", now
        self.db.commit()
        return sent

    @staticmethod
    def _sent(invite: Invitation) -> Dict[str, Any]:
        """The answer for the CENTRE that typed the address: the whole address (it must see a typo), no code, no link; ``mail_status`` follows the letter."""
        return {
            "id": str(invite.id),
            "email": invite.email or "",
            "expires_at": invite.expires_at.replace(tzinfo=timezone.utc).isoformat(),
            "mail_status": invite.mail_status or "sent",
        }

    # -------------------------------------------------------------------------------------------- resend
    def resend(self, org_id: Any, invite_id: Any, creator_id: Any, *, creator_role: Optional[str], background=None) -> Dict[str, Any]:
        """«Отправить ещё раз»: a NEW code by letter; the old one is cancelled (410 ``link_revoked``). Only a pending or an expired invitation of this centre."""
        from .schemas import InviteMemberRequest

        if self.expire_due(org_id=org_id):
            self.db.commit()
        try:
            invite = (
                self.db.query(Invitation)
                .filter(Invitation.id == invite_id, Invitation.org_id == org_id, Invitation.status.in_(("pending", "expired")))
                .first()
            )
        except Exception:  # a malformed id
            self.db.rollback()
            invite = None
        if invite is None:
            raise ValueError("Приглашение не найдено")
        return self.create(
            org_id, creator_id, InviteMemberRequest(email=invite.email, org_role=invite.org_role), creator_role=creator_role, background=background
        )

    # --------------------------------------------------------------------------------------------- list
    def list(self, org_id: Any) -> list:
        if self.expire_due(org_id=org_id):
            self.db.commit()
        invites = (
            self.db.query(Invitation).filter(Invitation.org_id == org_id, Invitation.status != "queued").order_by(Invitation.created_at.desc()).limit(100).all()
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
        invite = self.db.query(Invitation).filter(Invitation.code_hash == _hash_code(code)).first()
        if invite is not None and invite.status == "cancelled":
            raise revoked_error()
        if invite is None or invite.status not in ("pending", "expired"):
            raise ValueError(NOT_FOUND)
        if invite.status == "expired":
            raise ValueError(EXPIRED)
        org = self.db.query(Organization).filter(Organization.id == invite.org_id).first()
        from src.mail.sender import mask_address

        return {
            "org_name": org.name if org else "",
            "org_role": invite.org_role,
            "email_masked": mask_address(invite.email or ""),  # the whole address is for the owner of the mailbox only
            "is_returning": self.is_returning(invite.org_id, normalize_email(invite.email)),
        }

    # -------------------------------------------------------------------------------------------- accept
    def accept(self, request):
        from .schemas import AuthResponse  # noqa: F401  (typing only)
        from .service import hash_password

        invite = self.db.query(Invitation).filter(Invitation.code_hash == _hash_code(request.code)).with_for_update().populate_existing().first()
        if invite is not None and invite.status == "cancelled":
            self.db.rollback()
            raise revoked_error()
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
        reason = password_rules.problem(request.password, email=email)  # the one rule of the cabinets, with the e-mail of the account (K2-2, F)
        if reason:
            self.db.rollback()
            raise ValueError(reason)
        sent = normalize_email(getattr(request, "email", None))
        if sent is not None and invite.email and sent != normalize_email(invite.email):
            self.db.rollback()
            raise OpsError(400, "INVITE_EMAIL_MISMATCH", ERROR_CATALOGUE["INVITE_EMAIL_MISMATCH"][1])
        org = self.db.query(Organization).filter(Organization.id == invite.org_id).first()
        if not org:
            self.db.rollback()
            raise ValueError("Организация не найдена")
        if org.blocked_at is not None:                          # a centre blocked from the console takes nobody in
            self.db.rollback()
            raise OpsError(403, "ORG_BLOCKED", "Доступ центра приостановлен. Свяжитесь с ALGO")
        known = self.db.query(User.id).filter(func.lower(User.email) == email).scalar()
        if known is not None:
            user_sessions.lock_user(self.db, known)  # lock order: the per-user lock FIRST, then the links, then the user row (LESSONS 69; K2-2 R3, 3)
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
            if existing.platform_blocked_at is not None:        # blocked from the platform console: an invitation does not lift it
                self.db.rollback()
                raise conflict("USER_BLOCKED_BY_PLATFORM", "Пользователь заблокирован командой ALGO")
            # the same person comes back: the same user.id, a new password, the sessions of the past are over, the name is kept
            previous_role = own.org_role
            existing.password_hash = hash_password(request.password)
            existing.is_active = True
            existing.is_verified = True  # the link came by letter: the mailbox is proven (K2-1, A1)
            existing.must_change_password = False
            from . import password_links  # late: it pulls in the application package

            password_links.revoke_live_resets(self.db, existing.id)  # his new password ends the reset links that are still alive
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
                role=invite.org_role, default_org_id=org.id, is_verified=True,  # the link came by letter: the mailbox is proven (K2-1, A1)
            )
            self.db.add(user)
            self.db.flush()
            self.db.add(OrganizationMember(org_id=org.id, user_id=user.id, org_role=invite.org_role))
        invite.status = "accepted"
        invite.accepted_by = user.id
        invite.accepted_at = now
        tokens = self.svc._create_tokens(user)
        self.db.commit()
        logger.info("Invitation accepted: id=%s user_id=%s", invite.id, user.id)
        from .schemas import AuthResponse

        return AuthResponse(user=self.svc._build_user_response(user), tokens=tokens, org=self.svc._build_org_response(org))


def deliver_in_background(invite_id: str, code: str) -> None:
    """The task that runs after the response of «Пригласить» / «Отправить ещё раз»: a session of its own, the letter, the recorded outcome. Never raises."""
    from .background import background_session
    from .service import AuthService

    db = background_session()
    try:
        Invites(AuthService(db)).deliver(invite_id, code)
    except Exception as exc:  # noqa: BLE001 - the row then stays «pending» and the list shows it as «failed» after two minutes
        logger.error("Invitation delivery failed: invitation=%s %s", invite_id, type(exc).__name__)
        db.rollback()
    finally:
        db.close()
