"""LOGIC-B O1 - a recurring slot has "valid from": no lesson, no "unclosed lesson" before it.  Matrix rows [O1.R1] ... [O1.R9]."""
import logging
from datetime import date, datetime

import pytest

from src.auth.ops_schemas import CreateLessonRequestB as CreateLessonRequest
from src.models.auth import GroupAttendance, Group

from tests.ops_helpers import (  # noqa: F401  (fixtures)
    build_d0, clock, client, code_of, exams, new_group, new_slot, new_student, needs_pg, pg_session, run_parallel,
)

pytestmark = needs_pg

OPEN = "/api/v1/auth/org/attendance/open-actions"
OCC = "/api/v1/auth/org/schedule/occurrences"
LESSONS = "/api/v1/auth/org/schedule/lessons"


@pytest.fixture
def w(pg_session, clock):
    world = build_d0(pg_session)
    # G5 "10-А": its students joined on 01.09, no schedule yet (chain 4)
    world.G5 = new_group(pg_session, world.org, world.ids, "G5", "10-А", world.C, [], class_level=10)
    for i, name in enumerate(("Асадов", "Баков", "Гулов")):
        new_student(pg_session, world.org, world.ids, f"g5s{i}", name, "Али", world.G5, class_level=10)
    pg_session.commit()
    return world


def body(group, **over):
    data = {"groupId": str(group.id), "dayOfWeek": 1, "startTime": "10:00", "endTime": "11:30", "subject": None, "room": None,
            "format": "offline", "color": "blue", "topic": None}
    data.update(over)
    return data


def occurrences(c, group, d_from="2026-10-01", d_to="2026-11-10"):
    items = c.get(OCC, params={"date_from": d_from, "date_to": d_to}).json()
    return [i["occurrenceDate"] for i in items if i["groupId"] == str(group.id)]


def test_r1_new_slot_starts_today_and_makes_no_phantom_missed_lessons(w):
    """[O1.R1] Wed 21.10: Monday 10:00 added to a group whose students joined on 01.09 - lessons from 26.10, nothing missed before."""
    c = client(w, "admin")
    r = c.post(LESSONS, json=body(w.G5))
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["validFrom"] == "2026-10-21" and out["room"] is None and out["warnings"] == []
    assert occurrences(c, w.G5) == ["2026-10-26", "2026-11-02", "2026-11-09"]
    missed = [a for a in c.get(OPEN).json() if a["groupId"] == str(w.G5.id)]
    assert missed == []                                      # the old behaviour: 05.10, 12.10, 19.10 and more


def test_r2_double_click_is_one_slot_also_without_a_teacher(w):
    """[O1.R2] The same body twice: one slot, the second is 409 DUPLICATE_SLOT - with or without a group teacher."""
    c = client(w, "admin")
    assert c.post(LESSONS, json=body(w.G5)).status_code == 201
    second = c.post(LESSONS, json=body(w.G5))
    assert second.status_code == 409 and second.json()["detail"]["code"] == "DUPLICATE_SLOT"
    assert second.json()["detail"]["message"] == "Такой урок уже есть в расписании группы"
    w.G5.teacher_id = None
    w.db.commit()
    third = c.post(LESSONS, json=body(w.G5))
    assert third.status_code == 409 and third.json()["detail"]["code"] == "DUPLICATE_SLOT"
    w.db.refresh(w.G5)
    assert len(w.G5.schedule) == 1


def test_r3_no_deletion_and_end_before_start(w):
    """[O1.R3] A slot added by mistake is never deleted, it is closed from a future date; an end before its start is 422; validFrom cannot be changed."""
    c = client(w, "admin")
    lesson = c.post(LESSONS, json=body(w.G5, validFrom="2026-11-02")).json()
    assert c.delete(f"{LESSONS}/{lesson['id']}").status_code == 410
    early = c.post(f"{LESSONS}/{lesson['id']}/recurrence/end", json={"effectiveFrom": "2026-10-23", "reason": "по ошибке", "confirmReleaseExams": True})
    assert early.status_code == 422 and early.json()["detail"]["code"] == "END_BEFORE_START"
    # the recurrence PUT cannot move validFrom (the field does not exist there)
    c.put(f"{LESSONS}/{lesson['id']}/recurrence", json={"effectiveFrom": "2026-11-10", "reason": "перенос", "endTime": "12:00", "validFrom": "2026-12-01"})
    w.db.refresh(w.G5)
    assert [s["validFrom"] for s in w.G5.schedule] == ["2026-11-02"]


