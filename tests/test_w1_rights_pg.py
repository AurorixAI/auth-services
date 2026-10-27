"""W1 S1 / S3 / S13 / S15: who may do what (PostgreSQL).

S1: an administrator's right comes from the role in THIS centre only - the owner of another centre, who is a teacher here, is a teacher here (schedule, attendance, one-off
lessons, series).  S3: a teacher does not cancel a lesson himself (403 ``TEACHER_CANNOT_CANCEL``); «Урока не было» of a PAST lesson that never started stays his.
S13: an owner / admin who was made a substitute reads his lessons in ``GET /org/substitutions/mine``.  S15: no role is ever defaulted to ``admin``.
Sources: schedule-series-edit-stop.md M52 / ДЫРА-4 / T12, lesson-move-cancel-dayoff.md Д2 / Д14 / №15-17, substitutions.md D2 / D9 / D15."""
from datetime import date
from pathlib import Path

import pytest

from src.models.auth import GroupAttendance, LessonSubstitutionRequest as Req
from tests.b2_helpers import API, build_d2, client, clock, decide, needs_pg, pg_session, req, requests_of, row_of  # noqa: F401  (fixtures)
from tests.pg_world import make_client

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")                        # Wednesday
    return build_d2(pg_session)


def owner_elsewhere(w, who="B"):
    """The teacher ``who`` of this centre whose GLOBAL role is owner (he owns another centre): the token says ``role: owner`` and ``org_role: teacher``."""
    return make_client(w, user=getattr(w, who), org="own", org_role="teacher", role="owner")


# ===================================================================================================== S1
def test_s1_an_owner_of_another_centre_is_a_teacher_here_schedule_series_and_one_off(w):
    c = owner_elsewhere(w, "B")                                                      # B teaches G3 only; G1 is not his
    g1 = str(w.G1.id)
    assert c.post(f"{API}/org/schedule/lessons", json={"groupId": g1, "dayOfWeek": 5, "startTime": "12:00", "endTime": "13:00", "validFrom": "2026-11-02"}).status_code == 403
    assert c.post(f"{API}/org/schedule/occurrences", json={"groupId": g1, "lessonDate": "2026-11-05", "startTime": "12:00", "endTime": "13:00"}).status_code == 403
    assert c.put(f"{API}/org/schedule/lessons/lesson-l1/recurrence", json={"effectiveFrom": "2026-11-09", "startTime": "12:00", "endTime": "13:30", "reason": "Перенос"}).status_code == 403
    assert c.get(f"{API}/org/schedule/lessons/lesson-l1/recurrence/end-impact", params={"effective_from": "2026-11-09"}).status_code == 403
    assert c.post(f"{API}/org/schedule/lessons/lesson-l1/recurrence/end", json={"effectiveFrom": "2026-11-09", "reason": "Курс закончен"}).status_code == 403
    # nothing was written: the schedule of G1 is as it was
    w.db.expire_all()
    assert len(w.db.get(type(w.G1), w.G1.id).schedule) == 2
    # the real administrator may (the control of the test)
    assert client(w, "admin").put(f"{API}/org/schedule/lessons/lesson-l1/recurrence",
                                  json={"effectiveFrom": "2026-11-09", "startTime": "12:00", "endTime": "13:30", "reason": "Перенос"}).status_code == 200


def test_s1_an_owner_of_another_centre_does_not_read_or_mark_the_groups_of_others(w):
    c = owner_elsewhere(w, "B")
    g1 = str(w.G1.id)
    assert c.get(f"{API}/org/groups/{g1}/attendance", params={"month": "2026-10"}).status_code == 403
    assert c.post(f"{API}/org/groups/{g1}/attendance/start", json={"lessonDate": "2026-10-21", "lessonId": "lesson-l2"}).status_code == 403
    assert c.post(f"{API}/org/groups/{g1}/attendance/reconcile", json={"lessonDate": "2026-10-19", "lessonId": "lesson-l1"}).status_code == 403
    open_actions = c.get(f"{API}/org/attendance/open-actions")
    assert open_actions.status_code == 200 and {x["groupName"] for x in open_actions.json()} <= {"8-В"}         # the lessons of HIS group only, never those of 9-А / 7-Б
    # not an administrator of the students either: the roster of a group he does not teach
    assert c.get(f"{API}/org/groups/{g1}").status_code in (403, 404)
    # and nothing of «admin only» opens: a manager's route answers 403 whatever the global role is
    assert c.get(f"{SUB}/summary").status_code == 403
    assert c.get(f"{API}/org/days-off").status_code == 403


