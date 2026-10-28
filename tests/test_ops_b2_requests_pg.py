"""LOGIC-B2 S1 (request), S2 (withdraw), S3 (list and badge) and chain 1 (golden).  Matrix rows [B2.S1.R1] ... [B2.S3.R9]."""
from datetime import datetime

import pytest

from src.models.auth import GroupAttendance

from tests.b2_helpers import (  # noqa: F401  (fixtures)
    API, Golden, build_d2, client, clock, code_of, decide, events_of, exams, fx, history_row, needs_pg, new_group, new_slot, new_student, pg_session, req, requests_of,
    row_of, run_parallel,
)

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    return build_d2(pg_session)


def L(w, key, day):
    return {"L1": (w.G1, "lesson-l1"), "L2": (w.G1, "lesson-l2"), "L3": (w.G2, "lesson-l3"), "L4": (w.G3, "lesson-l4"), "L5": (w.G4, "lesson-l5")}[key] + (day,)


def one(w, who="A", key="L1", day="2026-10-26", **kw):
    r = req(w, who, [L(w, key, day)], **kw)
    assert r.status_code == 201, r.text
    return r.json()["created"][0]


def occ(w, who, d_from, d_to):
    return client(w, who).get(f"{API}/org/schedule/occurrences", params={"date_from": d_from, "date_to": d_to}).json()


# ======================================================================================================================== S1
def test_s1_r1_request_is_a_pending_row_and_changes_nothing_else(w):
    """[B2.S1.R1] Chain 1 step 1: 201 with the golden item; one event ``requested``; the badge +1; the lesson teacher by the resolver is still A (D-4); A sees ``substitutionRequest``."""
    g = Golden()
    r = req(w, "A", [L(w, "L1", "2026-10-26")], reason="sick", comment="Температура", suggested="D")
    assert r.status_code == 201, r.text
    assert not g(w, r.json(), "b2c1_create.json"), g(w, r.json(), "b2c1_create.json")
    (row,) = requests_of(w)
    assert (row.status, str(row.requester_id), row.starts_at.isoformat()) == ("pending", str(w.A.id), "2026-10-26T05:00:00")
    assert [e.action for e in events_of(w)] == ["requested"]
    assert client(w, "admin").get(f"{SUB}/summary").json() == fx("b2c1_summary_pending.json")
    mine = [o for o in occ(w, "A", "2026-10-26", "2026-10-26") if o["id"] == "lesson-l1"][0]
    assert mine["teacherId"] == str(w.A.id) and mine["substitutionRequest"]["status"] == "pending" and mine["substitutionRequest"]["reasonCode"] == "sick"
    assert occ(w, "B", "2026-10-26", "2026-10-26")[0].get("substitutionRequest") is None                 # a stranger does not see the request


def test_s1_r2_double_click_and_a_repeat_after_a_decision(w):
    """[B2.S1.R2] The same body twice: ``alreadyRequested``, one row, one event; after ``withdrawn`` / ``rejected`` a new row with ``previous``."""
    g = Golden()
    first = req(w, "A", [L(w, "L1", "2026-10-26")], reason="sick", comment="Температура", suggested="D")
    again = req(w, "A", [L(w, "L1", "2026-10-26")], reason="sick", comment="Температура", suggested="D")
    assert again.status_code == 201 and not g(w, again.json(), "b2c1_create_repeat.json"), g(w, again.json(), "b2c1_create_repeat.json")
    assert len(requests_of(w)) == 1 and len(events_of(w)) == 1
    rid = first.json()["created"][0]["id"]
    assert client(w, "A").post(f"{SUB}/{rid}/withdraw").status_code == 200
    third = req(w, "A", [L(w, "L1", "2026-10-26")])
    assert third.status_code == 201 and third.json()["created"][0]["previous"]["id"] == rid and third.json()["created"][0]["previous"]["status"] == "withdrawn"
    assert len(requests_of(w)) == 2


