"""LOGIC-B O7 - optimistic lock of the marks (``group_attendance.version``).  Matrix rows [O7.R1] ... [O7.R9]."""
import logging
from datetime import date

import pytest

from src.models.auth import GroupAttendance

from tests.ops_helpers import (  # noqa: F401  (fixtures)
    build_d0, clock, client, code_of, exams, fixture, needs_pg, pg_session, run_parallel,
)

pytestmark = needs_pg


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 16:20")                       # L2 of G1 runs: Wed 16:00-17:30
    world = build_d0(pg_session)
    return world


def url(w, tail=""):
    return f"/api/v1/auth/org/groups/{w.G1.id}/attendance{tail}"


def save(w, who, marks, *, status="in_progress", **extra):
    records = {str(getattr(w, k).id): v for k, v in marks.items()}
    return client(w, who).post(url(w), json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2", "status": status, "records": records, **extra})


def start(w, who="A"):
    return client(w, who).post(url(w, "/start"), json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2"})


def row(w):
    w.db.expire_all()
    return w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_id="lesson-l2", lesson_date="2026-10-21").one()


def test_r1_start_is_version_1_every_save_adds_one(w):
    """[O7.R1] The answers carry the version: start 1, save with expectedVersion=1 -> 2."""
    assert start(w).json() == {"ok": True, "started": True, "version": 1}
    r = save(w, "A", {"s1": "present"}, expectedVersion=1)
    assert r.status_code == 200 and r.json()["version"] == 2 and r.json()["saved"] is True
    assert row(w).version == 2


def test_r2_a_repeat_after_a_timeout_is_a_200_without_a_write(w):
    """[O7.R2] The first save went through, the browser did not hear: the same snapshot again, even with the old expectedVersion -> 200, version unchanged."""
    start(w)
    assert save(w, "A", {"s1": "present"}, expectedVersion=1).json()["version"] == 2
    again = save(w, "A", {"s1": "present"}, expectedVersion=1)           # stale
    assert again.status_code == 200 and again.json()["version"] == 2
    assert row(w).version == 2
    audits = w.db.execute(__import__("sqlalchemy").text("SELECT count(*) FROM group_attendance_audit WHERE attendance_id=:i AND action='updated'"),
                          {"i": row(w).id}).scalar()
    assert audits == 1


def test_r3_removing_a_mark_is_part_of_the_snapshot_and_conflicts_like_any_change(w):
    """[O7.R3] Taking a mark off is a change of the snapshot: with a stale version it is a conflict (the dialog decides), with the right one it is saved."""
    start(w)
    save(w, "A", {"s1": "present", "s2": "absent"}, expectedVersion=1)                        # v2
    save(w, "admin", {"s1": "present", "s2": "absent", "s3": "present"}, expectedVersion=2)    # v3
    stale = save(w, "A", {"s1": "present"}, expectedVersion=2)                                  # the teacher removes s2 on an old screen
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "ATTENDANCE_CONFLICT"
    ok = save(w, "A", {"s1": "present"}, expectedVersion=3)
    assert ok.status_code == 200 and row(w).records == {str(w.s1.id): "present"}


def test_r4_two_tabs_conflict_and_keep_mine_chain7(w, clock):
    """[O7.R4] Chain 7: tab 2 writes v2, tab 1 (expects v1) gets 409 ATTENDANCE_CONFLICT with the current marks, nothing is written; "keep mine" repeats with
    expectedVersion=2 -> v3; completing -> v4; the admin's correction with expectedVersion=4 -> v5; the same correction again -> 200, no bump."""
    exp_conflict = fixture("c7_conflict.json")["body"]["detail"]
    start(w, "A")
    assert save(w, "A", {"s1": "present"}, expectedVersion=1).json() == {"ok": True, "saved": True, "version": 2, "examSync": None, "warnings": []}      # tab 2
    r = save(w, "A", {"s2": "absent"}, expectedVersion=1)                                                                                # tab 1
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "ATTENDANCE_CONFLICT" and detail["currentVersion"] == 2 and detail["records"] == {str(w.s1.id): "present"}
    assert detail["updatedBy"] == {"id": str(w.A.id), "name": "Иванов И."}
    assert detail["message"].startswith("Отметки уже изменил(а) Иванов И. в ") and exp_conflict["message"].startswith("Отметки уже изменил(а)")
    assert row(w).records == {str(w.s1.id): "present"} and row(w).version == 2                   # nothing written
    kept = save(w, "A", {"s1": "present", "s2": "absent"}, expectedVersion=2)
    assert kept.json()["version"] == 3
    clock.set("2026-10-21 17:20")                                                                  # the last 15 minutes: the lesson may end
    done = save(w, "A", {"s1": "present", "s2": "absent", "s3": "present"}, status="completed", expectedVersion=3)
    assert done.status_code == 200 and done.json()["version"] == 4 and done.json()["examSync"] == {"released": [], "leftRunning": [], "warning": None}
    fix = {"s1": "present", "s2": "present", "s3": "present"}
    corrected = save(w, "admin", fix, status="completed", expectedVersion=4, correctionReason="Опечатка")
    assert corrected.status_code == 200 and corrected.json()["version"] == 5
    repeat = save(w, "admin", fix, status="completed", expectedVersion=4, correctionReason="Опечатка")
    assert repeat.status_code == 200 and repeat.json()["version"] == 5 and row(w).version == 5


def test_r4b_two_writers_with_one_expected_version_exactly_one_wins(w):
    """[O7.R4] [INV-O7-1] Two parallel saves with the same expectedVersion: one success, one conflict."""
    start(w)
    org, gid = str(w.org.id), str(w.G1.id)
    from src.auth.ops_schemas import SaveAttendanceRequestB as SaveAttendanceRequest

    def do(student, state):
        def call(svc):
            req = SaveAttendanceRequest(lessonDate=date(2026, 10, 21), lessonId="lesson-l2", status="in_progress",
                                        records={getattr(w, student).id: state}, expectedVersion=1)
            return svc.save_group_attendance(org, gid, req, marked_by=str(w.A.id), is_org_admin=False, is_org_manager=False)
        return call

    outcomes = run_parallel(w, do("s1", "present"), do("s2", "absent"))
    codes = sorted("ok" if err is None else code_of(err) for _r, err in outcomes)
    assert codes == ["ATTENDANCE_CONFLICT", "ok"], outcomes
    assert row(w).version == 2


def test_r5_a_month_old_correction_checks_the_version_too(w, clock):
    """[O7.R5]"""
    clock.set("2026-11-21 12:00")
    from tests.ops_helpers import history_row

    old = history_row(w.db, w.org, w.G1, "lesson-l1", "2026-10-26", w.A, [w.s1, w.s2, w.s3], start="10:00", end="11:25", version=3)
    w.db.commit()
    cl = client(w, "admin")
    payload = {"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "status": "completed", "correctionReason": "Исправление",
               "records": {str(w.s1.id): "absent", str(w.s2.id): "present", str(w.s3.id): "present"}}
    stale = cl.post(url(w), json={**payload, "expectedVersion": 2})
    assert stale.status_code == 409 and stale.json()["detail"]["currentVersion"] == 3
    ok = cl.post(url(w), json={**payload, "expectedVersion": 3})
    assert ok.status_code == 200 and ok.json()["version"] == 4


def test_r6_midnight_does_not_matter(w, clock):
    """[O7.R6] A lesson 23:50 -> 00:10: the version does not depend on the clock."""
    from tests.ops_helpers import new_group, new_slot, new_student

    group = new_group(w.db, w.org, w.ids, "Gn", "Ночная", w.C, [new_slot("lesson-n", 3, "23:30", "23:59", "2026-10-14")])
    new_student(w.db, w.org, w.ids, "night", "Ночной", "Ученик", group)
    w.db.commit()
    clock.set("2026-10-21 23:50")
    cl = client(w, "C")
    assert cl.post(f"/api/v1/auth/org/groups/{group.id}/attendance/start", json={"lessonDate": "2026-10-21", "lessonId": "lesson-n"}).json()["version"] == 1
    clock.set("2026-10-22 00:10")
    r = cl.post(f"/api/v1/auth/org/groups/{group.id}/attendance",
                json={"lessonDate": "2026-10-21", "lessonId": "lesson-n", "status": "in_progress", "records": {str(w.ids("night")): "present"},
                      "expectedVersion": 1})
    assert r.status_code == 200 and r.json()["version"] == 2


def test_r7_a_save_without_expected_version_is_accepted_and_logged(w, caplog):
    """[O7.R7]"""
    start(w)
    with caplog.at_level(logging.INFO):
        r = save(w, "A", {"s1": "present"})
    assert r.status_code == 200 and r.json()["version"] == 2
    assert "attendance_save_without_version" in caplog.text


def test_r8_every_role_that_can_save_is_judged_alike(w):
    """[O7.R8] Owner, admin and the teacher of the lesson are all subject to the version."""
    start(w)
    save(w, "A", {"s1": "present"}, expectedVersion=1)
    for who in ("owner", "admin", "A"):
        r = save(w, who, {"s1": "present", "s2": "late"}, expectedVersion=1)
        assert r.status_code == 409, who


def test_inv_o7_2_the_version_grows_by_one_per_write(w):
    """[INV-O7-2] [INV-O7-3] +1 per write, an identical repeat adds nothing."""
    start(w)
    seen = [row(w).version]
    for marks in ({"s1": "present"}, {"s1": "present"}, {"s1": "present", "s2": "absent"}, {"s1": "absent", "s2": "absent"}):
        save(w, "A", marks)
        seen.append(row(w).version)
    assert seen == [1, 2, 2, 3, 4]