def test_s1_a_manager_of_this_centre_keeps_every_right(w):
    c = client(w, "admin")
    assert c.get(f"{API}/org/groups/{w.G1.id}/attendance", params={"month": "2026-10"}).status_code == 200
    assert c.get(f"{SUB}/summary").status_code == 200
    assert client(w, "owner").get(f"{API}/org/days-off").status_code == 200


# ===================================================================================================== S15
def test_s15_no_role_is_defaulted_to_admin():
    root = Path(__file__).resolve().parents[1] / "src" / "auth"
    scope = ("router.py", "ops_router.py", "substitution_router.py", "days_off_router.py")            # the routes of the schedule, attendance, substitutions and days off
    offenders = [f"{name}:{i}" for name in scope for i, line in enumerate((root / name).read_text().splitlines(), 1) if 'or "admin"' in line or 'or user.get("role")' in line]
    assert offenders == []


# ===================================================================================================== S3
def cancel_body(day, lesson, reason="Отмена", **extra):
    return {"lessonDate": day, "lessonId": lesson, "status": "cancelled", "records": {}, "cancellationReason": reason, **extra}


def test_s3_a_teacher_does_not_cancel_a_future_lesson_himself_nor_does_a_substitute(w):
    r = client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance", json=cancel_body("2026-10-26", "lesson-l1"))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "TEACHER_CANNOT_CANCEL"
    assert "Не смогу провести" in r.json()["detail"]["message"]
    # the substitute of the lesson: the same (his lesson is an assignment, not his decision)
    assert client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher",
                                  json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Болезнь"}).status_code == 200
    r = client(w, "D").post(f"{API}/org/groups/{w.G1.id}/attendance", json=cancel_body("2026-10-26", "lesson-l1"))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "TEACHER_CANNOT_CANCEL"
    assert row_of(w, w.G1, "lesson-l1", "2026-10-26") is None


def test_s3_a_teacher_cannot_cancel_a_started_lesson_either_and_not_ten_minutes_before_it(w, clock):
    clock.set("2026-10-26 09:50")                        # Monday, ten minutes before the lesson (the case of D15)
    assert client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance", json=cancel_body("2026-10-26", "lesson-l1")).status_code == 403
    clock.set("2026-10-26 10:05")
    assert client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"}).status_code == 200
    r = client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance", json=cancel_body("2026-10-26", "lesson-l1"))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "TEACHER_CANNOT_CANCEL"
    assert row_of(w, w.G1, "lesson-l1", "2026-10-26").status == "in_progress"


def test_s3_nothing_the_teacher_sends_gets_past_the_refusal_not_even_a_holiday_kind(w):
    r = client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance", json=cancel_body("2026-10-26", "lesson-l1", cancellationKind="holiday"))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "TEACHER_CANNOT_CANCEL"


def test_s3_lesson_was_not_held_stays_the_teachers_for_a_past_lesson_and_is_in_the_journal(w):
    """«Урока не было» of a PAST lesson that never started: the teacher cancels it (always «по вине центра»), the record of the journal «Замены» says who."""
    body = {"lessonDate": "2026-10-20", "lessonId": "lesson-l3", "cancellationReason": "Урока не было"}      # yesterday's Tuesday lesson of G2: no row, never started
    r = client(w, "A").post(f"{API}/org/groups/{w.G2.id}/attendance/reconcile/cancel", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["cancelled"] is True and r.json()["warnings"] == []
    row = row_of(w, w.G2, "lesson-l3", "2026-10-20")
    assert (row.status, row.cancellation_kind, row.marked_by) == ("cancelled", "centre", w.A.id)
    (rec,) = [x for x in requests_of(w) if x.group_id == w.G2.id]
    assert (rec.status, rec.origin, rec.requester_id, rec.decided_by, rec.decided_via) == ("cancelled", "system", w.A.id, w.A.id, "teacher_cancel")
    assert client(w, "admin").get(f"{SUB}", params={"status": "decided"}).json()["items"][0]["decision"]["via"] == "teacher_cancel"
    # a teacher may not choose «Праздник»
    body2 = {"lessonDate": "2026-10-20", "lessonId": "lesson-l3", "cancellationReason": "Праздник", "cancellationKind": "holiday"}
    assert client(w, "A").post(f"{API}/org/groups/{w.G2.id}/attendance/reconcile/cancel", json=body2).status_code == 403


def test_s3_lesson_was_not_held_is_not_for_a_lesson_that_has_not_passed(w):
    r = client(w, "A").post(f"{API}/org/groups/{w.G1.id}/attendance/reconcile/cancel",
                            json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "cancellationReason": "Заранее"})
    assert r.status_code == 400                                                       # «Нельзя разбирать будущий урок»
    assert row_of(w, w.G1, "lesson-l1", "2026-10-26") is None