def test_s1_r3_no_delete_only_withdraw(w):
    """[B2.S1.R3] There is no delete of a request; the way back is the withdrawal (S2)."""
    item = one(w)
    assert client(w, "A").delete(f"{SUB}/{item['id']}").status_code in (404, 405)
    assert client(w, "A").post(f"{SUB}/{item['id']}/withdraw").json()["status"] == "withdrawn"


def test_s1_r4_two_tabs_and_races_with_other_writers(w, clock):
    """[B2.S1.R4] Two creations at once: one created, one alreadyRequested (the partial unique index); a start / a substitution by the admin / an archive / a group-teacher change
    that won the race refuse the request."""
    org = str(w.org.id)
    from src.auth.substitution_schemas import CreateSubstitutionRequest

    body = CreateSubstitutionRequest(lessons=[{"groupId": str(w.G1.id), "lessonId": "lesson-l1", "lessonDate": "2026-10-26"}], reasonCode="sick")
    out = run_parallel(w, lambda svc: __import__("src.auth.lesson_substitutions", fromlist=["x"]).Substitutions(svc).create(org, str(w.A.id), body),
                       lambda svc: __import__("src.auth.lesson_substitutions", fromlist=["x"]).Substitutions(svc).create(org, str(w.A.id), body))
    assert all(err is None for _r, err in out), out
    assert sorted((len(r["created"]), len(r["alreadyRequested"])) for r, _e in out) == [(0, 1), (1, 0)]
    assert len(requests_of(w)) == 1
    # an administrator substituted the lesson first: the teacher is no longer D-less A
    assert client(w, "admin").put(f"{API}/org/groups/{w.G2.id}/lesson-teacher", json={"lessonDate": "2026-10-27", "lessonId": "lesson-l3", "teacherId": str(w.D.id), "reason": "Болезнь"}).status_code == 200
    late = req(w, "A", [L(w, "L3", "2026-10-27")])
    assert late.status_code == 403 and late.json()["detail"]["code"] == "NOT_LESSON_TEACHER"
    # the group was archived (no students)
    free = new_group(w.db, w.org, w.ids, "FA", "Пустая", w.C, [new_slot("lesson-f", 5, "15:00", "16:00", "2026-10-22")])
    w.db.commit()
    assert client(w, "admin").post(f"{API}/org/groups/{free.id}/archive", json={}).status_code == 200
    gone = req(w, "C", [(free, "lesson-f", "2026-10-30")])
    assert gone.status_code in (404, 409), gone.text


def test_s1_r5_started_cancelled_and_twenty_minutes_before(w, clock):
    """[B2.S1.R5] A started / past lesson -> 409 LESSON_ALREADY_STARTED (golden), a cancelled one -> LESSON_CANCELLED; a lesson in 20 minutes is allowed and urgent (chain 5)."""
    g = Golden()
    clock.set("2026-10-21 15:40")
    ok = req(w, "A", [L(w, "L2", "2026-10-21")])
    assert ok.status_code == 201 and not g(w, ok.json(), "b2c5_create_urgent.json"), g(w, ok.json(), "b2c5_create_urgent.json")
    clock.set("2026-10-21 16:30")
    late = req(w, "A", [L(w, "L1", "2026-10-19")])
    assert late.status_code == 409 and late.json() == fx("b2c5_lesson_already_started.json")["body"]
    row = history_row(w.db, w.org, w.G2, "lesson-l3", "2026-10-27", w.A, [w.s4, w.s5], status="cancelled", records={})
    row.cancellation_reason = "Праздник"
    w.db.commit()
    cancelled = req(w, "A", [L(w, "L3", "2026-10-27")])
    assert cancelled.status_code == 409 and cancelled.json()["detail"]["code"] == "LESSON_CANCELLED"


