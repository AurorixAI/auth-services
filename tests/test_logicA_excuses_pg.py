"""LOGIC-A: excuses and pauses.  A pause closes the undecided excuses of its days as «Не требуется: ученик на паузе» (not counted anywhere);
an excuse cannot be opened for a pause day (422); shortening / cancelling the pause never reopens a closed excuse."""
from datetime import timedelta

import pytest

from src.auth.attendance_excuses import PAUSE_NOT_REQUIRED, ExcusePauseRefused
from src.models.auth import AttendanceExcuse, GroupAttendanceAudit, MembershipPause

from tests.pg_world import excused_lesson, make_client, needs_pg, pg_session, save, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, membership, owner

pytestmark = needs_pg


def pause_over(w, student, day, extra=1):
    m = membership(w, student)
    r = owner(w).post(f"{API}/org/memberships/{m.id}/pauses", json={"first_day": (day - timedelta(days=1)).isoformat(),
                                                                    "last_day": (day + timedelta(days=extra)).isoformat()})
    assert r.status_code == 201, r.text
    return r.json()["pause"]


def excuse_of(w, student):
    w.db.expire_all()
    return w.db.query(AttendanceExcuse).filter_by(org_id=w.org.id, student_id=student.id).one()


def test_logicA_excuses_a_pause_closes_the_pending_excuse_of_its_days_and_nothing_reopens_it(world):
    day, lesson_id = excused_lesson(world, complete=True)              # the first student is excused (pending)
    s = world.students[0]
    assert excuse_of(world, s).status == "pending"
    pause = pause_over(world, s, day)
    e = excuse_of(world, s)
    assert (e.status, e.review_note) == ("not_required", PAUSE_NOT_REQUIRED) == ("not_required", "Не требуется: ученик на паузе")
    assert world.db.query(GroupAttendanceAudit).filter_by(attendance_id=e.attendance_id, action="excuse_closed_pause").count() == 1
    # shortened, then cancelled: the closed excuse stays closed (no resurrection)
    r = owner(world).patch(f"{API}/org/pauses/{pause['id']}", json={"first_day": (day + timedelta(days=1)).isoformat(), "last_day": (day + timedelta(days=1)).isoformat()})
    assert r.status_code == 200, r.text
    assert excuse_of(world, s).status == "not_required"
    assert owner(world).post(f"{API}/org/pauses/{pause['id']}/void", json={"reason": "вернулся"}).status_code == 200
    assert excuse_of(world, s).status == "not_required"
    # it is not in the list of the excuses that wait for a decision
    pending = owner(world).get(f"{API}/org/attendance-excuses", params={"status": "pending"})
    assert pending.status_code in (200, 404) and (pending.status_code == 404 or str(s.id) not in pending.text)


def test_logicA_excuses_a_decided_excuse_stays_as_the_record_it_is(world):
    day, lesson_id = excused_lesson(world, complete=True)
    s = world.students[0]
    excuse_of(world, s).status = "confirmed"
    world.db.commit()
    pause_over(world, s, day)
    assert excuse_of(world, s).status == "confirmed"


def test_logicA_excuses_an_excuse_for_a_pause_day_is_refused_with_the_pause_text(world):
    from tests.pg_world import reconcile

    day, lesson_id = reconcile(world, 1)
    s1, s2, s3 = world.students
    save(world, day, lesson_id, [(s1, "present"), (s2, "present"), (s3, "present")], status="completed")
    m = membership(world, s2)
    world.db.add(MembershipPause(org_id=world.org.id, membership_id=m.id, student_id=s2.id, group_id=world.group.id, first_day=day,
                                 last_day=day, created_by=world.owner.id))          # a pause set backdated (the lesson was already held)
    world.db.commit()
    with pytest.raises(ExcusePauseRefused, match="Не требуется: ученик на паузе"):
        save(world, day, lesson_id, [(s1, "present"), (s2, "excused"), (s3, "present")], status="completed", excuseReasons={s2.id: "Болел, справка есть"})
    r = owner(world).post(f"{API}/org/groups/{world.group.id}/attendance", json={
        "lessonDate": day.isoformat(), "lessonId": lesson_id, "status": "completed",
        "records": {str(s1.id): "present", str(s2.id): "excused", str(s3.id): "present"}, "excuseReasons": {str(s2.id): "Болел, справка есть"}})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "EXCUSE_ON_PAUSE" and r.json()["detail"]["message"] == PAUSE_NOT_REQUIRED
