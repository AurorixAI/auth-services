"""R3 of LOGIC-B2 (lead, 22.10) on the integrated branch: M1 (production sessions do not autoflush), m1 .. m8, the colleagues list and the journal outcome.

Every test runs on the session of ``pg_world`` - ``autoflush=False`` like ``src/core/database.py`` - so what the production code does not flush, the test does not see either.
"""
import threading
import time
from datetime import datetime

import pytest
from sqlalchemy import text

from src.models.auth import GroupAudit, GroupScheduleOneOffLesson, LessonSubstitutionRequest, LessonTeacherAssignment

from tests.b2_helpers import (  # noqa: F401  (fixtures)
    API, build_d2, client, clock, decide, events_of, history_row, needs_pg, new_group, new_slot, new_student, pg_session, req, requests_of, row_of,
)
from tests.test_ops_b2_decide_pg import Sync, assignments, one, sub_body  # noqa: F401

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"
HANDOVER = None


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    assert pg_session.autoflush is False
    return build_d2(pg_session)


@pytest.fixture
def sync(monkeypatch):
    return Sync(monkeypatch)


# ------------------------------------------------------------------------------------------------------------------------------ M1
def test_m1_removing_the_substitute_reopens_the_request_even_when_he_has_a_pending_request_of_his_own(w, clock):
    """D substitutes A on 02.11 and (leading the lesson now) asks for a replacement himself; D is removed: his request is closed ``TEACHER_REMOVED`` and A's is reopened -
    with ``autoflush=False`` the closing must be flushed before the "pending already on this lesson?" query."""
    item = one(w, "A", "L1", "2026-11-02", reason="sick")
    assert decide(w, "admin", item["id"], **sub_body(w, "D")).status_code == 200
    own = req(w, "D", [(w.G1, "lesson-l1", "2026-11-02")], reason="personal")
    assert own.status_code == 201, own.text
    clock.set("2026-10-25 10:00")
    out = client(w, "owner").delete(f"{API}/org/members/{w.ids('member_D')}")
    assert out.status_code == 200 and out.json()["releasedSubstitutions"] == 1, out.text
    by_status = sorted((r.status, r.origin, r.closed_reason) for r in requests_of(w))
    assert by_status == [("closed", "teacher", "TEACHER_REMOVED"), ("pending", "system", None), ("substituted", "teacher", None)]


# ------------------------------------------------------------------------------------------------------------------------------ m1
def test_m1_an_assignment_waits_for_the_row_lock_of_its_target(w, clock):
    """``PUT lesson-teacher`` takes the target's user row BEFORE the busy check: while somebody else holds it (a removal of the teacher), the assignment waits."""
    engine = w.db.get_bind()
    holder = engine.connect()
    trans = holder.begin()
    holder.execute(text("SELECT id FROM users WHERE id = :i FOR UPDATE"), {"i": w.D.id})
    done = {}

    def call():
        done["start"] = time.monotonic()
        done["r"] = client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher",
                                           json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Болезнь"})
        done["end"] = time.monotonic()

    thread = threading.Thread(target=call)
    thread.start()
    time.sleep(1.0)
    assert "r" not in done                                   # still waiting for the lock
    trans.commit()
    holder.close()
    thread.join(timeout=20)
    assert done["r"].status_code == 200 and done["end"] - done["start"] >= 0.9


# ------------------------------------------------------------------------------------------------------------------------------ m2
def test_m2_a_lesson_half_a_minute_ahead_is_urgent_not_overdue(w, clock):
    """Minutes are rounded UP: 30 s before the start = 1 minute (urgent), exactly at the start = 0 (overdue); the item and the summary agree."""
    item = one(w, "A", "L1", "2026-10-26")
    clock.set("2026-10-26 09:59:30")
    listed = client(w, "admin").get(SUB, params={"status": "pending"}).json()
    got = listed["items"][0]
    assert (got["minutesToStart"], got["urgent"], got["overdue"]) == (1, True, False)
    assert (listed["urgentTotal"], client(w, "admin").get(f"{SUB}/summary").json()["overdueTotal"]) == (1, 0)
    clock.set("2026-10-26 10:00:00")
    got = client(w, "admin").get(SUB, params={"status": "pending"}).json()["items"]
    assert not got or (got[0]["minutesToStart"], got[0]["urgent"], got[0]["overdue"]) == (0, False, True)
    assert client(w, "admin").get(f"{SUB}/summary").json()["overdueTotal"] == 1


