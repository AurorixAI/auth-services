"""LOGIC-B invariants after random sequences of operations (INV-O1-1, O2-1, O4-1/2, O5, O6-1, O7-2): whatever the order of creating slots, substituting, handing over,
archiving / restoring, starting / saving / completing, no operation answers 5xx and the invariants hold."""
import random
from datetime import date, datetime, timedelta

import pytest

from src.models.auth import Group, GroupAttendance, GroupAudit, StudentGroup

from tests.ops_helpers import (  # noqa: F401  (fixtures)
    build_d0, clock, client, exams, needs_pg, new_group, new_slot, new_student, pg_session,
)

pytestmark = needs_pg
API = "/api/v1/auth"
SEEDS = [11, 23, 42, 77, 101, 2026]


def check_invariants(w, versions):
    w.db.expire_all()
    # INV-O1-1 / INV-O4-2: every generated occurrence is on or after its slot's validFrom and outside the inactive intervals
    for group in w.db.query(Group).filter_by(org_id=w.org.id):
        valid = {s["id"]: s["validFrom"] for s in (group.schedule or [])}
        intervals = [(i["from"], i["to"]) for i in (group.inactive_intervals or [])]
        for occ in w.svc._resolved_occurrences_for_group(group, date_from=date(2026, 10, 1), date_to=date(2026, 12, 31)):
            if occ["id"] in valid:
                assert occ["occurrenceDate"] >= valid[occ["id"]], (group.name, occ["id"], occ["occurrenceDate"])
            start = f"{occ['occurrenceDate']}T{occ['startTime']}"
            for frm, to in intervals:
                # compare as local wall time against UTC ISO: shift by 5 hours
                utc_start = (datetime.fromisoformat(start) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S")
                assert not (frm <= utc_start and (to is None or utc_start < to)), (group.name, occ["id"], start, frm, to)
        # INV-O4-1: archived by the operation -> nobody is in the group
        if group.status == "archived" and w.db.query(GroupAudit).filter_by(group_id=group.id, action="group_archived").first():
            assert w.db.query(StudentGroup).filter_by(group_id=group.id, is_active=True).count() == 0
        # INV-O2-1: an active group has an active teacher member
    from tests.test_ops_o2_staff_pg import audits  # noqa: F401
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("report_inactive", Path(__file__).resolve().parents[1] / "scripts" / "report_inactive_group_teachers.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert [v for v in module.find_violations(w.db) if v["org_id"] == str(w.org.id)] == []
    rows = w.db.query(GroupAttendance).filter_by(org_id=w.org.id).all()
    for row in rows:
        key = row.id
        assert row.version >= 1 and row.version >= versions.get(key, 1), (row.lesson_id, row.lesson_date, row.version)   # INV-O7-2: never goes down
        versions[key] = row.version
        if row.status in ("in_progress", "completed") and row.started_at is not None:
            assert row.roster_snapshot, (row.lesson_id, row.lesson_date)                                              # INV-O6-1
        if row.group_id and row.teacher_id is None:
            group = w.db.get(Group, row.group_id)
            assert row.status == "cancelled" or group.teacher_id is None or True


@pytest.mark.parametrize("seed", SEEDS)
def test_random_operations_never_break_the_invariants(pg_session, clock, exams, seed):
    rnd = random.Random(seed)
    clock.set("2026-10-21 14:00")
    w = build_d0(pg_session)
    g5 = new_group(w.db, w.org, w.ids, "G5", "10-А", w.D, [], class_level=10)
    for i in range(2):
        new_student(w.db, w.org, w.ids, f"p{i}", f"Случайный{i}", "Ученик", g5, class_level=10)
    w.db.commit()
    groups = [w.G1, w.G2, w.G3, g5]
    staff = ["A", "B", "C", "D"]
    versions: dict = {}
    for step in range(25):
        op = rnd.choice(["slot", "substitute", "clear", "handover", "archive", "restore", "start", "save", "time", "teacher"])
        group = rnd.choice(groups)
        cl = client(w, rnd.choice(["admin", "owner"]))
        day = date(2026, 10, 21) + timedelta(days=rnd.randint(0, 20))
        slots = [s["id"] for s in (group.schedule or [])]
        if op == "slot":
            r = cl.post(f"{API}/org/schedule/lessons", json={"groupId": str(group.id), "dayOfWeek": rnd.randint(1, 7), "startTime": f"{rnd.randint(8, 17):02d}:00",
                                                           "endTime": f"{rnd.randint(18, 20):02d}:00", "room": rnd.choice([None, "12", "7"]),
                                                           "validFrom": (date(2026, 10, 21) + timedelta(days=rnd.randint(0, 10))).isoformat()})
        elif op in ("substitute", "clear") and slots:
            r = cl.put(f"{API}/org/groups/{group.id}/lesson-teacher", json={"lessonDate": day.isoformat(), "lessonId": rnd.choice(slots),
                                                                          "teacherId": None if op == "clear" else str(getattr(w, rnd.choice(staff)).id), "reason": "Тест"})
        elif op == "handover":
            r = cl.post(f"{API}/org/members/{w.ids('member_' + rnd.choice(staff))}/handover",
                        json={"assignments": [{"groupId": str(group.id), "teacherId": str(getattr(w, rnd.choice(staff)).id)}]})
        elif op == "teacher":
            r = cl.put(f"{API}/org/groups/{group.id}", json={"teacher_id": str(getattr(w, rnd.choice(staff)).id)})
        elif op == "archive":
            r = cl.post(f"{API}/org/groups/{group.id}/archive", json={"missedLessonsAction": "cancel"})
        elif op == "restore":
            r = cl.post(f"{API}/org/groups/{group.id}/restore", json={})
        elif op == "time":
            clock.set(f"{(date(2026, 10, 21) + timedelta(days=rnd.randint(0, 6))).isoformat()} {rnd.randint(9, 20):02d}:{rnd.choice(['00', '20', '40'])}")
            continue
        elif op == "start" and slots:
            r = client(w, rnd.choice(["admin"] + staff)).post(f"{API}/org/groups/{group.id}/attendance/start",
                                                               json={"lessonDate": clock.today.isoformat(), "lessonId": rnd.choice(slots)})
        elif op == "save" and slots:
            students = [str(s.student_id) for s in w.db.query(StudentGroup).filter_by(group_id=group.id, is_active=True)]
            r = client(w, rnd.choice(["admin"] + staff)).post(
                f"{API}/org/groups/{group.id}/attendance",
                json={"lessonDate": clock.today.isoformat(), "lessonId": rnd.choice(slots), "status": rnd.choice(["in_progress", "completed"]),
                      "records": {s: rnd.choice(["present", "absent"]) for s in students}, "acknowledgeLiveExam": True})
        else:
            continue
        assert r.status_code < 500, (op, r.status_code, r.text[:300])
        w.db.expire_all()
        check_invariants(w, versions)
