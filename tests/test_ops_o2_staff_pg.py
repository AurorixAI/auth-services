"""LOGIC-B O2 - removing a staff member needs the groups handed over; "return to the team".  Matrix rows [O2.R1] ... [O2.R9]."""
from datetime import date

import pytest
from fastapi import HTTPException

from src.auth.dependencies import get_current_active_user_payload
from src.auth.service import hash_password
from src.models.auth import Group, GroupAttendance, GroupAudit, MemberAudit, User

from tests.ops_helpers import (  # noqa: F401  (fixtures)
    build_d0, clock, client, code_of, exams, fixture, history_row, needs_pg, new_group, new_slot, new_student, pg_session, run_parallel,
)

pytestmark = needs_pg
M = "/api/v1/auth/org/members/{mid}"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    world = build_d0(pg_session)
    for key in "ABCD":
        user = getattr(world, key)
        user.password_hash = hash_password("secret-" + key)
    pg_session.commit()
    return world


def mid(w, key):
    return str(w.ids("member_" + key))


def audits(w, key, action=None):
    w.db.expire_all()
    q = w.db.query(MemberAudit).filter_by(member_id=w.ids("member_" + key))
    return [a for a in q.all() if action is None or a.action == action]


def handover(w, who, key, pairs, **extra):
    body = {"assignments": [{"groupId": str(getattr(w, g).id), "teacherId": str(getattr(w, t).id) if t else None} for g, t in pairs], **extra}
    return client(w, who).post(M.format(mid=mid(w, key)) + "/handover", json=body)


def test_r1_removal_needs_the_groups_handed_over_first_chain1(w):
    """[O2.R1] Chain 1: removal-impact says canRemove=false (2 groups), DELETE is 409, the handover G1 -> B is TEACHER_BUSY, G1 -> C and G2 -> B pass, then DELETE works:
    member and user inactive, member_audit(removed)."""
    ids = w.ids
    impact = client(w, "owner").get(M.format(mid=mid(w, "A")) + "/removal-impact")
    assert impact.status_code == 200 and impact.json() == ids.remap(fixture("c1_removal_impact_A.json")), impact.json()
    blocked = client(w, "owner").delete(M.format(mid=mid(w, "A")))
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "MEMBER_HAS_ASSIGNMENTS"
    assert [g["name"] for g in blocked.json()["detail"]["groups"]] == ["9-А", "7-Б"] or {g["name"] for g in blocked.json()["detail"]["groups"]} == {"9-А", "7-Б"}
    busy = handover(w, "owner", "A", [("G1", "B")])
    assert busy.status_code == 409 and busy.json()["detail"] == ids.remap(fixture("c1_handover_busy.json")["body"]["detail"])
    done = handover(w, "owner", "A", [("G1", "C"), ("G2", "B")])
    assert done.status_code == 200 and done.json() == ids.remap(fixture("c1_handover_ok.json")), done.text
    rows = {r.group_id: r for r in w.db.query(GroupAudit).filter_by(action="teacher_changed")}
    assert rows[w.G1.id].snapshot == {"teacher_id": [str(w.A.id), str(w.C.id)], "via": "handover"} and rows[w.G1.id].created_at == clock_utc(w)
    removed = client(w, "owner").delete(M.format(mid=mid(w, "A")))
    assert removed.status_code == 200 and removed.json() == ids.remap(fixture("c1_delete_member_A.json"))
    w.db.expire_all()
    assert w.member("A").is_active is False and w.db.get(User, w.A.id).is_active is False
    assert len(audits(w, "A", "removed")) == 1


def clock_utc(w):
    from datetime import datetime
    return datetime(2026, 10, 21, 9, 0)


def test_r2_double_delete_and_double_handover(w):
    """[O2.R2]"""
    assert handover(w, "owner", "A", [("G1", "C"), ("G2", "D")]).status_code == 200
    again = handover(w, "owner", "A", [("G1", "C"), ("G2", "D")])
    assert again.status_code == 200 and again.json()["handedOver"] == []
    assert client(w, "owner").delete(M.format(mid=mid(w, "A"))).status_code == 200
    second = client(w, "owner").delete(M.format(mid=mid(w, "A")))
    assert second.status_code == 200 and second.json() == {"message": "Сотрудник уже удалён", "releasedSubstitutions": 0, "closedSubstitutionRequests": 0}
    assert len(audits(w, "A", "removed")) == 1