# ------------------------------------------------------------------------------------------------------------------------------ m3
def test_m3_a_decision_judges_the_current_time_of_a_lesson_that_was_moved_in_the_schedule(w, clock, sync):
    """The request was made for Wed 16:00; the schedule moved the lesson to 13:00 (already over at 14:00): ``reschedule`` is refused ``LESSON_ALREADY_STARTED``."""
    item = one(w, "A", "L2", "2026-10-21")
    slots = [dict(s) for s in w.G1.schedule]
    for s in slots:
        if s["id"] == "lesson-l2":
            s["startTime"], s["endTime"] = "12:00", "13:00"
    w.G1.schedule = slots
    w.db.commit()
    out = decide(w, "admin", item["id"], decision="reschedule", newDate="2026-10-22", startTime="12:00", endTime="13:00", reason="Перенос")
    assert out.status_code == 409 and out.json()["detail"]["code"] == "LESSON_ALREADY_STARTED", out.text
    assert requests_of(w)[0].status == "pending"


# ------------------------------------------------------------------------------------------------------------------------------ m4
def test_m4_cancelling_a_lesson_releases_its_substitute_and_hides_the_replacement(w, clock, sync):
    """D substitutes A on 02.11, then (leading) asks for a replacement; the admin cancels the lesson: D's assignment is released and «Замена» is gone from his list."""
    item = one(w, "A", "L1", "2026-11-02")
    assert decide(w, "admin", item["id"], **sub_body(w, "D")).status_code == 200
    mine = client(w, "D").get(f"{SUB}/mine").json()
    assert [a["lessonDate"] for a in mine["assignedToMe"]] == ["2026-11-02"]
    own = req(w, "D", [(w.G1, "lesson-l1", "2026-11-02")], reason="personal").json()["created"][0]
    assert decide(w, "admin", own["id"], decision="cancel", reason="Урока не будет").status_code == 200
    assert assignments(w, w.G1, "lesson-l1", "2026-11-02") == []                       # released
    assert client(w, "D").get(f"{SUB}/mine").json()["assignedToMe"] == []
    w.db.expire_all()
    cleared = [a for a in w.db.query(GroupAudit).filter_by(group_id=w.G1.id, action="lesson_teacher_cleared")]
    assert len(cleared) == 1 and "перенесён" in cleared[0].snapshot["reason"]


def test_m4_an_assignment_on_a_cancelled_row_is_not_shown_and_not_reopened(w, clock, sync):
    """A lesson cancelled by another way (a row) after the substitution: «Замена» is not shown, and removing the substitute does not reopen a request for it."""
    item = one(w, "A", "L1", "2026-11-02")
    assert decide(w, "admin", item["id"], **sub_body(w, "D")).status_code == 200
    history_row(w.db, w.org, w.G1, "lesson-l1", "2026-11-02", None, [w.s1], status="cancelled", records={})
    w.db.commit()
    assert client(w, "D").get(f"{SUB}/mine").json()["assignedToMe"] == []
    clock.set("2026-10-25 10:00")
    assert client(w, "owner").delete(f"{API}/org/members/{w.ids('member_D')}").status_code == 200
    assert [r.status for r in requests_of(w)] == ["substituted"]


# ------------------------------------------------------------------------------------------------------------------------------ m5
def test_m5_a_substitutes_journal_names_former_students_by_id_name_and_display_id_only(w, clock):
    """The former students of the substitute's lesson come without periods, dates of leaving and transfers."""
    from src.auth.membership_periods import close_period
    from src.models.auth import StudentGroup

    gone = new_student(w.db, w.org, w.ids, "gone", "Ушедший", "Ученик", w.G1, display_id="AL09-077")
    gone_membership = w.db.query(StudentGroup).filter_by(student_id=gone.id).one()
    close_period(gone_membership, datetime(2026, 10, 20, 8, 0))
    gone_membership.is_active = False
    w.db.commit()                                                      # (row_of expires the session: pending changes would be lost - autoflush is off)
    row = row_of(w, w.G1, "lesson-l1", "2026-10-19")                                                                      # the lesson of 19.10 was D's, with the student who left later
    row.teacher_id = w.D.id
    row.records = {**dict(row.records), str(gone.id): "present"}
    w.db.add(LessonTeacherAssignment(org_id=w.org.id, group_id=w.G1.id, lesson_id="lesson-l1", lesson_date="2026-10-19", teacher_id=w.D.id, reason="Болезнь",
                                     set_by=w.admin.id, created_at=datetime(2026, 10, 15, 8, 0)))
    row.roster_snapshot = list(row.roster_snapshot) + [{"student_id": str(gone.id), "first_name": gone.first_name, "last_name": gone.last_name,
                                                        "full_name": "Ушедший Ученик", "class_level": 9, "display_id": gone.display_id}]
    w.db.commit()
    journal = client(w, "D").get(f"{API}/org/groups/{w.G1.id}/attendance", params={"month": "2026-10"})
    assert journal.status_code == 200, journal.text
    former = journal.json()["historicalStudents"]
    assert [h["id"] for h in former] == [str(gone.id)]
    assert set(former[0]) == {"id", "first_name", "last_name", "full_name", "display_id"}


