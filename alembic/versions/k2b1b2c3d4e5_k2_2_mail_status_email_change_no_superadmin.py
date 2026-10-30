"""K2-2: the status of an invitation's letter, the pending change of an e-mail, the journal actions, no ``superadmin``.

Revision ID: k2b1b2c3d4e5
Revises: k2a1b2c3d4e5
Create Date: 2026-10-30

* **G2** ``invitations.mail_status`` (``pending`` / ``sent`` / ``failed``, default ``sent``) and ``mail_status_at``; the invitation ``status`` gains ``queued``
  (a replacement whose letter is still on its way: the older pending invitation keeps working until the new letter has gone).
* **E** ``email_changes``: the pending change of an employee's e-mail (new address, SHA-256 of the token, 24 h); at most ONE live row per user.
* **C / E journal** ``member_audit.action`` gains ``password_link_sent``, ``email_change_started``, ``email_changed``.
* **H** the role ``superadmin`` leaves ``users.role`` and the two audit ``actor_role`` checks (0 such users on prod and stage; the migration refuses to run if one exists).

Only ADDS, except the check constraints that are re-made without ``superadmin``. Idempotent (``IF [NOT] EXISTS``); the downgrade restores the shape.
"""
from __future__ import annotations

from alembic import op

revision: str = "k2b1b2c3d4e5"
down_revision: str = "k2a1b2c3d4e5"
branch_labels = None
depends_on = None


def _drop_checks_like(table: str, *needles: str) -> str:
    cond = " AND ".join(f"pg_get_constraintdef(oid) LIKE '%{n}%'" for n in needles)
    return f"""
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN SELECT conname FROM pg_constraint WHERE conrelid = '{table}'::regclass AND contype = 'c' AND {cond} LOOP
                EXECUTE 'ALTER TABLE {table} DROP CONSTRAINT ' || quote_ident(r.conname);
            END LOOP;
        END $$
    """


def _add_check(table: str, name: str, expression: str) -> str:
    """LESSONS 26: a new CHECK is added ``NOT VALID`` (no scan of the table under the strong lock) and then validated (a weaker lock)."""
    return f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression}) NOT VALID"


def _validate(table: str, name: str) -> str:
    return f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # ---- H: no superadmin
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM users WHERE role = 'superadmin') THEN
                RAISE EXCEPTION 'users with the role superadmin exist: the role is removed in K2-2 (there must be none)';
            END IF;
        END $$
        """
    )
    op.execute(_drop_checks_like("users", "role", "owner", "teacher"))
    op.execute(_add_check("users", "users_role_check", "role IN ('owner', 'admin', 'teacher')"))
    op.execute(_validate("users", "users_role_check"))
    for table, name in (("student_audit", "ck_student_audit_actor_role"), ("group_audit", "ck_group_audit_actor_role")):
        op.execute(_drop_checks_like(table, "actor_role"))
        op.execute(_add_check(table, name, "actor_role IN ('owner', 'admin', 'teacher')"))
        op.execute(_validate(table, name))

    # ---- G2: the status of the letter of an invitation
    op.execute("ALTER TABLE invitations ADD COLUMN IF NOT EXISTS mail_status VARCHAR(10) NOT NULL DEFAULT 'sent'")
    op.execute("ALTER TABLE invitations ADD COLUMN IF NOT EXISTS mail_status_at TIMESTAMP")
    op.execute(_drop_checks_like("invitations", "mail_status"))
    op.execute(_add_check("invitations", "ck_invitations_mail_status", "mail_status IN ('pending', 'sent', 'failed')"))
    op.execute(_validate("invitations", "ck_invitations_mail_status"))
    op.execute(_drop_checks_like("invitations", "status", "cancelled", "pending", "accepted"))
    op.execute(_add_check("invitations", "invitations_status_check", "status IN ('pending', 'queued', 'accepted', 'expired', 'cancelled')"))
    op.execute(_validate("invitations", "invitations_status_check"))

    # ---- journal actions
    op.execute("ALTER TABLE member_audit DROP CONSTRAINT IF EXISTS ck_member_audit_action")
    op.execute(
        _add_check(
            "member_audit", "ck_member_audit_action",
            "action IN ('removed', 'restored', 'reactivated_by_invite', 'role_changed', 'password_link_sent', 'email_change_started', 'email_changed')",
        )
    )
    op.execute(_validate("member_audit", "ck_member_audit_action"))

    # ---- E: the pending change of an e-mail
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS email_changes (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            new_email VARCHAR(255) NOT NULL,
            token_hash VARCHAR(64) NOT NULL UNIQUE,
            requested_by UUID REFERENCES users(id) ON DELETE SET NULL,
            expires_at TIMESTAMP NOT NULL,
            used_at TIMESTAMP,
            revoked_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT timezone('utc', now())
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_email_changes_user ON email_changes (user_id, created_at)")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_email_changes_live ON email_changes (user_id) WHERE used_at IS NULL AND revoked_at IS NULL"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS email_changes")
    op.execute("ALTER TABLE member_audit DROP CONSTRAINT IF EXISTS ck_member_audit_action")
    op.execute("DELETE FROM member_audit WHERE action IN ('password_link_sent', 'email_change_started', 'email_changed')")
    op.execute(_add_check("member_audit", "ck_member_audit_action", "action IN ('removed', 'restored', 'reactivated_by_invite', 'role_changed')"))
    op.execute(_validate("member_audit", "ck_member_audit_action"))
    op.execute("UPDATE invitations SET status = 'cancelled' WHERE status = 'queued'")
    op.execute("ALTER TABLE invitations DROP CONSTRAINT IF EXISTS invitations_status_check")
    op.execute(_add_check("invitations", "invitations_status_check", "status IN ('pending', 'accepted', 'expired', 'cancelled')"))
    op.execute(_validate("invitations", "invitations_status_check"))
    op.execute("ALTER TABLE invitations DROP CONSTRAINT IF EXISTS ck_invitations_mail_status")
    op.execute("ALTER TABLE invitations DROP COLUMN IF EXISTS mail_status_at")
    op.execute("ALTER TABLE invitations DROP COLUMN IF EXISTS mail_status")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check")
    op.execute(_add_check("users", "users_role_check", "role IN ('owner', 'admin', 'teacher', 'superadmin')"))
    op.execute(_validate("users", "users_role_check"))
    for table, name in (("student_audit", "ck_student_audit_actor_role"), ("group_audit", "ck_group_audit_actor_role")):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        op.execute(_add_check(table, name, "actor_role IN ('owner', 'admin', 'teacher', 'superadmin')"))
        op.execute(_validate(table, name))
