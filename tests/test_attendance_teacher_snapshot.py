"""N13 P1: every lesson row freezes the group's teacher at creation (PostgreSQL)."""
from datetime import timedelta

from sqlalchemy import text

from src.auth.schemas import (
    GroupUpdateRequest,
    ReconcileMissedLessonRequest,
    SaveAttendanceRequest,
    StartAttendanceRequest,
)
from src.models.auth import GroupAttendance

from tests.pg_world import (  # noqa: F401  (fixtures)
    LIVE_SLOT, business_today, needs_pg, pg_session, reconcile, save, world,
)

pytestmark = needs_pg


def _row(w, day, lesson_id):
    w.db.expire_all()
    return (
        w.db.query(GroupAttendance)
        .filter_by(group_id=w.group.id, lesson_date=day.isoformat(), lesson_id=lesson_id)
        .one()
    )


def test_reconcile_start_path_snapshots_teacher(world):
    day, lesson_id = reconcile(world)
    row = _row(world, day, lesson_id)
    assert row.teacher_id == world.teacher.id
    assert row.to_dict()["teacher_id"] == str(world.teacher.id)


def test_cancel_missed_path_snapshots_teacher(world):
    day = business_today() - timedelta(days=2)
    world.svc.cancel_missed_group_attendance(
        str(world.org.id), str(world.group.id),
        ReconcileMissedLessonRequest(lessonDate=day, lessonId=f"s{day.isoweekday()}", cancellationReason="Праздник"),
        cancelled_by=str(world.admin.id), is_org_admin=True,
    )
    assert _row(world, day, f"s{day.isoweekday()}").teacher_id == world.teacher.id


def test_live_start_path_snapshots_teacher(world):
    world.svc.start_group_attendance(
        str(world.org.id), str(world.group.id),
        StartAttendanceRequest(lessonDate=business_today(), lessonId=LIVE_SLOT),
        started_by=str(world.teacher.id), is_org_admin=False,
    )
    assert _row(world, business_today(), LIVE_SLOT).teacher_id == world.teacher.id


def test_save_create_path_snapshots_teacher(world):
    # Only a cancellation may create a lesson row straight from "save".
    day = business_today() + timedelta(days=3)
    lesson_id = f"s{day.isoweekday()}"
    world.svc.save_group_attendance(
        str(world.org.id), str(world.group.id),
        SaveAttendanceRequest(lessonDate=day, lessonId=lesson_id, status="cancelled", cancellationReason="Каникулы"),
        marked_by=str(world.admin.id), is_org_admin=True,
    )
    assert _row(world, day, lesson_id).teacher_id == world.teacher.id


def test_teacher_change_never_rewrites_old_lessons(world):
    day, lesson_id = reconcile(world)
    world.svc.update_group(
        str(world.org.id), str(world.group.id),
        GroupUpdateRequest(teacher_id=str(world.teacher2.id)),
        actor_id=str(world.admin.id), actor_role="admin",
    )
    assert _row(world, day, lesson_id).teacher_id == world.teacher.id
    # LOGIC-B O5: a past lesson that ended before the change stays with the teacher of that date ...
    day2, lesson2 = reconcile(world, offset_days=2)
    assert _row(world, day2, lesson2).teacher_id == world.teacher.id
    # ... a lesson that is still ahead (today's all-day slot ends at 23:59) belongs to the new teacher.
    world.svc.start_group_attendance(
        str(world.org.id), str(world.group.id),
        StartAttendanceRequest(lessonDate=business_today(), lessonId=LIVE_SLOT),
        started_by=str(world.teacher2.id), is_org_admin=False,
    )
    assert _row(world, business_today(), LIVE_SLOT).teacher_id == world.teacher2.id
    # Saving marks on the old lesson does not touch the snapshot either.
    save(world, day, lesson_id, [(s, "present") for s in world.students], status="completed")
    assert _row(world, day, lesson_id).teacher_id == world.teacher.id


def test_backfill_sql_uses_current_group_teacher(world):
    day, lesson_id = reconcile(world)
    row = _row(world, day, lesson_id)
    world.db.execute(text("UPDATE group_attendance SET teacher_id = NULL WHERE id = :i"), {"i": row.id})
    world.db.commit()
    # The statement used by the migration's backfill.
    world.db.execute(text(
        "UPDATE group_attendance a SET teacher_id = g.teacher_id FROM groups g "
        "WHERE a.group_id = g.id AND a.teacher_id IS NULL AND a.id = :i"), {"i": row.id})
    world.db.commit()
    assert _row(world, day, lesson_id).teacher_id == world.teacher.id