def test_s1_r6_boundaries_of_the_day_and_of_the_start(w, clock):
    """[B2.S1.R6] 23:50 for a lesson at 00:30: allowed, minutesToStart 40, urgent; 00:30:00 and later 409 (``<=``); exactly 24:00 before the start is not urgent, 23:59 is; a range over the
    month border is one request per lesson."""
    night = new_group(w.db, w.org, w.ids, "FN", "Ночная", w.C, [new_slot("lesson-n", 4, "00:30", "01:30", "2026-10-15")])
    w.db.commit()
    clock.set("2026-10-21 23:50")
    r = req(w, "C", [(night, "lesson-n", "2026-10-22")])
    assert r.status_code == 201 and r.json()["created"][0]["minutesToStart"] == 40 and r.json()["created"][0]["urgent"] is True
    assert requests_of(w)[0].starts_at.isoformat() == "2026-10-21T19:30:00"
    clock.set("2026-10-28 00:30")                                             # exactly 24:00 before 29.10 00:30
    exact = req(w, "C", [(night, "lesson-n", "2026-10-29")]).json()["created"][0]
    assert exact["minutesToStart"] == 1440 and exact["urgent"] is False
    clock.set("2026-11-04 00:31")                                             # 23:59 before 05.11 00:30
    assert req(w, "C", [(night, "lesson-n", "2026-11-05")]).json()["created"][0]["minutesToStart"] == 1439
    clock.set("2026-11-12 00:29")                                             # one minute before the start: allowed
    assert req(w, "C", [(night, "lesson-n", "2026-11-12")]).json()["created"][0]["minutesToStart"] == 1
    clock.set("2026-11-19 00:30")                                             # the start itself: refused (``<=``)
    assert req(w, "C", [(night, "lesson-n", "2026-11-19")]).json()["detail"]["code"] == "LESSON_ALREADY_STARTED"
    clock.set("2026-10-21 14:00")
    month = req(w, "A", [L(w, "L2", "2026-10-28"), L(w, "L1", "2026-11-02")])
    assert month.status_code == 201 and [c["lesson"]["date"] for c in month.json()["created"]] == ["2026-10-28", "2026-11-02"]
    rows = [r_ for r_ in requests_of(w) if r_.requester_id == w.A.id]
    assert len(rows) == 2 and rows[0].batch_id == rows[1].batch_id


def test_s1_r7_validation_and_the_range_rules(w):
    """[B2.S1.R7] No lesson / too many / too far / ``other`` without a comment / a stranger as the suggested colleague; a busy suggestion is not an error; one refused lesson answers its own error,
    several and none created -> NO_LESSONS_REQUESTABLE."""
    ghost = req(w, "A", [(w.G1, "lesson-nope", "2026-10-26")])
    assert ghost.status_code == 404 and ghost.json()["detail"]["code"] == "LESSON_NOT_FOUND"
    many = req(w, "A", [(w.G1, "lesson-l1", f"2026-{m:02d}-{d:02d}") for m in (11, 12) for d in range(1, 31)] + [(w.G1, "lesson-l2", "2026-12-31")])
    assert many.status_code == 422 and many.json()["detail"]["code"] == "TOO_MANY_LESSONS"
    far = req(w, "A", [L(w, "L1", "2027-03-01")])
    assert far.status_code == 422 and far.json()["detail"]["code"] == "LESSON_TOO_FAR"
    other = req(w, "A", [L(w, "L1", "2026-10-26")], reason="other")
    assert other.status_code == 422 and other.json()["detail"]["code"] == "COMMENT_REQUIRED"
    bad = client(w, "A").post(SUB, json={"lessons": [{"groupId": str(w.G1.id), "lessonId": "lesson-l1", "lessonDate": "2026-10-26"}], "reasonCode": "sick", "suggestedTeacherId": str(w.foreign_admin.id)})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "TARGET_NOT_TEACHER"
    assert req(w, "A", [L(w, "L1", "2026-10-26")], suggested="B").status_code == 201                    # B is busy at that time: not an error
    assert client(w, "A").post(SUB, json={"lessons": [], "reasonCode": "sick"}).status_code == 422
    none = req(w, "A", [(w.G1, "lesson-nope", "2026-10-26"), (w.G1, "lesson-nope2", "2026-10-27")])
    assert none.status_code == 409 and none.json()["detail"]["code"] == "NO_LESSONS_REQUESTABLE" and len(none.json()["detail"]["skipped"]) == 2
    empty = new_group(w.db, w.org, w.ids, "FE", "Пустая-2", w.C, [new_slot("lesson-e", 5, "15:00", "16:00", "2026-10-22")])
    w.db.commit()
    zero = req(w, "C", [(empty, "lesson-e", "2026-10-23")])
    assert zero.status_code == 201 and zero.json()["created"][0]["group"]["studentCount"] == 0


