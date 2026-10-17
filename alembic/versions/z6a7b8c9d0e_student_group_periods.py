"""Membership periods: a student who returns to a group keeps the time he spent there before (N13, ROADMAP #59).

Revision ID: z6a7b8c9d0e
Revises: y5z6a7b8c9d
Create Date: 2026-10-17

Before this revision a membership row (``student_groups``, one per student and group) knew ONE period only:
returning to a group he had left set ``joined_at`` to today and the earlier period was lost - the card history
showed the new date alone and every "was he in the group at instant X" check (rosters of missed lessons, the
open-attendance actions) treated him as absent during the earlier time.

``student_group_periods`` - one row per stay of a student in a group:

* ``membership_id`` -> ``student_groups.id`` ON DELETE CASCADE (the membership row stays ONE row per student and
  group, so the payments that point to it - ``student_payments.membership_id`` ON DELETE RESTRICT - keep their
  history);
* ``joined_at`` NOT NULL / ``left_at`` NULL, both naive UTC like ``student_groups.joined_at`` / ``left_at``;
* ``created_at``.

Invariants (kept by ``src/auth/membership_periods.py``; the first two are also enforced by the database):

* at most ONE open period (``left_at IS NULL``) per membership - a partial unique index;
* ``left_at >= joined_at`` - a CHECK;
* an open period exists if and only if the membership is active;
* the periods of a membership do not overlap;
* the current / last period has the same ``joined_at`` / ``left_at`` as the membership row, so that
  ``student_groups.joined_at`` / ``left_at`` keep their meaning and the code that only knows them keeps working.

Backfill (idempotent: memberships that already have a period are skipped):

1. every membership gets the period it has today: ``joined_at`` (a missing one falls back to the student's, then
  the group's ``created_at`` - ``student_groups`` has no ``created_at`` of its own), ``left_at`` NULL while the
  membership is active, else its ``left_at`` (a missing or earlier-than-joined one becomes ``joined_at``: a
  zero-length period that covers no instant);
2. the periods an older return overwrote are rebuilt where the log says so without any doubt: the journal row
  ``group_audit.action = 'student_archived'`` of a student carries ``membership_id``, ``joined_at`` (the start of
  the period AT THE MOMENT of archiving) and ``archived_at`` (its end).  When the student was brought back into
  the same group the return overwrote that start - and the journal row still holds the whole period.  It is
  added when it ends no later than the membership's current period starts and overlaps no period of the
  membership.  NOTHING else is rebuilt: a leave-and-return without the archive left no trace with a time in it
  (the journal records no join / leave events; attendance snapshots and payments say "he was there", not when
  he came and went), and history is not invented.

Rollback (downgrade): drops the table - the earlier periods are lost; the membership rows are not touched
(they always held the current period), so the previous code runs unchanged.  An older auth image cannot start
on a database at this head (unknown revision): downgrade first, roll the image back afterwards.
"""
from __future__ import annotations

import logging

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "z6a7b8c9d0e"
down_revision: str = "y5z6a7b8c9d"
branch_labels = None
depends_on = None

TABLE = "student_group_periods"
INDEX = "ix_student_group_periods_membership"
OPEN_INDEX = "uq_student_group_periods_open"
ORDER_CHECK = "ck_student_group_periods_order"

log = logging.getLogger("alembic.runtime.migration")

_UUID_RE = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_MOMENT_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?$"


def _index_names(inspector) -> set:
    return {ix["name"] for ix in inspector.get_indexes(TABLE)}