def test_r3_return_to_the_team_restores_login_but_not_the_groups(w):
    """[O2.R3] restore: active again, the same role, the old password works, the groups do NOT come back; NOT_REMOVED when already active."""
    from src.auth.schemas import LoginRequest

    handover(w, "owner", "A", [("G1", "C"), ("G2", "D")])
    client(w, "owner").delete(M.format(mid=mid(w, "A")))
    with pytest.raises(Exception):
        w.svc.login(LoginRequest(email=w.A.email, password="secret-A"))                          # removed: no login (INV-O2-2)
    r = client(w, "owner").post(M.format(mid=mid(w, "A")) + "/restore")
    assert r.status_code == 200 and r.json() == {"member_id": mid(w, "A"), "is_active": True}
    w.db.expire_all()
    assert w.member("A").is_active and w.member("A").org_role == "teacher" and w.db.get(User, w.A.id).is_active
    assert w.svc.login(LoginRequest(email=w.A.email, password="secret-A")).user.email == w.A.email     # INV-O2-3
    assert w.db.query(Group).filter_by(teacher_id=w.A.id).count() == 0                                # the groups stay where they were put
    again = client(w, "owner").post(M.format(mid=mid(w, "A")) + "/restore")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "NOT_REMOVED"
    assert [a.action for a in audits(w, "A")] == ["removed", "restored"]


def test_r4_removal_and_a_start_are_serialised_and_two_admins_changing_a_teacher_do_not_lose_an_update(w, clock):
    """[O2.R4] The owner removes A while the admin starts A's lesson in 7-Б: either the lesson is already in progress (409 MEMBER_HAS_ASSIGNMENTS) or the removal came first
    (and the groups have to be handed over, so it is refused anyway) - never a removed member with a running lesson; two administrators changing a group's teacher leave a consistent audit chain."""
    from src.auth.schemas import StartAttendanceRequest, GroupUpdateRequest

    clock.set("2026-10-27 10:05")
    org = str(w.org.id)
    results = run_parallel(
        w,
        lambda svc: svc.remove_member(org, mid(w, "A"), actor_id=str(w.owner.id)),
        lambda svc: svc.start_group_attendance(org, str(w.G2.id), StartAttendanceRequest(lessonDate=date(2026, 10, 27), lessonId="lesson-l3"),
                                               started_by=str(w.admin.id), is_org_admin=True),
    )
    w.db.expire_all()
    assert w.member("A").is_active is True                                                       # A still teaches 9-А and 7-Б: never removable here
    assert code_of(results[0][1]) == "MEMBER_HAS_ASSIGNMENTS"
    # two admins change the teacher of one group at once
    out = run_parallel(
        w,
        lambda svc: svc.update_group(org, str(w.G2.id), GroupUpdateRequest(teacher_id=str(w.C.id)), actor_id=str(w.admin.id), actor_role="admin"),
        lambda svc: svc.update_group(org, str(w.G2.id), GroupUpdateRequest(teacher_id=str(w.D.id)), actor_id=str(w.owner.id), actor_role="owner"),
    )
    assert all(err is None for _r, err in out), out
    w.db.expire_all()
    chain = [a.snapshot["teacher_id"] for a in w.db.query(GroupAudit).filter_by(group_id=w.G2.id, action="teacher_changed").order_by(GroupAudit.created_at)]
    assert len(chain) == 2 and chain[0][0] == str(w.A.id) and chain[1][0] == chain[0][1]           # the second saw the first: no lost update


def test_r5_missed_lessons_do_not_block_and_stay_with_the_admins(w, clock):
    """[O2.R5] Missed lessons of A do not block the removal; they stay in the admins' list under A; once reconciled the row is A's (who taught), marked_by the admin."""
    handover(w, "owner", "A", [("G1", "C"), ("G2", "D")])
    assert client(w, "owner").delete(M.format(mid=mid(w, "A"))).status_code == 200
    items = client(w, "admin").get("/api/v1/auth/org/attendance/open-actions").json()
    missed = [i for i in items if i["lessonId"] == "lesson-l3" and i["actionType"] == "missed"]
    assert [m["lessonDate"] for m in missed] == ["2026-10-20"] and missed[0]["teacherId"] == str(w.A.id)
    r = client(w, "admin").post(f"/api/v1/auth/org/groups/{w.G2.id}/attendance/reconcile", json={"lessonDate": "2026-10-20", "lessonId": "lesson-l3"})
    assert r.status_code == 200 and r.json() == {"ok": True, "reconciled": True, "version": 1}
    row = w.db.query(GroupAttendance).filter_by(group_id=w.G2.id, lesson_date="2026-10-20").one()
    assert (row.teacher_id, row.marked_by) == (w.A.id, w.admin.id)


