"""LOGIC-B2 invariants INV-B2-1 ... INV-B2-9 after random sequences of operations (requests, withdrawals, the four decisions, the operations of B around them)."""
import json
import random
from collections import Counter
from datetime import date, timedelta

import pytest

from src.models.auth import GroupAttendance, GroupScheduleOneOffLesson, LessonSubstitutionEvent, LessonSubstitutionRequest, LessonTeacherAssignment

from tests.b2_helpers import API, build_d2, client, clock, needs_pg, pg_session, requests_of  # noqa: F401  (fixtures)
from tests.test_ops_b2_decide_pg import Sync, sync  # noqa: F401

pytestmark = needs_pg
SEEDS = [5, 31, 2026]
SECRET = "СЕКРЕТНАЯ-ПРИЧИНА"
TERMINAL = {"substituted", "rescheduled", "cancelled", "rejected", "withdrawn", "closed"}
DECISION_EVENTS = TERMINAL


def check(w, seen):
    w.db.expire_all()
    org = w.org.id
    reqs = requests_of(w)
    events = w.db.query(LessonSubstitutionEvent).filter_by(org_id=org).all()
    by_req = {}
    for e in events:
        by_req.setdefault(e.request_id, []).append(e.action)
    pend = Counter((r.group_id, r.lesson_id, r.lesson_date) for r in reqs if r.status == "pending")
    assert all(n == 1 for n in pend.values()), pend                                                           # INV-B2-1
    for r in reqs:
        assert (r.decided_at is None) == (r.status == "pending"), (r.status, r.decided_at)                    # INV-B2-2
        if r.id in seen and seen[r.id] != "pending":
            assert r.status == seen[r.id], (seen[r.id], r.status)
        seen[r.id] = r.status
        actions = by_req.get(r.id, [])
        assert sum(1 for a in actions if a in ("requested", "reopened")) == 1, (r.status, actions)            # INV-B2-7
        assert sum(1 for a in actions if a in DECISION_EVENTS) == (0 if r.status == "pending" else 1), (r.status, actions)
        row = w.db.query(GroupAttendance).filter_by(group_id=r.group_id, lesson_id=r.lesson_id, lesson_date=r.lesson_date).first()
        if r.status in ("cancelled", "rescheduled"):                                                          # INV-B2-4 / 5 / 8
            assert row is not None and row.status == "cancelled" and row.records == {} and row.version == 1, (r.status, row and row.status)
        if r.status == "rescheduled":
            assert r.moved_to_lesson_id and w.db.query(GroupScheduleOneOffLesson).filter_by(id=r.moved_to_lesson_id, group_id=r.group_id).count() == 1
        else:
            assert r.moved_to_lesson_id is None
    asg = Counter((a.group_id, a.lesson_id, a.lesson_date) for a in w.db.query(LessonTeacherAssignment).filter_by(org_id=org, cancelled_at=None))
    assert all(n == 1 for n in asg.values()), asg                                                             # INV-B2-3 (one active assignment)
    for r in reqs:
        if r.status == "substituted" and r.assignment_id:
            a = w.db.get(LessonTeacherAssignment, r.assignment_id)
            if a is not None and a.cancelled_at is None:
                assert a.teacher_id == r.substitute_id
    # INV-B2-9: what a student / a stranger reads never carries a reason
    for s in (w.s1, w.s4):
        text = json.dumps(client(w, "admin").get(f"{API}/org/students/{s.id}/attendance", params={"month": "2026-10"}).json(), ensure_ascii=False)
        assert SECRET not in text
    by_id = {str(r.id): r for r in reqs}
    for key in ("A", "B", "C", "D"):
        occ = client(w, key).get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-10-21", "date_to": "2026-11-20"}).json()
        assert SECRET not in json.dumps(occ, ensure_ascii=False)
        for o in occ:
            mark = o.get("substitutionRequest")
            if mark:                                                  # a teacher sees the mark of HIS OWN requests only
                assert by_id[mark["id"]].requester_id == getattr(w, key).id


