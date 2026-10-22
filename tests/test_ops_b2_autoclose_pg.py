"""LOGIC-B2 auto-closing (D-9: the eager hooks, the ``before_flush`` listener on new journal rows, the lazy pass) and the races of the decision with the other writers."""
from datetime import date, datetime

import pytest

from src.models.auth import Group, GroupAttendance

from tests.b2_helpers import (  # noqa: F401  (fixtures)
    API, build_d2, client, clock, code_of, decide, events_of, exams, needs_pg, pg_session, req, requests_of, row_of, run_parallel,
)
from tests.test_ops_b2_decide_pg import L, one, sync, sub_body, assignments  # noqa: F401

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    return build_d2(pg_session)


def test_a_journal_row_written_by_any_path_closes_the_pending_request_at_once(w):
    """A row of the journal (started / cancelled) that appears through ANY writer closes the pending request in the same transaction (the ``before_flush`` listener), before any list is read."""
    a, b = one(w, "A", "L1", "2026-10-26"), one(w, "A", "L2", "2026-10-28")
    w.db.add(GroupAttendance(org_id=w.org.id, group_id=w.G1.id, teacher_id=w.A.id, lesson_date="2026-10-26", lesson_id="lesson-l1", status="in_progress", records={},
                             roster_snapshot=[], marked_by=w.A.id, started_at=datetime(2026, 10, 26, 5, 0), version=1))
    w.db.add(GroupAttendance(org_id=w.org.id, group_id=w.G1.id, teacher_id=w.A.id, lesson_date="2026-10-28", lesson_id="lesson-l2", status="cancelled", records={},
                             roster_snapshot=[], marked_by=w.A.id, cancellation_reason="Праздник", version=1))
    w.db.commit()
    rows = {str(r.id): (r.status, r.closed_reason, r.decided_via) for r in requests_of(w)}
    assert rows[a["id"]] == ("closed", "LESSON_STARTED", "system") and rows[b["id"]] == ("closed", "LESSON_CANCELLED", "system")
    assert [e.action for e in events_of(w)].count("closed") == 2


def test_the_lazy_pass_refreshes_a_changed_time_and_closes_a_removed_lesson(w):
    """The lesson time changed in the schedule: ``revalidate`` refreshes ``starts_at`` / the times; the slot was deleted: ``closed/LESSON_REMOVED``."""
    a, b = one(w, "A", "L1", "2026-10-26"), one(w, "A", "L2", "2026-10-28")
    group = w.db.get(Group, w.G1.id)
    slots = [dict(s) for s in group.schedule]
    for s in slots:
        if s["id"] == "lesson-l1":
            s["startTime"], s["endTime"] = "11:00", "12:30"
    slots = [s for s in slots if s["id"] != "lesson-l2"]
    group.schedule = slots
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(group, "schedule")
    w.db.commit()
    items = {i["id"]: i for i in client(w, "admin").get(SUB, params={"status": "all"}).json()["items"]}
    assert items[a["id"]]["status"] == "pending" and items[a["id"]]["lesson"]["startTime"] == "11:00" and items[a["id"]]["lesson"]["startsAtUtc"] == "2026-10-26T06:00:00"
    assert items[b["id"]]["status"] == "closed" and items[b["id"]]["decision"]["closedReason"] == "LESSON_REMOVED"


def test_decision_against_a_start_in_another_transaction(w, clock):
    """[B2.S4.R4] ``decide`` ‖ the start of the lesson: either the request is ``closed/LESSON_STARTED`` or the substitution wins and A's start is refused - never both."""
    from src.auth.lesson_substitutions import Substitutions
    from src.auth.schemas import StartAttendanceRequest
    from src.auth.substitution_schemas import DecideRequest

    item = one(w)
    clock.set("2026-10-26 10:00")
    org = str(w.org.id)
    run_parallel(
        w,
        lambda svc: Substitutions(svc).decide(org, str(w.admin.id), "admin", item["id"], DecideRequest(decision="substitute", teacherId=str(w.D.id))),
        lambda svc: svc.start_group_attendance(org, str(w.G1.id), StartAttendanceRequest(lessonDate=date(2026, 10, 26), lessonId="lesson-l1"), started_by=str(w.A.id), is_org_admin=False),
    )
    row = row_of(w, w.G1, "lesson-l1", "2026-10-26")
    (req_row,) = requests_of(w)
    if row is not None:                                      # the start won
        assert row.teacher_id == w.A.id and req_row.status in ("closed", "substituted")
        assert req_row.status == "closed", "A started a lesson that was substituted to D"
    else:
        assert req_row.status == "substituted"


def test_creation_against_a_handover(w):
    """[B2.S1.R4] A request ‖ a change of the group teacher: either the request is created and then closed ``GROUP_TEACHER_CHANGED``, or refused - never a pending request of a teacher who does not lead."""
    from src.auth.lesson_substitutions import Substitutions
    from src.auth.schemas import GroupUpdateRequest
    from src.auth.substitution_schemas import CreateSubstitutionRequest

    org = str(w.org.id)
    body = CreateSubstitutionRequest(lessons=[{"groupId": str(w.G1.id), "lessonId": "lesson-l1", "lessonDate": "2026-11-02"}], reasonCode="sick")
    run_parallel(
        w,
        lambda svc: Substitutions(svc).create(org, str(w.A.id), body),
        lambda svc: svc.update_group(org, str(w.G1.id), GroupUpdateRequest(teacher_id=str(w.C.id)), actor_id=str(w.admin.id), actor_role="admin"),
    )
    w.db.expire_all()
    for r in requests_of(w):
        assert r.status == "closed" and r.closed_reason == "GROUP_TEACHER_CHANGED"


def test_a_decision_leaves_nothing_behind_when_a_part_fails(w, sync):
    """[B2.S5.R1] The reschedule is atomic: a failing exam release after the cancelled row and the one-off lesson were flushed rolls everything back."""
    from src.auth.service import AuthService
    from src.models.auth import GroupScheduleOneOffLesson

    item = one(w, "A", "L2", "2026-10-28")
    calls = []

    def flaky(self, *, phase, **kw):
        calls.append(phase)
        if phase == "release":
            raise RuntimeError("down at release")

    AuthService._sync_exam_lesson_cancellation = flaky           # the ``sync`` fixture restores the original at its teardown
    r = decide(w, "admin", item["id"], decision="reschedule", newDate="2026-10-29", startTime="16:00", endTime="17:30", reason="Перенос")
    assert r.status_code == 503 and calls == ["cancellation-check", "release"]
    assert row_of(w, w.G1, "lesson-l2", "2026-10-28") is None and w.db.query(GroupScheduleOneOffLesson).filter_by(group_id=w.G1.id).count() == 0
    assert [x.status for x in requests_of(w)] == ["pending"] and [e.action for e in events_of(w)] == ["requested"]