def test_r6_the_border_of_a_month_does_not_move_old_lessons(w, clock):
    """[O2.R6] Removing on 31.10 changes nothing about October's rows: they keep teacher_id; the handover only moves what is ahead."""
    clock.set("2026-10-31 12:00")
    before = {(r.lesson_date, r.lesson_id): r.teacher_id for r in w.db.query(GroupAttendance).filter_by(org_id=w.org.id)}
    assert handover(w, "owner", "A", [("G1", "C"), ("G2", "D")]).status_code == 200
    assert client(w, "owner").delete(M.format(mid=mid(w, "A"))).status_code == 200
    after = {(r.lesson_date, r.lesson_id): r.teacher_id for r in w.db.query(GroupAttendance).filter_by(org_id=w.org.id)}
    assert before == after and all(t == w.A.id for t in after.values())


def test_r7_no_member_no_groups_and_no_user(w):
    """[O2.R7] An unknown member is 404; a teacher without groups is removed at once; a handover of an unknown group is 404."""
    assert client(w, "owner").delete(M.format(mid="00000000-0000-4000-8000-000000000000")).status_code == 404
    assert client(w, "owner").get(M.format(mid="not-a-uuid") + "/removal-impact").status_code == 404
    group_less = client(w, "owner").delete(M.format(mid=mid(w, "D")))
    assert group_less.status_code == 200 and group_less.json()["releasedSubstitutions"] == 0
    missing = client(w, "owner").post(M.format(mid=mid(w, "A")) + "/handover", json={"assignments": [{"groupId": "00000000-0000-4000-8000-000000000000", "teacherId": str(w.C.id)}]})
    assert missing.status_code == 404


def test_r8_who_may_do_what(w):
    """[O2.R8] Owner: all four; admin: handover yes, DELETE / restore 403; teacher 403; the owner is not removable; teacher -> admin with groups is MEMBER_HAS_ASSIGNMENTS."""
    assert handover(w, "admin", "A", [("G1", "C")]).status_code == 200
    assert client(w, "admin").delete(M.format(mid=mid(w, "D"))).status_code == 403
    assert client(w, "admin").post(M.format(mid=mid(w, "D")) + "/restore").status_code == 403
    for method, tail in (("get", "/removal-impact"), ("post", "/handover")):
        r = getattr(client(w, "C"), method)(M.format(mid=mid(w, "A")) + tail, **({"json": {"assignments": []}} if method == "post" else {}))
        assert r.status_code == 403
    assert client(w, "C").delete(M.format(mid=mid(w, "A"))).status_code == 403
    assert client(w, "owner").delete(M.format(mid=mid(w, "owner"))).status_code == 400
    role = client(w, "owner").put(M.format(mid=mid(w, "A")) + "/role", params={"role": "admin"})
    assert role.status_code == 409 and role.json()["detail"]["code"] == "MEMBER_HAS_ASSIGNMENTS"
    assert client(w, "owner").put(M.format(mid=mid(w, "D")) + "/role", params={"role": "admin"}).status_code == 200
    assert [a.action for a in audits(w, "D")] == ["role_changed"]
    assert client(w, "owner").delete(M.format(mid=mid(w, "D"))).status_code == 200                      # an admin with no groups: no dialog


def test_r9_an_admin_with_groups_from_a_bad_import_is_blocked_and_healed_the_same_way(w):
    """[O2.R9] The old import could make an admin the "teacher" of groups: the same block and the same handover (it heals bad data)."""
    w.G3.teacher_id = w.admin.id
    w.db.commit()
    blocked = client(w, "owner").delete(M.format(mid=mid(w, "admin")))
    assert blocked.status_code == 409 and [g["name"] for g in blocked.json()["detail"]["groups"]] == ["8-В"]
    assert handover(w, "owner", "admin", [("G3", "C")]).status_code == 200
    assert client(w, "owner").delete(M.format(mid=mid(w, "admin"))).status_code == 200