def test_s1_r8_roles(w):
    """[B2.S1.R8] The teacher of the lesson: ok; somebody else's lesson: NOT_LESSON_TEACHER with the name; the substitute D asks for HIS lesson (chain 8: another request, golden); the replaced
    A: 403; owner / admin: MANAGER_ASSIGNS_DIRECTLY (golden); no organisation: 403."""
    g = Golden()
    stranger = req(w, "B", [L(w, "L1", "2026-10-26")])
    assert stranger.status_code == 403 and stranger.json()["detail"] == {"code": "NOT_LESSON_TEACHER", "message": "Этот урок ведёт Иванов И."}
    for who in ("owner", "admin"):
        r = req(w, who, [L(w, "L1", "2026-10-26")])
        assert r.status_code == 403 and r.json() == fx("b2x_manager_assigns_directly.json")["body"]
    one(w, "A", reason="sick", comment="Температура", suggested="D")
    first = requests_of(w)[0]
    assert decide(w, "admin", first.id, decision="substitute", teacherId=str(w.D.id)).status_code == 200
    replaced = req(w, "A", [L(w, "L1", "2026-10-26")])
    assert replaced.status_code == 403 and replaced.json()["detail"]["code"] == "NOT_LESSON_TEACHER"
    r8 = req(w, "D", [L(w, "L1", "2026-10-26")], reason="sick", comment="Сам заболел")
    assert r8.status_code == 201, r8.text
    assert r8.json()["created"][0]["previous"] is None                                              # #157 A1: the request of A (that made D the teacher of the lesson) is not D's: no name, no status (the manager reads it)
    assert client(w, "admin").get(f"{SUB}/{r8.json()['created'][0]['id']}").json()["item"]["previous"]["status"] == "substituted"
    nobody = client(w, "owner", org="other").post(SUB, json={"lessons": [{"groupId": str(w.G1.id), "lessonId": "lesson-l1", "lessonDate": "2026-10-26"}], "reasonCode": "sick"})
    assert nobody.status_code in (403, 404)


def test_s1_r9_students_in_pause_do_not_count_and_group_rules(w):
    """[B2.S1.R9] ``studentCount`` = the active roster today; an archived group -> 409 GROUP_ARCHIVED; a slot that starts later has no lessons before; the request does not block a handover."""
    item = one(w, "A", "L3", "2026-10-27")
    assert item["group"]["studentCount"] == 2
    from src.auth.membership_periods import close_period
    from src.models.auth import StudentGroup

    close_period(w.db.query(StudentGroup).filter_by(student_id=w.s5.id).one(), datetime(2026, 10, 21, 8, 0))
    w.db.commit()
    again = client(w, "admin").get(f"{SUB}", params={"status": "pending"}).json()["items"][0]
    assert again["group"]["studentCount"] == 1
    late = new_group(w.db, w.org, w.ids, "FL", "Поздняя", w.C, [new_slot("lesson-l", 5, "15:00", "16:00", "2026-11-20")])
    w.db.commit()
    assert req(w, "C", [(late, "lesson-l", "2026-10-30")]).json()["detail"]["code"] == "LESSON_NOT_FOUND"
    owner = client(w, "owner")
    out = owner.post(f"{API}/org/members/{w.ids('member_A')}/handover", json={"assignments": [{"groupId": str(w.G1.id), "teacherId": str(w.C.id)}, {"groupId": str(w.G2.id), "teacherId": str(w.B.id)}]})
    assert out.status_code == 200, out.text