def test_r4_two_admins_add_slots_at_once_no_slot_is_lost(w, monkeypatch):
    """[O1.R4] The JSON slot list is written under the group lock: two different slots at once - both stay; the same slot - one success."""
    from src.auth import schedule_rules

    real = schedule_rules.ScheduleRules._candidate_dates

    def slow(self, *a, **kw):
        import time

        time.sleep(0.4)  # inside the lock: the other request is on the lock now
        return real(self, *a, **kw)

    monkeypatch.setattr(schedule_rules.ScheduleRules, "_candidate_dates", slow)
    org, gid, admin = str(w.org.id), str(w.G5.id), str(w.admin.id)

    def add(day):
        return lambda svc: svc.create_lesson(
            org, CreateLessonRequest(groupId=gid, dayOfWeek=day, startTime="10:00", endTime="11:00", room=None), actor_id=admin, is_org_admin=True)

    results = run_parallel(w, add(2), add(4))
    assert all(err is None for _r, err in results), results
    w.db.expire_all()
    assert sorted(s["dayOfWeek"] for s in w.db.get(Group, w.G5.id).schedule) == [2, 4]
    same = run_parallel(w, add(5), add(5))
    codes = sorted([("ok" if err is None else code_of(err)) for _r, err in same])
    assert codes == ["DUPLICATE_SLOT", "ok"]
    w.db.expire_all()
    assert sorted(s["dayOfWeek"] for s in w.db.get(Group, w.G5.id).schedule) == [2, 4, 5]


def test_r5_valid_from_in_the_past_is_refused(w):
    """[O1.R5] validFrom = 14.10 on 21.10 -> 422 VALID_FROM_PAST."""
    r = client(w, "admin").post(LESSONS, json=body(w.G5, validFrom="2026-10-14"))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "VALID_FROM_PAST"
    assert r.json()["detail"]["message"] == "Дата начала не может быть в прошлом"


def test_r6_business_day_midnight_and_month_end(w, clock):
    """[O1.R6] 00:20 on Thursday 22.10 (UTC 21.10 19:20): the business day is 22.10; a slot valid from 26.10 gives a lesson on 26.10 from 00:00;
    31.10 23:50 with a Monday slot: the first lesson is 02.11."""
    clock.set("2026-10-22 00:20")
    c = client(w, "admin")
    r = c.post(LESSONS, json=body(w.G5, dayOfWeek=4, startTime="09:00", endTime="10:00"))
    assert r.status_code == 201 and r.json()["validFrom"] == "2026-10-22"            # not 21.10
    past_today = c.post(LESSONS, json=body(w.G5, dayOfWeek=4, startTime="00:10", endTime="00:15"))
    assert past_today.status_code == 400 and "прошедшее время" in past_today.json()["detail"]
    future_slot = c.post(LESSONS, json=body(w.G5, dayOfWeek=1, startTime="00:30", endTime="01:00", validFrom="2026-10-26")).json()
    assert future_slot["validFrom"] == "2026-10-26"
    assert "2026-10-26" in occurrences(c, w.G5, "2026-10-22", "2026-10-31")          # a date comparison, not a time of day one
    clock.set("2026-10-31 23:50")
    late = c.post(LESSONS, json=body(w.G5, dayOfWeek=1, startTime="14:00", endTime="15:00")).json()
    first = [o for o in occurrences(c, w.G5, "2026-10-31", "2026-11-20")]
    assert late["validFrom"] == "2026-10-31" and "2026-11-02" in first