# ===================================================================================================== S13
def test_s13_an_owner_or_admin_who_is_a_substitute_reads_his_lessons(w):
    for who in ("owner", "admin"):
        assert client(w, who).get(f"{SUB}/mine").status_code == 200
        assert client(w, who).get(f"{SUB}/mine").json() == {"items": [], "pendingTotal": 0, "assignedToMe": [], "clearedForMe": []}
    put = client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher",
                                 json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "teacherId": str(w.owner.id), "reason": "Болезнь"})
    assert put.status_code == 200, put.text
    mine = client(w, "owner").get(f"{SUB}/mine").json()
    assert [(x["lessonId"], x["lessonDate"], x["groupName"]) for x in mine["assignedToMe"]] == [("lesson-l1", "2026-10-26", "9-А")]
    assert mine["assignedToMe"][0]["replacing"]["id"] == str(w.A.id)
    assert client(w, "admin").get(f"{SUB}/mine").json()["assignedToMe"] == []                    # somebody else's lessons are not his
    # a teacher keeps what he had; the admin of ANOTHER centre sees only his own centre (nothing of this one)
    assert client(w, "A").get(f"{SUB}/mine").status_code == 200
    assert make_client(w, user=w.foreign_admin, org="other", org_role="admin", role="admin").get(f"{SUB}/mine").json()["assignedToMe"] == []


# ===================================================================================================== F5: «Ставка не задана» in every window
def test_rate_missing_is_a_yes_no_for_the_admin_in_both_assignment_windows(w):
    """D3: the lesson sheet's candidates and the decision's candidates both say who has no rate for the type of the group in the month (D has none in D2); rates stay the owner's."""
    sheet_candidates = client(w, "admin").get(f"{API}/org/groups/{w.G1.id}/lesson-teacher-candidates", params={"lessonId": "lesson-l1", "lessonDate": "2026-10-26"})
    assert sheet_candidates.status_code == 200, sheet_candidates.text
    flags = {c["teacherId"]: c["rateMissing"] for c in sheet_candidates.json()["candidates"]}
    assert flags[str(w.D.id)] is True and flags[str(w.B.id)] is False
    item = req(w, "A", [(w.G1, "lesson-l1", "2026-10-26")]).json()["created"][0]
    for who in ("admin", "owner"):
        cands = client(w, who).get(f"{SUB}/{item['id']}/candidates").json()["candidates"]
        assert {c["teacherId"]: c["rateMissing"] for c in cands}[str(w.D.id)] is True


# ===================================================================================================== S28
def test_s28_every_route_takes_its_rights_from_the_org_role_only():
    """The one-off lesson route (router.py create_one_off_lesson) and every route W1 added ask ``is_org_manager`` / the manager dependency, never the global role."""
    root = Path(__file__).resolve().parents[1] / "src" / "auth"
    for name in ("router.py", "ops_router.py", "substitution_router.py", "days_off_router.py", "attendance_access.py"):
        text = (root / name).read_text()
        assert "is_org_admin(" not in text, name
    router = (root / "router.py").read_text()
    block = router[router.index("def create_one_off_lesson("):]
    assert "is_org_admin=is_org_manager(user)" in block[:700]
    assert "def restore_cancelled_lesson" in (root / "substitution_router.py").read_text()
    sub = (root / "substitution_router.py").read_text()
    assert "user: dict = Depends(_manager)" in sub[sub.index("def restore_cancelled_lesson"):][:300]