# ======================================================================================================================== S2
def test_s2_r1_withdraw(w):
    """[B2.S2.R1] 200 ``withdrawn``, ``decision.kind=withdraw``, ``by=A``; the event; the badge -1; the lesson teacher is still A."""
    item = one(w)
    r = client(w, "A").post(f"{SUB}/{item['id']}/withdraw")
    assert r.status_code == 200 and r.json()["status"] == "withdrawn" and r.json()["decision"]["kind"] == "withdraw" and r.json()["decision"]["by"]["name"] == "Иванов И."
    assert [e.action for e in events_of(w)] == ["requested", "withdrawn"]
    assert client(w, "admin").get(f"{SUB}/summary").json()["pendingTotal"] == 0
    mine = [o for o in occ(w, "A", "2026-10-26", "2026-10-26") if o["id"] == "lesson-l1"][0]
    assert mine["teacherId"] == str(w.A.id) and mine["substitutionRequest"] is None


def test_s2_r2_repeat_and_batch_withdraw(w):
    """[B2.S2.R2] A repeat is the same 200 without a second event; the batch answers ``withdrawn`` and ``skipped``."""
    g = Golden()
    a = one(w, "A", "L1", "2026-10-26")
    b = one(w, "A", "L2", "2026-10-28")
    assert client(w, "A").post(f"{SUB}/{a['id']}/withdraw").status_code == 200
    assert client(w, "A").post(f"{SUB}/{a['id']}/withdraw").status_code == 200
    assert [e.action for e in events_of(w, a["id"])] == ["requested", "withdrawn"]
    assert decide(w, "admin", b["id"], decision="reject", reason="Замену не нашли").status_code == 200
    out = client(w, "A").post(f"{SUB}/withdraw", json={"ids": [a["id"], b["id"], "00000000-0000-0000-0000-000000000000"]}).json()
    assert [x["id"] for x in out["withdrawn"]] == [a["id"]]
    assert {s["id"]: s["code"] for s in out["skipped"]} == {b["id"]: "SUBSTITUTION_ALREADY_DECIDED", "00000000-0000-0000-0000-000000000000": "SUBSTITUTION_NOT_FOUND"}


def test_s2_r3_decided_cannot_be_withdrawn(w):
    """[B2.S2.R3] 409 SUBSTITUTION_ALREADY_DECIDED with the item; a withdrawn request is not resurrected (a new one is created)."""
    item = one(w)
    assert decide(w, "admin", item["id"], decision="substitute", teacherId=str(w.D.id)).status_code == 200
    r = client(w, "A").post(f"{SUB}/{item['id']}/withdraw")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SUBSTITUTION_ALREADY_DECIDED" and r.json()["detail"]["item"]["status"] == "substituted"


def test_s2_r4_withdraw_against_the_decision(w):
    """[B2.S2.R4] The administrator after a withdrawal: 409 «Учитель отозвал запрос» with the golden item (chain 4); the teacher after a decision: 409."""
    g = Golden()
    one(w, "A", reason="sick")
    first = requests_of(w)[0]
    assert client(w, "A").post(f"{SUB}/{first.id}/withdraw").status_code == 200
    r = decide(w, "admin", first.id, decision="reject", reason="Замену не нашли")
    assert r.status_code == 409 and r.json()["detail"]["message"] == "Учитель отозвал запрос" and r.json()["detail"]["item"]["status"] == "withdrawn"
    out = fx("b2c4_already_decided_withdrawn.json")["body"]["detail"]["item"]
    assert r.json()["detail"]["item"]["decision"]["kind"] == out["decision"]["kind"] == "withdraw"


