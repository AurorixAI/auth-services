"""N13 P1: excused absences - reasons on save, journal payload, admin API (PostgreSQL)."""
from datetime import timedelta

import pytest

from src.models.auth import AttendanceExcuse, AttendanceExcuseFile, GroupAttendance, GroupAttendanceAudit

from tests.pg_world import (  # noqa: F401  (fixtures)
    LIVE_SLOT, business_today, excused_lesson, make_client, needs_pg, pg_session, reconcile, save, world,
)
from src.auth.schemas import StartAttendanceRequest

pytestmark = needs_pg

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PDF = b"%PDF-1.4\n" + b"0" * 64


def key_of(day, lesson_id):
    return f"{day.isoformat()}_{lesson_id}"


def excuse_of(w, student=None):
    w.db.expire_all()
    return w.db.query(AttendanceExcuse).filter_by(student_id=(student or w.students[0]).id).one_or_none()


def journal(w, actor, is_admin=True):
    today = business_today()
    return w.svc.get_group_attendance(
        str(w.org.id), str(w.group.id), date_from=(today - timedelta(days=30)).isoformat(),
        date_to=today.isoformat(), actor_id=str(actor.id), is_org_admin=is_admin,
    )


# ============================================================================
# Saving the marks
# ============================================================================


def test_excused_without_reason_is_rejected_and_nothing_is_written(world):
    day, lesson_id = reconcile(world)
    with pytest.raises(ValueError, match="Укажите причину уважительного пропуска"):
        save(world, day, lesson_id, [(world.students[0], "excused"), (world.students[1], "present")])
    world.db.rollback()
    assert excuse_of(world) is None
    att = world.db.query(GroupAttendance).filter_by(lesson_id=lesson_id, group_id=world.group.id).one()
    assert att.records == {}


@pytest.mark.parametrize("reason", ["", "  ", "ab", "x" * 301])
def test_reason_length_is_enforced(world, reason):
    day, lesson_id = reconcile(world)
    with pytest.raises(ValueError):
        save(world, day, lesson_id, [(world.students[0], "excused")], excuseReasons={world.students[0].id: reason})


def test_excused_with_reason_creates_pending_excuse(world):
    excused_lesson(world, reason="  Болел, написал в Telegram  ")
    excuse = excuse_of(world)
    assert (excuse.status, excuse.reason) == ("pending", "Болел, написал в Telegram")
    assert excuse.reason_by == world.admin.id and excuse.reason_at is not None
    assert excuse.reviewed_by is None and excuse.org_id == world.org.id


def test_teacher_marks_excused_on_live_lesson(world):
    world.svc.start_group_attendance(
        str(world.org.id), str(world.group.id),
        StartAttendanceRequest(lessonDate=business_today(), lessonId=LIVE_SLOT),
        started_by=str(world.teacher.id), is_org_admin=False,
    )
    s = world.students[0]
    with pytest.raises(ValueError, match="причину"):
        save(world, business_today(), LIVE_SLOT, [(s, "excused")], actor=world.teacher, is_admin=False)
    world.db.rollback()
    save(world, business_today(), LIVE_SLOT, [(s, "excused")], actor=world.teacher, is_admin=False,
         excuseReasons={s.id: "Справка у врача"})
    assert excuse_of(world).reason_by == world.teacher.id


def test_changing_the_mark_deletes_excuse_and_its_files(world):
    day, lesson_id = excused_lesson(world)
    excuse = excuse_of(world)
    world.db.add(AttendanceExcuseFile(org_id=world.org.id, excuse_id=excuse.id, filename="a.png", content_type="image/png",
                                      size_bytes=10, data=b"x" * 10, uploaded_by=world.admin.id,
                                      uploaded_at=excuse.reason_at))
    world.db.commit()
    save(world, day, lesson_id, [(world.students[0], "present"), (world.students[1], "present")])
    assert excuse_of(world) is None
    assert world.db.query(AttendanceExcuseFile).filter_by(org_id=world.org.id).count() == 0


def test_resave_without_reason_keeps_existing_excuse_and_reason_can_change(world):
    day, lesson_id = excused_lesson(world)
    s = world.students[0]
    first = excuse_of(world)
    first.status, first.review_note = "confirmed", "ok"
    world.db.commit()
    # Same marks, no excuseReasons: nothing changes.
    save(world, day, lesson_id, [(s, "excused"), (world.students[1], "present")])
    assert (excuse_of(world).status, excuse_of(world).reason) == ("confirmed", "Болел, написал в Telegram")
    # A new reason for an already excused student: status stays.
    save(world, day, lesson_id, [(s, "excused")], excuseReasons={s.id: "Уехал на олимпиаду"})
    changed = excuse_of(world)
    assert (changed.status, changed.reason, changed.id) == ("confirmed", "Уехал на олимпиаду", first.id)