# ------------------------------------------------------------------------------------------------------------------------------ m6
def test_m6_a_read_commits_nothing_and_revalidates_only_what_it_returns(w, clock):
    """``GET /org/substitutions``: no commit when nothing is stale; a stale request outside the page is not touched."""
    a = one(w, "A", "L1", "2026-10-26")
    b = one(w, "A", "L2", "2026-10-28")
    commits = []
    from sqlalchemy import event

    event.listen(w.db, "after_commit", lambda session: commits.append(1))
    got = client(w, "admin").get(SUB, params={"status": "pending"})
    assert got.status_code == 200 and len(got.json()["items"]) == 2
    assert commits == []                                                           # a GET with nothing to fix does not commit
    slots = [s for s in w.G1.schedule if s["id"] != "lesson-l2"]                  # the lesson of the second request is gone: it is stale
    w.G1.schedule = slots
    w.db.commit()
    page = client(w, "admin").get(SUB, params={"status": "pending", "limit": 1}).json()
    assert [i["id"] for i in page["items"]] == [a["id"]]
    assert sorted(r.status for r in requests_of(w)) == ["pending", "pending"]      # the stale one was not revalidated: it is not on the page
    full = client(w, "admin").get(SUB, params={"status": "pending"}).json()
    assert [i["id"] for i in full["items"]] == [a["id"]] and sorted(r.status for r in requests_of(w)) == ["closed", "pending"]


def test_m6_the_rate_of_every_candidate_is_found_with_one_query(w, clock):
    """Owner's candidates list: ``rateMissing`` of the staff without a rate for the type of lessons (batched)."""
    item = one(w, "A", "L1", "2026-10-26")
    cands = client(w, "owner").get(f"{SUB}/{item['id']}/candidates").json()["candidates"]
    by_name = {c["name"]: c["rateMissing"] for c in cands}
    assert by_name["Рахимов Т."] is True and by_name["Сидоров С."] is False
    assert all(c["rateMissing"] is None for c in client(w, "admin").get(f"{SUB}/{item['id']}/candidates").json()["candidates"])


# ------------------------------------------------------------------------------------------------------------------------------ m7
def test_m7_a_bulk_decision_stops_after_the_first_exam_service_failure(w, clock, sync):
    ids = [one(w, "A", "L1", d)["id"] for d in ("2026-10-26", "2026-11-02", "2026-11-09")]
    sync.mode = "down"
    out = client(w, "admin").post(f"{SUB}/decide-bulk", json={"ids": ids, "decision": "cancel", "reason": "Урока не будет"})
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["updated"] == 0 and [s["code"] for s in body["skipped"]] == ["EXAM_SERVICE_UNAVAILABLE"] * 3
    assert [c[0] for c in sync.calls] == ["cancellation-check"]                    # exam-service was asked once, not three times
    assert sorted(r.status for r in requests_of(w)) == ["pending"] * 3


# ------------------------------------------------------------------------------------------------------------------------------ m8
def test_m8_a_reschedule_with_an_invalid_slot_writes_nothing(w, clock, sync):
    """The one-off lesson goes through ``_validate_schedule_payload``: a legacy format of the slot is refused (422), no cancelled row, no one-off lesson."""
    slots = [dict(s) for s in w.G1.schedule]
    for s in slots:
        if s["id"] == "lesson-l2":
            s["format"] = "hybrid"
    w.G1.schedule = slots
    w.db.commit()
    item = one(w, "A", "L2", "2026-10-28")
    out = decide(w, "admin", item["id"], decision="reschedule", newDate="2026-10-29", startTime="16:00", endTime="17:30", reason="Перенос")
    assert out.status_code == 422 and out.json()["detail"]["code"] == "SCHEDULE_PAYLOAD_INVALID", out.text
    assert row_of(w, w.G1, "lesson-l2", "2026-10-28") is None
    assert w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=w.G1.id).count() == 0