@pytest.mark.legacy_valid_from
def test_r7_legacy_slot_without_valid_from_follows_the_group_creation_day(w, caplog):
    """[O1.R7] A slot written by an old writer (no validFrom): lessons only from the business day the group was created, and it is logged."""
    group = new_group(w.db, w.org, w.ids, "Gleg", "Старая", w.D, [{"id": "old", "dayOfWeek": 1, "startTime": "10:00", "endTime": "11:00"}],
                      created=datetime(2026, 10, 10, 9, 0))
    new_student(w.db, w.org, w.ids, "sleg", "Давлатов", "Бобур", group, joined=datetime(2026, 10, 10, 9, 5))
    w.db.commit()
    with caplog.at_level(logging.WARNING):
        dates = occurrences(client(w, "admin"), group, "2026-10-01", "2026-10-31")
    assert dates == ["2026-10-12", "2026-10-19", "2026-10-26"]                      # 05.10 is before the group existed
    assert "schedule_slot_without_valid_from" in caplog.text
    missed = [a["lessonDate"] for a in client(w, "admin").get(OPEN).json() if a["groupId"] == str(group.id)]
    assert "2026-10-05" not in missed


def test_r8_roles_range_and_archived(w):
    """[O1.R8] owner / admin create in any group, a teacher only in his own (else 403); validFrom up to +365 days; an archived group is 409."""
    assert client(w, "owner").post(LESSONS, json=body(w.G5, dayOfWeek=2, validFrom="2027-10-21")).status_code == 201     # today + 365
    too_far = client(w, "admin").post(LESSONS, json=body(w.G5, dayOfWeek=3, validFrom="2027-10-22"))
    assert too_far.status_code == 422 and too_far.json()["detail"]["code"] == "VALID_FROM_RANGE"
    own = client(w, "C").post(LESSONS, json=body(w.G5, dayOfWeek=5))                    # C teaches G5
    assert own.status_code == 201
    foreign = client(w, "D").post(LESSONS, json=body(w.G5, dayOfWeek=6))
    assert foreign.status_code == 403
    w.G5.status = "archived"
    w.db.commit()
    archived = client(w, "admin").post(LESSONS, json=body(w.G5, dayOfWeek=7))
    assert archived.status_code == 409 and archived.json()["detail"]["code"] == "GROUP_ARCHIVED"


def test_r9_imported_group_first_schedule_has_no_phantoms_and_history_is_visible(w):
    """[O1.R9] A group whose students are old and whose schedule is filled for the first time: no phantom lessons; a lesson row older than
    validFrom stays in the journal (INV-O1-4)."""
    c = client(w, "admin")
    slot = c.post(LESSONS, json=body(w.G5, dayOfWeek=3, startTime="15:00", endTime="16:00")).json()
    assert [a for a in c.get(OPEN).json() if a["groupId"] == str(w.G5.id)] == []
    # a row that exists before validFrom (a lesson that really took place) is shown by the journal
    w.db.add(GroupAttendance(org_id=w.org.id, group_id=w.G5.id, lesson_date="2026-10-14", lesson_id=slot["id"], status="completed",
                             records={}, roster_snapshot=[], teacher_id=w.C.id, marked_by=w.C.id))
    w.db.commit()
    journal = c.get(f"/api/v1/auth/org/groups/{w.G5.id}/attendance", params={"month": "2026-10"}).json()
    assert f"2026-10-14_{slot['id']}" in journal["statuses"]


def test_inv_o1_1_no_occurrence_before_valid_from_whatever_the_slot(w):
    """[INV-O1-1] For any slot and any day before validFrom there is no occurrence (a sweep over every day of two months)."""
    for valid_from in ("2026-10-05", "2026-10-21", "2026-11-02"):
        group = new_group(w.db, w.org, w.ids, f"Ginv{valid_from}", f"G{valid_from}", w.D,
                          [new_slot("s1", 1, "10:00", "11:00", valid_from), new_slot("s3", 3, "12:00", "13:00", valid_from)])
        w.db.commit()
        d_from, d_to = date(2026, 9, 28), date(2026, 11, 29)
        for item in w.svc._resolved_occurrences_for_group(group, date_from=d_from, date_to=d_to):
            assert item["occurrenceDate"] >= valid_from
        assert w.svc._resolved_occurrences_for_group(group, date_from=date.fromisoformat(valid_from), date_to=d_to)
