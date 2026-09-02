"""Audit explicit reconciliation of never-started lesson occurrences.

Revision ID: p6q7r8s9t0u
Revises: o5p6q7r8s9t
Create Date: 2026-09-01
"""
from __future__ import annotations

from alembic import op


revision: str = "p6q7r8s9t0u"
down_revision: str = "o5p6q7r8s9t"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The check was introduced under two possible physical names in older
    # local databases. Drop both safely before installing the canonical rule.
    op.execute(
        "ALTER TABLE group_attendance_audit "
        "DROP CONSTRAINT IF EXISTS ck_group_attendance_audit_action"
    )
    op.execute(
        "ALTER TABLE group_attendance_audit "
        "DROP CONSTRAINT IF EXISTS group_attendance_audit_action_check"
    )
    op.create_check_constraint(
        "ck_group_attendance_audit_action",
        "group_attendance_audit",
        "action IN ('started', 'reconciliation_started', 'created', 'updated', "
        "'corrected', 'cancelled', 'reconciliation_cancelled')",
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE group_attendance_audit "
        "DROP CONSTRAINT IF EXISTS ck_group_attendance_audit_action"
    )
    op.create_check_constraint(
        "ck_group_attendance_audit_action",
        "group_attendance_audit",
        "action IN ('started', 'created', 'updated', 'corrected', 'cancelled')",
    )