def test_m8_the_reason_of_an_assignment_is_the_admins_note_never_the_teachers_comment(w, clock):
    item = one(w, "A", "L1", "2026-10-26", reason="other", comment="Секретный комментарий учителя")
    assert decide(w, "admin", item["id"], **sub_body(w, "D")).status_code == 200
    (asg,) = assignments(w)
    assert "Секретный" not in asg.reason and asg.reason
    audit = w.db.query(GroupAudit).filter_by(group_id=w.G1.id, action="lesson_teacher_set").one()
    assert "Секретный" not in str(audit.snapshot)
    noted = one(w, "A", "L2", "2026-10-28")
    assert decide(w, "admin", noted["id"], decision="substitute", teacherId=str(w.D.id), note="Договорились по телефону").status_code == 200
    assert assignments(w, w.G1, "lesson-l2", "2026-10-28")[0].reason == "Договорились по телефону"


def test_m8_the_archive_counts_the_requests_closed_by_the_cancelled_missed_lessons(w, clock):
    """A request for a lesson that passed unstarted: the archive cancels the missed lesson (that closes the request) and still reports it in ``closedSubstitutionRequests``."""
    from src.auth.membership_periods import close_period
    from src.models.auth import StudentGroup

    group = new_group(w.db, w.org, w.ids, "ARM", "Архив-М", w.A, [new_slot("lesson-m", 3, "16:00", "17:00", "2026-10-14")])
    student = new_student(w.db, w.org, w.ids, "arm", "Ушедший", "Позже", group)
    history_row(w.db, w.org, group, "lesson-m", "2026-10-14", w.A, [student], start="16:00", end="17:00")
    w.db.commit()
    request = req(w, "A", [(group, "lesson-m", "2026-10-21")]).json()["created"][0]
    clock.set("2026-10-21 17:30")
    close_period(w.db.query(StudentGroup).filter_by(student_id=student.id).one(), datetime(2026, 10, 21, 12, 0))      # left at 17:00 local, after the lesson
    w.db.commit()
    out = client(w, "admin").post(f"{API}/org/groups/{group.id}/archive", json={"missedLessonsAction": "cancel", "reason": "Закрыта"})
    assert out.status_code == 200, out.text
    assert out.json()["cancelledMissedLessons"] >= 1 and out.json()["closedSubstitutionRequests"] == 1
    w.db.expire_all()
    assert w.db.get(LessonSubstitutionRequest, request["id"]).status == "closed"


# ------------------------------------------------------------------------------------------------------------------------------ colleagues
def test_colleagues_list_for_a_teacher_has_no_contacts_and_not_the_caller(w, clock):
    """«Предложить коллегу»: the active staff who can teach, except the caller; id, name, role only; teachers allowed; a removed member is not listed."""
    got = client(w, "A").get(f"{SUB}/colleagues")
    assert got.status_code == 200, got.text
    items = got.json()
    assert set(items[0]) == {"id", "name", "role"}
    assert str(w.A.id) not in {i["id"] for i in items}
    assert {i["id"] for i in items} == {str(getattr(w, k).id) for k in ("owner", "admin", "B", "C", "D")}
    assert {i["role"] for i in items} == {"owner", "admin", "teacher"}
    assert "@" not in str(items) and "+998" not in str(items)
    assert client(w, "admin").get(f"{SUB}/colleagues").status_code == 200
    assert client(w, "owner").delete(f"{API}/org/members/{w.ids('member_D')}").status_code == 200
    assert str(w.D.id) not in {i["id"] for i in client(w, "A").get(f"{SUB}/colleagues").json()}


# ------------------------------------------------------------------------------------------------------------------------------ journal outcome
def test_journal_lessons_carry_the_outcome_of_a_cancelled_or_moved_lesson(w, clock, sync):
    cancel = one(w, "A", "L1", "2026-10-26")
    move = one(w, "A", "L2", "2026-10-28")
    assert decide(w, "admin", cancel["id"], decision="cancel", reason="Урока не будет").status_code == 200
    assert decide(w, "admin", move["id"], decision="reschedule", newDate="2026-10-29", startTime="16:00", endTime="17:30", reason="Перенос").status_code == 200
    journal = client(w, "A").get(f"{API}/org/groups/{w.G1.id}/attendance", params={"month": "2026-10"}).json()
    by_key = {l["instanceKey"]: l for l in journal["lessons"]}
    assert by_key["2026-10-26_lesson-l1"]["substitutionOutcome"] == {"kind": "cancelled", "movedTo": None}
    moved = by_key["2026-10-28_lesson-l2"]["substitutionOutcome"]
    assert moved["kind"] == "rescheduled" and moved["movedTo"]["date"] == "2026-10-29" and moved["movedTo"]["startTime"] == "16:00"
    assert "reason" not in str(moved)
    assert "substitutionOutcome" not in by_key["2026-10-19_lesson-l1"]
