"""LOGIC-B (2/2): group archive as an operation, member audit, one pending invitation per e-mail, honest student status.

Revision ID: lb2b3c4d5e6
Revises: lb1a2b3c4d5
Create Date: 2026-10-21

Spec: ``artifacts/partner_doc_check/logicB_spec.md`` 3.3.

* ``groups.inactive_intervals`` JSONB NOT NULL DEFAULT '[]' and ``groups.archive_reason`` VARCHAR(300) (O4).  DATA: every group that is
  already ``archived`` gets ``[{"from": <updated_at>, "to": null}]`` (``updated_at`` is UTC; the format is
  ``YYYY-MM-DDTHH:MM:SS``) - the schedule of an archived group never generates lessons after its archiving;
* ``group_audit`` accepts ``group_archived`` / ``group_restored`` / ``lesson_teacher_set`` / ``lesson_teacher_cleared`` (the CHECK is
  rebuilt from the live definition, so the actions added by a parallel revision are kept);
* ``member_audit`` (O2) + index ``(org_id, created_at)``;
* invitations (O10): DATA every ``pending`` invitation without an e-mail becomes ``expired`` (the owner creates a new one);
  every older duplicate ``pending`` of one (centre, lower(e-mail)) becomes ``cancelled`` (the newest stays); then the partial unique
  index ``uq_invitations_pending_email ON (org_id, lower(email)) WHERE status = 'pending'``;
* DATA (O15 / O9): ``students.status IN ('new', 'diagnosed')`` with an active membership in a live group becomes ``active``.

Idempotent: every step is guarded; a second run changes nothing.

Rollback (downgrade): drops ``member_audit`` and the index, ``groups.inactive_intervals`` / ``archive_reason`` (an archived group is
still ``status = 'archived'``), removes the ``group_audit`` rows with the four new actions and restores the CHECK.  The data steps (expired /
cancelled invitations, ``active`` students) are not reverted.
"""
from __future__ import annotations

import re

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "lb2b3c4d5e6"
down_revision: str = "lb1a2b3c4d5"
branch_labels = None
depends_on = None

GROUPS = "groups"
GROUP_AUDIT = "group_audit"
GROUP_AUDIT_CHECK = "ck_group_audit_action"
NEW_GROUP_ACTIONS = ("group_archived", "group_restored", "lesson_teacher_set", "lesson_teacher_cleared")
MEMBER_AUDIT = "member_audit"
INVITE_INDEX = "uq_invitations_pending_email"


def _check_actions(bind, table: str, name: str) -> list[str]:
    row = bind.execute(
        sa.text(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname = :t AND c.conname = :n"
        ),
        {"t": table, "n": name},
    ).scalar()
    return re.findall(r"'([a-z_]+)'", row or "")


def _set_check_actions(table: str, name: str, actions: list[str]) -> None:
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_action_check")
    listed = ", ".join(f"'{a}'" for a in actions)
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK (action IN ({listed}))")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns(GROUPS)}

    # 1. group archive
    if "inactive_intervals" not in columns:
        op.add_column(
            GROUPS,
            sa.Column("inactive_intervals", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        )
        # backfill only on the run that added the column: a re-run must not overwrite a real interval
        op.execute(
            sa.text(
                r"""UPDATE groups SET inactive_intervals = jsonb_build_array(jsonb_build_object(
                    'from', to_char(updated_at, 'YYYY-MM-DD"T"HH24\:MI\:SS'), 'to', NULL))
                    WHERE status = 'archived' AND updated_at IS NOT NULL"""
            )
        )
    if "archive_reason" not in columns:
        op.add_column(GROUPS, sa.Column("archive_reason", sa.String(300), nullable=True))

    actions = _check_actions(bind, GROUP_AUDIT, GROUP_AUDIT_CHECK)
    if actions and not set(NEW_GROUP_ACTIONS) <= set(actions):
        _set_check_actions(GROUP_AUDIT, GROUP_AUDIT_CHECK, actions + [a for a in NEW_GROUP_ACTIONS if a not in actions])

    # 2. member audit
    if MEMBER_AUDIT not in inspector.get_table_names():
        op.create_table(
            MEMBER_AUDIT,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organization_members.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("actor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("action", sa.String(30), nullable=False),
            sa.Column("snapshot", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("timezone('utc', now())")),
            sa.CheckConstraint(
                "action IN ('removed', 'restored', 'reactivated_by_invite', 'role_changed')", name="ck_member_audit_action"
            ),
        )
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes(MEMBER_AUDIT)}
    if "idx_member_audit_org" not in indexes:
        op.create_index("idx_member_audit_org", MEMBER_AUDIT, ["org_id", "created_at"])
    if "idx_member_audit_member" not in indexes:
        op.create_index("idx_member_audit_member", MEMBER_AUDIT, ["member_id", "created_at"])

    # 3. invitations
    op.execute("UPDATE invitations SET status = 'expired' WHERE status = 'pending' AND (email IS NULL OR btrim(email) = '')")
    op.execute(
        "UPDATE invitations i SET status = 'cancelled' "
        "WHERE i.status = 'pending' AND EXISTS ("
        "  SELECT 1 FROM invitations j WHERE j.org_id = i.org_id AND lower(j.email) = lower(i.email) AND j.status = 'pending' "
        "  AND (coalesce(j.created_at, 'epoch'::timestamp), j.id) > (coalesce(i.created_at, 'epoch'::timestamp), i.id))"
    )
    if INVITE_INDEX not in {i["name"] for i in sa.inspect(bind).get_indexes("invitations")}:
        op.create_index(
            INVITE_INDEX, "invitations", ["org_id", sa.text("lower(email)")],
            unique=True, postgresql_where=sa.text("status = 'pending'"),
        )

    # 4. the status of the students who are in a group
    op.execute(
        "UPDATE students s SET status = 'active' "
        "WHERE s.status IN ('new', 'diagnosed') AND EXISTS ("
        "  SELECT 1 FROM student_groups sg JOIN groups g ON g.id = sg.group_id "
        "  WHERE sg.student_id = s.id AND sg.is_active AND g.status = 'active')"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    op.execute(f"DROP INDEX IF EXISTS {INVITE_INDEX}")
    if MEMBER_AUDIT in inspector.get_table_names():
        op.drop_table(MEMBER_AUDIT)
    columns = {c["name"] for c in inspector.get_columns(GROUPS)}
    for column in ("inactive_intervals", "archive_reason"):
        if column in columns:
            op.drop_column(GROUPS, column)
    actions = _check_actions(bind, GROUP_AUDIT, GROUP_AUDIT_CHECK)
    if set(NEW_GROUP_ACTIONS) & set(actions):
        listed = ", ".join(repr(a) for a in NEW_GROUP_ACTIONS)
        op.execute(f"DELETE FROM {GROUP_AUDIT} WHERE action IN ({listed})")
        _set_check_actions(GROUP_AUDIT, GROUP_AUDIT_CHECK, [a for a in actions if a not in NEW_GROUP_ACTIONS])