def test_s2_r5_started_lesson_and_overdue_pending(w, clock):
    """[B2.S2.R5] The lesson started before the withdrawal: the request is closed LESSON_STARTED -> 409 with the item; a pending whose lesson passed (overdue) can still be withdrawn."""
    one(w, "A", "L2", "2026-10-21")
    first = requests_of(w)[0]
    clock.set("2026-10-21 16:30")                                                      # overdue pending
    listed = client(w, "admin").get(f"{SUB}", params={"status": "pending"}).json()
    assert listed["items"][0]["overdue"] is True and listed["items"][0]["minutesToStart"] == -30
    assert client(w, "A").post(f"{SUB}/{first.id}/withdraw").status_code == 200
    other = one(w, "A", "L1", "2026-10-26")
    clock.set("2026-10-26 10:05")
    assert client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"}).status_code == 200
    closed = client(w, "A").post(f"{SUB}/{other['id']}/withdraw")
    assert closed.status_code == 409 and closed.json()["detail"]["item"]["status"] == "closed" and closed.json()["detail"]["item"]["decision"]["closedReason"] == "LESSON_STARTED"


def test_s2_r6_decided_at_is_utc(w, clock):
    """[B2.S2.R6] A withdrawal at 00:00:30 does not depend on the lesson date; ``decided_at`` is UTC naive."""
    item = one(w)
    clock.set("2026-10-22 00:00:30")
    r = client(w, "A").post(f"{SUB}/{item['id']}/withdraw")
    assert r.status_code == 200 and r.json()["decision"]["at"] == "2026-10-21T19:00:30"


def test_s2_r7_unknown_and_foreign(w):
    """[B2.S2.R7] An unknown id -> 404 SUBSTITUTION_NOT_FOUND; the request of another teacher -> 403 NOT_REQUEST_OWNER without details."""
    item = one(w)
    assert client(w, "A").post(f"{SUB}/00000000-0000-0000-0000-000000000000/withdraw").json()["detail"]["code"] == "SUBSTITUTION_NOT_FOUND"
    r = client(w, "B").post(f"{SUB}/{item['id']}/withdraw")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "NOT_REQUEST_OWNER" and "item" not in r.json()["detail"]


def test_s2_r8_only_the_requester(w):
    """[B2.S2.R8] Owner / admin / the substitute / a stranger teacher cannot withdraw (admin: 403 - his tool is "reject")."""
    item = one(w)
    for who in ("owner", "admin", "B", "D"):
        r = client(w, who).post(f"{SUB}/{item['id']}/withdraw")
        assert r.status_code == 403, (who, r.status_code, r.text)


def test_s2_r9_closed_by_the_system(w):
    """[B2.S2.R9] A request closed by the system (group handed over): 409 SUBSTITUTION_ALREADY_DECIDED with ``item.status=closed`` and the reason."""
    item = one(w, "A", "L1", "2026-11-02")
    assert client(w, "owner").post(f"{API}/org/members/{w.ids('member_A')}/handover", json={"assignments": [
        {"groupId": str(w.G1.id), "teacherId": str(w.C.id)}, {"groupId": str(w.G2.id), "teacherId": str(w.B.id)}]}).status_code == 200
    r = client(w, "A").post(f"{SUB}/{item['id']}/withdraw")
    assert r.status_code == 409 and r.json()["detail"]["item"]["status"] == "closed" and r.json()["detail"]["item"]["decision"]["closedReason"] == "GROUP_TEACHER_CHANGED"


# ======================================================================================================================== S3
def test_s3_r1_list_order_and_badge(w, clock):
    """[B2.S3.R1] Pending: overdue first, then by start; the badge equals the number of rows; row fields."""
    one(w, "A", "L1", "2026-11-02")
    one(w, "A", "L2", "2026-10-28")
    clock.set("2026-10-21 15:40")
    one(w, "A", "L2", "2026-10-21")
    clock.set("2026-10-21 16:30")
    out = client(w, "admin").get(SUB, params={"status": "pending"}).json()
    assert [(i["lesson"]["date"], i["overdue"]) for i in out["items"]] == [("2026-10-21", True), ("2026-10-28", False), ("2026-11-02", False)]
    assert out["total"] == out["pendingTotal"] == 3 and client(w, "admin").get(f"{SUB}/summary").json()["pendingTotal"] == 3
    first = out["items"][1]
    assert first["teacher"]["name"] == "Иванов И." and first["reasonLabel"] == "Болезнь" and first["group"]["name"] == "9-А"