def test_unassigned_handover_needs_the_confirmation_and_the_target_must_teach(w):
    """[O2.R1] teacherId null needs confirmUnassigned (422); the target must be an active teacher (422 TARGET_NOT_TEACHER); all or nothing (INV-O2-5)."""
    r = handover(w, "owner", "A", [("G1", None)])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "UNASSIGNED_NOT_CONFIRMED"
    r = client(w, "owner").post(M.format(mid=mid(w, "A")) + "/handover", json={"assignments": [{"groupId": str(w.G1.id), "teacherId": str(w.foreign_admin.id)}]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "TARGET_NOT_TEACHER"            # not a member of this centre
    for who in ("owner", "admin"):                                                                    # W1 round 2: only a teacher can take a group
        refused = handover(w, "owner", "A", [("G1", who)])
        assert refused.status_code == 422 and refused.json()["detail"]["code"] == "TARGET_NOT_TEACHER"
    r = handover(w, "owner", "A", [("G1", "C"), ("G2", "A")])
    assert r.status_code == 422                                                                       # the same person is not a target
    w.db.expire_all()
    assert {g.id: g.teacher_id for g in w.db.query(Group).filter(Group.org_id == w.org.id)}[w.G1.id] == w.A.id        # nothing moved
    ok = handover(w, "owner", "A", [("G1", None), ("G2", "D")], confirmUnassigned=True)
    assert ok.status_code == 200 and ok.json()["handedOver"][0]["toTeacherId"] is None


def test_substitutions_of_a_removed_member_are_released(w):
    """[O2.R1] Future substitutions where the removed member substitutes are taken off (lessons return to the group teachers)."""
    handover(w, "owner", "D", [])
    client(w, "admin").put(f"/api/v1/auth/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Болезнь"})
    impact = client(w, "owner").get(M.format(mid=mid(w, "D")) + "/removal-impact").json()
    assert impact["futureSubstitutions"] == {"count": 1} and impact["canRemove"] is True
    r = client(w, "owner").delete(M.format(mid=mid(w, "D")))
    assert r.status_code == 200 and r.json()["releasedSubstitutions"] == 1
    occ = client(w, "admin").get("/api/v1/auth/org/schedule/occurrences", params={"date_from": "2026-10-26", "date_to": "2026-10-26"}).json()
    assert [o["teacherId"] for o in occ if o["id"] == "lesson-l1"] == [str(w.A.id)]


def test_inv_o2_2_a_removed_user_passes_no_request(w):
    """[INV-O2-2] After the removal the live check of the token refuses (account disabled), the membership is revoked."""
    client(w, "owner").delete(M.format(mid=mid(w, "D")))
    payload = {"sub": str(w.D.id), "role": "teacher", "org_id": str(w.org.id), "org_role": "teacher", "type": "access"}
    with pytest.raises(HTTPException) as err:
        get_current_active_user_payload(payload=payload, db=w.db)
    assert err.value.status_code in (401, 403)


def test_members_list_by_status(w):
    """[O2.R3] GET /org/members?status=removed lists the former staff with removed_at; active hides them; all shows both."""
    client(w, "owner").delete(M.format(mid=mid(w, "D")))
    active = client(w, "owner").get("/api/v1/auth/org/members").json()
    removed = client(w, "owner").get("/api/v1/auth/org/members", params={"status": "removed"}).json()
    everybody = client(w, "owner").get("/api/v1/auth/org/members", params={"status": "all"}).json()
    assert str(w.D.id) not in [m["id"] for m in active] and [m["id"] for m in removed] == [str(w.D.id)]
    assert removed[0]["is_active"] is False and removed[0]["removed_at"] == "2026-10-21T09:00:00" and len(everybody) == len(active) + 1


def test_inv_o2_1_the_legacy_report_lists_groups_with_a_removed_teacher(w):
    """[INV-O2-1] Nothing in the new operations leaves an active group with a removed teacher; the script lists such legacy rows."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("report_inactive", Path(__file__).resolve().parents[1] / "scripts" / "report_inactive_group_teachers.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    mine = lambda: [v for v in module.find_violations(w.db) if v["org_id"] == str(w.org.id)]  # noqa: E731
    assert mine() == []
    client(w, "owner").delete(M.format(mid=mid(w, "A")))                      # refused: A has groups
    assert mine() == []
    w.member("A").is_active = False                                           # the OLD delete: the member goes, the groups stay
    w.db.commit()
    assert sorted(v["group"] for v in mine()) == ["7-Б", "9-А"] and mine()[0]["member"] == "removed"