def upgrade() -> None:
    # Fail fast instead of queueing behind a long transaction on a hot table.
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE not in set(inspector.get_table_names()):
        op.create_table(
            TABLE,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "membership_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("student_groups.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("joined_at", sa.DateTime(), nullable=False),
            sa.Column("left_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("left_at IS NULL OR left_at >= joined_at", name=ORDER_CHECK),
        )
    inspector = sa.inspect(bind)
    indexes = _index_names(inspector)
    if INDEX not in indexes:
        op.create_index(INDEX, TABLE, ["membership_id", "joined_at"])
    if OPEN_INDEX not in indexes:
        op.create_index(
            OPEN_INDEX, TABLE, ["membership_id"], unique=True, postgresql_where=sa.text("left_at IS NULL")
        )

    # 1. The period every membership has today.
    anomalies = bind.execute(
        sa.text(
            "SELECT count(*) FROM student_groups sg "
            "WHERE NOT EXISTS (SELECT 1 FROM student_group_periods p WHERE p.membership_id = sg.id) "
            "AND ((sg.is_active IS NOT TRUE AND sg.left_at IS NULL) "
            "  OR (sg.is_active IS NOT TRUE AND sg.left_at < sg.joined_at) "
            "  OR (sg.is_active IS TRUE AND sg.left_at IS NOT NULL))"
        )
    ).scalar()
    if anomalies:
        log.warning(
            "student_group_periods backfill: %s membership row(s) are inconsistent (inactive without left_at, "
            "left_at before joined_at, or active with left_at); the active flag decides, a closed period never "
            "ends before it starts",
            anomalies,
        )
    op.execute(
        """
        INSERT INTO student_group_periods (id, membership_id, joined_at, left_at)
        SELECT gen_random_uuid(), b.membership_id, b.joined_at,
               CASE WHEN b.active THEN NULL
                    ELSE GREATEST(COALESCE(b.left_at, b.joined_at), b.joined_at) END
        FROM (
            SELECT sg.id AS membership_id,
                   COALESCE(sg.joined_at, s.created_at, g.created_at, (now() AT TIME ZONE 'UTC')) AS joined_at,
                   (sg.is_active IS TRUE) AS active,
                   sg.left_at AS left_at
            FROM student_groups sg
            JOIN students s ON s.id = sg.student_id
            JOIN groups g ON g.id = sg.group_id
            WHERE NOT EXISTS (SELECT 1 FROM student_group_periods p WHERE p.membership_id = sg.id)
        ) AS b
        """
    )

    # 2. The earlier periods the journal of archivings still holds, whole and unambiguous.
    op.execute(
        f"""
        INSERT INTO student_group_periods (id, membership_id, joined_at, left_at)
        SELECT gen_random_uuid(), r.membership_id, r.joined_at, r.archived_at
        FROM (
            SELECT DISTINCT
                   a.membership_id, a.joined_at, a.archived_at
            FROM (
                SELECT CASE WHEN ga.snapshot->>'membership_id' ~ '{_UUID_RE}'
                            THEN (ga.snapshot->>'membership_id')::uuid END AS membership_id,
                       CASE WHEN ga.snapshot->>'student_id' ~ '{_UUID_RE}'
                            THEN (ga.snapshot->>'student_id')::uuid END AS student_id,
                       CASE WHEN ga.snapshot->>'joined_at' ~ '{_MOMENT_RE}'
                            THEN (ga.snapshot->>'joined_at')::timestamp END AS joined_at,
                       CASE WHEN ga.snapshot->>'archived_at' ~ '{_MOMENT_RE}'
                            THEN (ga.snapshot->>'archived_at')::timestamp END AS archived_at
                FROM group_audit ga
                WHERE ga.action = 'student_archived'
            ) AS a
            JOIN student_groups sg ON sg.id = a.membership_id AND sg.student_id = a.student_id
            JOIN student_group_periods cur
              ON cur.membership_id = sg.id
             AND cur.joined_at = (SELECT max(p.joined_at) FROM student_group_periods p WHERE p.membership_id = sg.id)
            WHERE a.joined_at IS NOT NULL
              AND a.archived_at IS NOT NULL
              AND a.joined_at < a.archived_at
              AND a.archived_at <= cur.joined_at
              AND NOT EXISTS (
                    SELECT 1 FROM student_group_periods p
                    WHERE p.membership_id = sg.id
                      AND p.joined_at < a.archived_at
                      AND COALESCE(p.left_at, 'infinity'::timestamp) > a.joined_at)
        ) AS r
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    inspector = sa.inspect(op.get_bind())
    if TABLE in set(inspector.get_table_names()):
        op.drop_table(TABLE)  # its indexes and the CHECK go with it