@pytest.mark.parametrize("seed", SEEDS)
def test_b2_invariants_after_random_operations(pg_session, clock, sync, seed):
    rnd = random.Random(seed)
    clock.set("2026-10-21 14:00")
    w = build_d2(pg_session)
    staff = ["A", "B", "C", "D"]
    lessons = [(w.G1, "lesson-l1", 1), (w.G1, "lesson-l2", 3), (w.G2, "lesson-l3", 2), (w.G3, "lesson-l4", 1), (w.G4, "lesson-l5", 4)]
    seen: dict = {}
    for step in range(70):
        op = rnd.choice(["create"] * 8 + ["withdraw"] * 2 + ["decide"] * 8 + ["put", "put", "handover", "teacher", "start", "archive", "restore", "remove", "time", "list", "list"])
        w.db.expire_all()
        pending = [r for r in requests_of(w) if r.status == "pending"]
        group, lesson_id, dow = rnd.choice(lessons)
        day = (date(2026, 10, 21) + timedelta(days=rnd.randint(0, 24)))
        day = day + timedelta(days=(dow - day.isoweekday()) % 7)
        if op == "create":
            from src.auth.lesson_teachers import TeacherBook
            from src.models.auth import Group as _G

            g_row = w.db.get(_G, group.id)
            end_m = {"lesson-l1": 690, "lesson-l2": 1050, "lesson-l3": 660, "lesson-l4": 690, "lesson-l5": 1050}[lesson_id]
            led = TeacherBook(w.db).resolve(g_row, lesson_id, day.isoformat(), end_m).teacher_id
            teacher = next((k for k in staff if getattr(w, k).id == led), rnd.choice(staff))
            r = client(w, teacher).post(f"{API}/org/substitutions", json={"lessons": [{"groupId": str(group.id), "lessonId": lesson_id, "lessonDate": day.isoformat()}],
                                                                       "reasonCode": rnd.choice(["sick", "personal"]), "comment": SECRET + " " + str(step)})
        elif op == "withdraw" and pending:
            p = rnd.choice(pending)
            owner = next((k for k in staff if getattr(w, k).id == p.requester_id), "A")
            r = client(w, owner).post(f"{API}/org/substitutions/{p.id}/withdraw")
        elif op == "decide" and pending:
            p = rnd.choice(pending)
            kind = rnd.choice(["substitute", "substitute", "cancel", "reject", "reschedule"])
            body = {"decision": kind}
            if kind == "substitute":
                body["teacherId"] = str(getattr(w, rnd.choice(staff)).id)
            elif kind in ("cancel", "reject"):
                body["reason"] = SECRET
            else:
                nd = day + timedelta(days=rnd.randint(1, 8))
                body.update(newDate=nd.isoformat(), startTime=f"{rnd.randint(8, 18):02d}:00", endTime=f"{rnd.randint(19, 21):02d}:00", reason=SECRET)
            r = client(w, rnd.choice(["admin", "owner"])).post(f"{API}/org/substitutions/{p.id}/decide", json=body)
        elif op == "put":
            r = client(w, "admin").put(f"{API}/org/groups/{group.id}/lesson-teacher", json={"lessonDate": day.isoformat(), "lessonId": lesson_id,
                                                                                        "teacherId": rnd.choice([None] + [str(getattr(w, k).id) for k in staff]), "reason": "Тест"})
        elif op == "handover":
            r = client(w, "owner").post(f"{API}/org/members/{w.ids('member_' + rnd.choice(staff))}/handover",
                                        json={"assignments": [{"groupId": str(group.id), "teacherId": str(getattr(w, rnd.choice(staff)).id)}]})
        elif op == "teacher":
            r = client(w, "admin").put(f"{API}/org/groups/{group.id}", json={"teacher_id": str(getattr(w, rnd.choice(staff)).id)})
        elif op == "start":
            r = client(w, rnd.choice(["admin"] + staff)).post(f"{API}/org/groups/{group.id}/attendance/start", json={"lessonDate": clock.today.isoformat(), "lessonId": lesson_id})
        elif op == "archive":
            r = client(w, "admin").post(f"{API}/org/groups/{group.id}/archive", json={"missedLessonsAction": "cancel"})
        elif op == "restore":
            r = client(w, "admin").post(f"{API}/org/groups/{group.id}/restore", json={})
        elif op == "remove":
            r = client(w, "owner").delete(f"{API}/org/members/{w.ids('member_' + rnd.choice(['D', 'C']))}")
        elif op == "time":
            clock.set(f"{(date(2026, 10, 21) + timedelta(days=rnd.randint(0, 8))).isoformat()} {rnd.randint(8, 20):02d}:{rnd.choice(['00', '30'])}")
            continue
        elif op == "list":
            r = client(w, "admin").get(f"{API}/org/substitutions", params={"status": "pending"})
            w.db.expire_all()
            for p in requests_of(w):                                                                          # INV-B2-6 after the lazy pass
                if p.status == "pending":
                    from src.auth.lesson_teachers import TeacherBook
                    from src.models.auth import Group
                    g = w.db.get(Group, p.group_id)
                    assert g.status == "active" and p.starts_at > __import__("src.auth.business_clock", fromlist=["x"]).utc_now() - timedelta(days=62)
                    assert w.db.query(GroupAttendance).filter_by(group_id=p.group_id, lesson_id=p.lesson_id, lesson_date=p.lesson_date).count() == 0
                    end = next((int(o["endTime"][:2]) * 60 + int(o["endTime"][3:]) for o in w.svc._resolved_occurrences_for_group(
                        g, date_from=date.fromisoformat(p.lesson_date), date_to=date.fromisoformat(p.lesson_date)) if o["id"] == p.lesson_id), None)
                    assert end is not None, "the lesson of a pending request must exist"
                    assert TeacherBook(w.db).resolve(g, p.lesson_id, p.lesson_date, end).teacher_id == p.requester_id
        else:
            continue
        assert r.status_code < 500, (op, r.status_code, r.text[:300])
        check(w, seen)
    stats = Counter(r.status for r in requests_of(w))
    print("B2-STATS", seed, dict(stats))
    assert sum(stats.values()) >= 3, stats                                       # the run really exercised requests