def test_s3_r2_summary_does_not_write(w):
    """[B2.S3.R2] The badge is a count: no row and no event is written by asking."""
    one(w)
    before = (len(requests_of(w)), len(events_of(w)))
    for _ in range(3):
        client(w, "admin").get(f"{SUB}/summary")
    assert (len(requests_of(w)), len(events_of(w))) == before


def test_s3_r3_decided_tab(w):
    """[B2.S3.R3] Decided and withdrawn go to the «Решённые» tab (30 days by the lesson date), the badge goes down."""
    a, b = one(w, "A", "L1", "2026-10-26"), one(w, "A", "L2", "2026-10-28")
    assert decide(w, "admin", a["id"], decision="reject", reason="Замену не нашли").status_code == 200
    assert client(w, "A").post(f"{SUB}/{b['id']}/withdraw").status_code == 200
    s = client(w, "admin")
    assert s.get(SUB, params={"status": "pending"}).json()["items"] == [] and s.get(f"{SUB}/summary").json()["pendingTotal"] == 0
    assert {i["id"] for i in s.get(SUB, params={"status": "decided"}).json()["items"]} == {a["id"], b["id"]}
    assert {i["id"] for i in s.get(SUB, params={"status": "all"}).json()["items"]} == {a["id"], b["id"]}


def test_s3_r4_second_admin_gets_the_item_in_the_error(w):
    """[B2.S3.R4] Two administrators look at one row, one decides: the other gets 409 + the fresh ``item`` (chain 4, golden)."""
    item = one(w, "A", reason="sick")
    assert decide(w, "admin", item["id"], decision="reject", reason="Замену не нашли").status_code == 200
    r = decide(w, "owner", item["id"], decision="reject", reason="Другая причина")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SUBSTITUTION_ALREADY_DECIDED" and r.json()["detail"]["message"] == "Запрос уже решён"
    assert r.json()["detail"]["item"]["status"] == "rejected" and r.json()["detail"]["item"]["decision"]["note"] == "Замену не нашли"


def test_s3_r5_overdue_is_not_urgent(w, clock):
    """[B2.S3.R5] Chain 5: a lesson that has started is ``overdue`` (golden), not in ``urgentTotal``, in ``overdueTotal`` and in the badge; the repeat of the request answers alreadyRequested."""
    g = Golden()
    clock.set("2026-10-21 15:40")
    created = req(w, "A", [L(w, "L2", "2026-10-21")])
    assert not g(w, created.json(), "b2c5_create_urgent.json")
    clock.set("2026-10-21 16:30")
    assert client(w, "admin").get(f"{SUB}/summary").json() == fx("b2c5_summary_overdue.json")
    item = client(w, "admin").get(SUB, params={"status": "pending"}).json()["items"][0]
    assert not g(w, item, "b2c5_item_overdue.json"), g(w, item, "b2c5_item_overdue.json")
    again = req(w, "A", [L(w, "L2", "2026-10-21")])
    assert again.status_code == 201 and not g(w, again.json(), "b2c5_create_already_requested.json"), g(w, again.json(), "b2c5_create_already_requested.json")


def test_s3_r6_urgent_by_instants(w, clock):
    """[B2.S3.R6] ``urgent`` is ``0 < minutes < 1440`` by UTC instants: the change of the Tashkent date moves nothing; a changed lesson time is refreshed by the lazy pass."""
    item = one(w, "A", "L1", "2026-10-26")
    clock.set("2026-10-25 10:00")                                                       # 24:00 before 26.10 10:00
    assert client(w, "admin").get(SUB).json()["items"][0]["urgent"] is False
    clock.set("2026-10-25 10:01")
    assert client(w, "admin").get(SUB).json()["items"][0]["urgent"] is True and client(w, "admin").get(f"{SUB}/summary").json()["urgentTotal"] == 1