def test_excuse_reasons_for_non_excused_students_are_ignored(world):
    day, lesson_id = reconcile(world)
    s = world.students[1]
    save(world, day, lesson_id, [(s, "present")], excuseReasons={s.id: "лишнее"})
    assert excuse_of(world, s) is None


def test_cancelling_removes_excuses(world):
    day, lesson_id = excused_lesson(world)
    save(world, day, lesson_id, [], status="cancelled", cancellationReason="Праздник")
    assert excuse_of(world) is None


# ============================================================================
# Correcting a completed lesson
# ============================================================================


def test_admin_corrects_completed_lesson_into_excused(world):
    day, lesson_id = reconcile(world)
    everyone = [(s, "present") for s in world.students]
    save(world, day, lesson_id, everyone, status="completed")
    marker = world.db.query(GroupAttendance).filter_by(lesson_id=lesson_id, group_id=world.group.id).one()
    marker.marked_by = world.teacher.id
    world.db.commit()

    corrected = [(world.students[0], "excused"), (world.students[1], "present"), (world.students[2], "present")]
    with pytest.raises(ValueError, match="причину"):  # correction reason comes first or the excuse reason
        save(world, day, lesson_id, corrected, status="completed", excuseReasons={world.students[0].id: "Болел"})
    world.db.rollback()
    with pytest.raises(ValueError, match="Укажите причину уважительного пропуска"):
        save(world, day, lesson_id, corrected, status="completed", correctionReason="Ошибка в журнале")
    world.db.rollback()
    save(world, day, lesson_id, corrected, status="completed", correctionReason="Ошибка в журнале",
         excuseReasons={world.students[0].id: "Болел"})

    world.db.expire_all()
    att = world.db.query(GroupAttendance).filter_by(lesson_id=lesson_id, group_id=world.group.id).one()
    assert att.status == "completed" and att.records[str(world.students[0].id)] == "excused"
    assert att.marked_by == world.teacher.id  # the author of the marks is not overwritten
    assert excuse_of(world).status == "pending"

    data = journal(world, world.admin)
    key = key_of(day, lesson_id)
    assert data.corrections[key]["reason"] == "Ошибка в журнале"
    assert data.corrections[key]["by_name"] and data.corrections[key]["at"]
    assert data.excuses[key][str(world.students[0].id)]["reason"] == "Болел"


def test_teacher_cannot_touch_a_completed_lesson(world):
    day, lesson_id = reconcile(world)
    save(world, day, lesson_id, [(s, "present") for s in world.students], status="completed")
    with pytest.raises(ValueError, match="только у начатого урока"):
        save(world, day, lesson_id, [(s, "absent") for s in world.students], status="completed",
             actor=world.teacher, is_admin=False, correctionReason="Хочу поменять")


def test_completed_correction_requires_reason(world):
    day, lesson_id = reconcile(world)
    save(world, day, lesson_id, [(s, "present") for s in world.students], status="completed")
    with pytest.raises(ValueError, match="укажите причину"):
        save(world, day, lesson_id, [(s, "absent") for s in world.students], status="completed")


# ============================================================================
# Journal payload
# ============================================================================


def test_journal_exposes_excuses_and_hides_review_note_from_teachers(world):
    day, lesson_id = excused_lesson(world)
    client = make_client(world)
    excuse = excuse_of(world)
    r = client.post(f"/api/v1/auth/org/attendance/excuses/{excuse.id}/review", json={"decision": "confirmed", "note": "Справка есть"})
    assert r.status_code == 200, r.text

    admin_view = journal(world, world.admin).excuses[key_of(day, lesson_id)][str(world.students[0].id)]
    assert set(admin_view) == {"id", "status", "reason", "reason_by_name", "reason_at", "reviewed_by_name",
                               "reviewed_at", "review_note", "has_files"}
    assert admin_view["status"] == "confirmed" and admin_view["review_note"] == "Справка есть"
    assert admin_view["reviewed_by_name"] and admin_view["has_files"] is False

    teacher_view = journal(world, world.teacher, is_admin=False).excuses[key_of(day, lesson_id)][str(world.students[0].id)]
    assert "review_note" not in teacher_view and teacher_view["status"] == "confirmed"
    assert journal(world, world.admin).corrections == {}


def test_journal_http_roundtrip_with_excuse_reasons(world):
    day, lesson_id = reconcile(world)
    s = world.students[0]
    client = make_client(world)
    base = f"/api/v1/auth/org/groups/{world.group.id}/attendance"
    body = {"lessonDate": day.isoformat(), "lessonId": lesson_id, "status": "in_progress",
            "records": {str(s.id): "excused"}}
    assert client.post(base, json=body).status_code == 400
    r = client.post(base, json={**body, "excuseReasons": {str(s.id): "Болел"}})
    assert r.status_code == 200, r.text
    got = client.get(base, params={"date_from": day.isoformat(), "date_to": day.isoformat()}).json()
    assert got["excuses"][key_of(day, lesson_id)][str(s.id)]["reason"] == "Болел"
    assert got["corrections"] == {}
