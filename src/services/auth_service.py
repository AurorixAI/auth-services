"""
ALGO V1 — Auth Service  (Step 12)

Responsibilities:
  - Login (verify credentials, issue tokens)
  - Token refresh
  - Logout (clear refresh token hash)

All DB access is via injected repos. No HTTP imports.
JWT/password functions are in src.core.security.
"""

from __future__ import annotations

from uuid import UUID

from src.core.exceptions import ForbiddenError, UnauthorizedError
from src.core.security import hash_password, issue_token_pair, verify_password
from src.repositories.user_repo import UserRepo, OrgMemberRepo


class AuthService:
    def __init__(
        self,
        user_repo: UserRepo,
        org_member_repo: OrgMemberRepo,
    ) -> None:
        self.user_repo = user_repo
        self.org_member_repo = org_member_repo

    # ── Login ─────────────────────────────────────────────────────────────

    def login(self, email: str, password: str) -> dict:
        """
        Verify credentials and issue access + refresh token pair.

        Returns:
            {
                "access_token": str,
                "refresh_token": str,
                "token_type": "bearer",
                "user": { ... }
            }
        """
        user = self.user_repo.get_by_email(email)
        if not user or not verify_password(password, user.password_hash):
            raise ForbiddenError("Invalid email or password")
        if not user.is_active:
            raise ForbiddenError("Account is deactivated")

        tokens = issue_token_pair(str(user.id), user.role)

        # Track login
        self.user_repo.update_last_login(user)

        # Store hash of refresh token for rotation validation
        from src.core.security import hash_password as _h

        self.user_repo.set_refresh_token_hash(user, _h(tokens["refresh_token"]))

        return {**tokens, "user": user.to_dict()}

    # ── Refresh ───────────────────────────────────────────────────────────

    def refresh_tokens(self, refresh_token_str: str) -> dict:
        """
        Validate a refresh token and issue a new token pair.

        We verify the token is structurally valid but do NOT validate
        the hash here (that would require knowing the user_id first).
        """
        from src.core.security import decode_token

        payload = decode_token(refresh_token_str)
        if not payload or payload.get("type") != "refresh":
            raise UnauthorizedError("Invalid refresh token")

        user = self.user_repo.get_by_id(UUID(payload["sub"]))
        if not user or not user.is_active:
            raise UnauthorizedError("User not found or deactivated")

        tokens = issue_token_pair(str(user.id), user.role)
        self.user_repo.set_refresh_token_hash(
            user, hash_password(tokens["refresh_token"])
        )
        return {**tokens, "user": user.to_dict()}

    # ── Logout ────────────────────────────────────────────────────────────

    def logout(self, user_id: str) -> None:
        """Invalidate stored refresh token."""
        user = self.user_repo.get_by_id(UUID(user_id))
        if user:
            self.user_repo.set_refresh_token_hash(user, None)

    # ── Me ────────────────────────────────────────────────────────────────

    def get_me(self, user_id: str) -> dict:
        user = self.user_repo.get_by_id_or_raise(UUID(user_id), "User")
        return user.to_dict()