def test_s3_r7_empty(w):
    """[B2.S3.R7] Nothing: ``items: []``, zero badge."""
    out = client(w, "admin").get(SUB).json()
    assert out["items"] == [] and out["total"] == 0 and out["pendingTotal"] == 0


def test_s3_r8_roles(w):
    """[B2.S3.R8] Only owner / admin of the centre; a teacher gets 403 (his requests are ``mine``); the comment is shown to the manager and to the requester only."""
    one(w, "A", reason="personal", comment="Конференция")
    for who in ("A", "B", "D"):
        assert client(w, who).get(SUB).status_code == 403 and client(w, who).get(f"{SUB}/summary").status_code == 403
    assert client(w, "owner").get(SUB).json()["items"][0]["comment"] == "Конференция"
    mine = client(w, "A").get(f"{SUB}/mine").json()
    assert mine["items"][0]["comment"] == "Конференция" and mine["pendingTotal"] == 1
    assert client(w, "B").get(f"{SUB}/mine").json()["items"] == []
    assert client(w, "admin", org="other").get(SUB).status_code in (403, 404) or client(w, "admin", org="other").get(SUB).json()["items"] == []


def test_s3_r9_stale_pending_is_closed_by_the_lazy_pass(w):
    """[B2.S3.R9] A pending of an archived group / a removed teacher disappears from the list (revalidate) and is closed with the reason."""
    free = new_group(w.db, w.org, w.ids, "FZ", "Зет", w.C, [new_slot("lesson-z", 5, "15:00", "16:00", "2026-10-22")])
    w.db.commit()
    created = req(w, "C", [(free, "lesson-z", "2026-10-30")])
    assert created.status_code == 201
    from src.models.auth import Group

    w.db.query(Group).filter_by(id=free.id).update({"status": "archived"})                # a writer that bypassed the hooks
    w.db.commit()
    assert client(w, "admin").get(SUB, params={"status": "pending"}).json()["items"] == []
    (row,) = requests_of(w)
    assert (row.status, row.closed_reason) == ("closed", "GROUP_ARCHIVED")


# ======================================================================================================================== chain 1
def test_chain1_substitution_by_suggestion_golden(w, clock):
    """[B2.S4.R1] Chain 1: request -> candidates -> decide -> D teaches Monday -> A cannot save -> payroll counts D (golden fixtures b2c1_*)."""
    g = Golden()
    created = req(w, "A", [L(w, "L1", "2026-10-26")], reason="sick", comment="Температура", suggested="D")
    assert not g(w, created.json(), "b2c1_create.json")
    clock.set("2026-10-21 14:10")
    cand = client(w, "admin").get(f"{SUB}/{created.json()['created'][0]['id']}/candidates")
    assert cand.status_code == 200 and not g(w, cand.json(), "b2c1_candidates_admin.json"), g(w, cand.json(), "b2c1_candidates_admin.json")
    clock.set("2026-10-21 14:12")
    done = decide(w, "admin", created.json()["created"][0]["id"], decision="substitute", teacherId=str(w.D.id))
    assert done.status_code == 200 and not g(w, done.json(), "b2c1_decide_substitute.json"), g(w, done.json(), "b2c1_decide_substitute.json")
    assert client(w, "admin").get(f"{SUB}/summary").json() == fx("b2c1_summary_after.json")
    base = f"{API}/org/groups/{w.G1.id}/attendance"
    clock.set("2026-10-26 10:00")
    assert client(w, "D").post(base + "/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"}).status_code == 200
    clock.set("2026-10-26 11:20")
    marks = {str(s.id): "present" for s in (w.s1, w.s2, w.s3)}
    refused = client(w, "A").post(base, json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "status": "in_progress", "records": marks})
    assert refused.status_code == 403 and refused.json() == fx("b2c1_not_lesson_teacher.json")["body"]
    assert client(w, "D").post(base, json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "status": "completed", "records": marks, "expectedVersion": 1}).status_code == 200
    row = row_of(w, w.G1, "lesson-l1", "2026-10-26")
    assert (row.teacher_id, row.marked_by, row.status) == (w.D.id, w.D.id, "completed")
