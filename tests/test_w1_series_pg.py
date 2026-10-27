"""W1 S8 / S9 / S14: changing and stopping a weekly lesson (PostgreSQL).

Sources: schedule-series-edit-stop.md ДЫРА-1 / ДЫРА-2 / ДЫРА-3 / ДЫРА-12 / А-1 / А-7 / А-9 / T2 / T3 / T6 / T7 / T8 / T15 / I2 / I3.
A change reaches the revisions already planned for a LATER date; a change of the day / time looks at the exams like a move of one lesson does; the substitutes and the requests of
the lessons that vanish are closed with a trace; a stop removes the cancellations set in advance for the dates after it; two writers never meet an IntegrityError."""
from datetime import date

import pytest

from src.auth.ops_rules import OpsError
from src.auth.service import AuthService
from src.models.auth import GroupAttendance, GroupScheduleAudit, GroupScheduleOverride, LessonSubstitutionEvent, LessonTeacherAssignment
from tests.b2_helpers import API, build_d2, clock, decide, needs_pg, pg_session, req, requests_of, row_of, run_parallel  # noqa: F401  (fixtures)
from tests.ops_helpers import client
from tests.test_ops_b2_decide_pg import Sync, one  # noqa: F401

pytestmark = needs_pg
REV = f"{API}/org/schedule/lessons/lesson-l1/recurrence"
END = f"{REV}/end"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")                        # Wednesday
    return build_d2(pg_session)


@pytest.fixture
def sync(monkeypatch):
    return Sync(monkeypatch)


class Exams:
    """Replaces the two calls of ``AuthService`` to exam-service for the exams of the future dates of a series."""

    def __init__(self, monkeypatch) -> None:
        self.exams, self.calls, self.released, self.down = [], [], [], False
        me = self
        monkeypatch.setattr(AuthService, "_fetch_exam_recurring_end_impact", lambda s, **kw: me.impact(kw))
        monkeypatch.setattr(AuthService, "_release_exam_recurring_end", lambda s, **kw: me.release(kw))

    def impact(self, kw):
        self.calls.append(("impact", kw["effective_from"]))
        if self.down:
            raise RuntimeError("Сервис экзаменов временно недоступен; остановка расписания не выполнена")
        blocked = [e for e in self.exams if not e["can_release"]]
        return {"can_end": not blocked, "affected_exam_count": len(self.exams), "releasable_exam_count": len(self.exams) - len(blocked), "blocked_exam_count": len(blocked),
                "exams": self.exams}

    def release(self, kw):
        self.calls.append(("release", kw["effective_from"]))
        self.released = [e["exam_id"] for e in self.exams]
        return list(self.released)

    def add(self, day, *, can_release=True, status="draft", title="Контрольная"):
        self.exams.append({"exam_id": f"ex-{len(self.exams) + 1}", "title": title, "lesson_date": day, "delivery_mode": "online", "status": status, "can_release": can_release,
                           "blocker_reason": None if can_release else "идёт"})


@pytest.fixture
def exams_(monkeypatch):
    return Exams(monkeypatch)


def revise(w, body, who="admin", lesson="lesson-l1", expect=200):
    r = client(w, who).put(f"{API}/org/schedule/lessons/{lesson}/recurrence", json={"reason": "Изменение", **body})
    assert r.status_code == expect, r.text
    return r.json()


def stop(w, day, who="admin", lesson="lesson-l1", expect=200, **extra):
    r = client(w, who).post(f"{API}/org/schedule/lessons/{lesson}/recurrence/end", json={"effectiveFrom": day, "reason": "Курс закончен", **extra})
    assert r.status_code == expect, r.text
    return r.json()


def occurrence(w, day, lesson="lesson-l1"):
    out = client(w, "admin").get(f"{API}/org/schedule/occurrences", params={"date_from": day, "date_to": day}).json()
    return next((o for o in out if o["id"] == lesson), None)


def revisions(w, lesson="lesson-l1"):
    w.db.expire_all()
    return {o.effective_date: o.payload for o in w.db.query(GroupScheduleOverride).filter_by(group_id=w.G1.id, lesson_id=lesson, scope="recurring").order_by(GroupScheduleOverride.effective_date)}


# ======================================================================================================= S8 / ДЫРА-1
def test_s8_a_change_reaches_the_revision_that_is_already_planned_for_a_later_date(w):
    """T2: «время 12:00 с 16.11», then «кабинет 5 с 09.11» -> from 16.11 the room is 5 AND the time is 12:00 (nothing falls back)."""
    revise(w, {"effectiveFrom": "2026-11-16", "startTime": "12:00", "endTime": "13:30"})
    out = revise(w, {"effectiveFrom": "2026-11-09", "room": "Каб. 5"})
    assert out["laterRevisions"] == [{"effectiveFrom": "2026-11-16", "fields": ["room"]}]
    assert out["releasedExamIds"] == []
    for day, (start, room) in {"2026-11-09": ("10:00", "Каб. 5"), "2026-11-16": ("12:00", "Каб. 5"), "2026-11-23": ("12:00", "Каб. 5")}.items():
        o = occurrence(w, day)
        assert (o["startTime"], o["room"]) == (start, room), day
    assert occurrence(w, "2026-11-02")["room"] is None                                              # before the change: untouched
    # the trace: the later revision has an audit row that says why it changed
    audit = w.db.query(GroupScheduleAudit).filter_by(group_id=w.G1.id, lesson_id="lesson-l1", effective_date="2026-11-16").order_by(GroupScheduleAudit.created_at).all()
    assert len(audit) == 2 and "применено и к этому изменению" in audit[-1].reason and audit[-1].before_payload["room"] is None and audit[-1].after_payload["room"] == "Каб. 5"


def test_s8_an_intentional_later_value_of_the_same_field_is_overwritten_the_decision_of_the_lead(w):
    """A later revision that set the SAME field gets the new value too (the answer lists it: the front says «есть изменение с ДД.ММ — оно тоже обновится»)."""
    revise(w, {"effectiveFrom": "2026-11-23", "room": "Каб. 7"})
    out = revise(w, {"effectiveFrom": "2026-11-09", "room": "Каб. 5"})
    assert out["laterRevisions"] == [{"effectiveFrom": "2026-11-23", "fields": ["room"]}]
    assert occurrence(w, "2026-11-23")["room"] == "Каб. 5" and occurrence(w, "2026-11-16")["room"] == "Каб. 5"


def test_s8_no_difference_on_the_date_but_a_later_revision_differs_is_still_a_change(w):
    """The server decides «нет различий» on the date AND on the later revisions (S14): the revision itself is not written when nothing changes on its date."""
    revise(w, {"effectiveFrom": "2026-11-09", "room": "Каб. 5"})
    revise(w, {"effectiveFrom": "2026-11-23", "room": "Каб. 7"})
    out = revise(w, {"effectiveFrom": "2026-11-16", "room": "Каб. 5"})                              # on 16.11 the room is 5 already; the one of 23.11 is 7
    assert out["laterRevisions"] == [{"effectiveFrom": "2026-11-23", "fields": ["room"]}]
    assert sorted(revisions(w)) == ["2026-11-09", "2026-11-23"]                                      # no new row for 16.11
    assert occurrence(w, "2026-11-23")["room"] == "Каб. 5"


def test_s14_the_server_alone_says_there_is_no_difference_with_a_code(w):
    revise(w, {"effectiveFrom": "2026-11-09", "room": "Каб. 5"})
    r = client(w, "admin").put(REV, json={"effectiveFrom": "2026-11-16", "room": "Каб. 5", "reason": "То же"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "NO_CHANGES"
    assert r.json()["detail"]["message"] == "Укажите хотя бы одно изменение постоянного расписания"       # the text the front recognises


def test_s14_a_sibling_series_whose_state_on_the_date_differs_from_the_template_of_today_is_changed(w):
    """А-1: Wednesday has a revision «с 04.11 в 15:00»; the template of today says 16:00; «с 11.11 в 16:00» is the template of today but NOT the state on that date."""
    revise(w, {"effectiveFrom": "2026-11-04", "startTime": "15:00", "endTime": "16:30"}, lesson="lesson-l2")
    assert occurrence(w, "2026-11-11", "lesson-l2")["startTime"] == "15:00"
    revise(w, {"effectiveFrom": "2026-11-11", "startTime": "16:00", "endTime": "17:30"}, lesson="lesson-l2")
    assert occurrence(w, "2026-11-11", "lesson-l2")["startTime"] == "16:00" and occurrence(w, "2026-11-04", "lesson-l2")["startTime"] == "15:00"
    r = client(w, "admin").put(f"{API}/org/schedule/lessons/lesson-l2/recurrence", json={"effectiveFrom": "2026-11-11", "startTime": "16:00", "endTime": "17:30", "reason": "То же"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "NO_CHANGES"


def test_s8_the_same_date_twice_is_one_revision_and_two_audit_rows(w):
    revise(w, {"effectiveFrom": "2026-11-09", "room": "Каб. 5"})
    revise(w, {"effectiveFrom": "2026-11-09", "room": "Каб. 6"})
    assert list(revisions(w)) == ["2026-11-09"] and revisions(w)["2026-11-09"]["room"] == "Каб. 6"
    assert w.db.query(GroupScheduleAudit).filter_by(group_id=w.G1.id, lesson_id="lesson-l1", effective_date="2026-11-09").count() == 2


def test_s8_a_revision_after_the_stop_is_dead_and_is_not_patched(w):
    revise(w, {"effectiveFrom": "2026-11-30", "room": "Каб. 9"})
    stop(w, "2026-11-23")
    out = revise(w, {"effectiveFrom": "2026-11-09", "room": "Каб. 5"})
    assert out["laterRevisions"] == [] and revisions(w)["2026-11-30"]["room"] == "Каб. 9"


# ======================================================================================================= S8 / ДЫРА-2: the exams
def test_s8_a_change_of_the_day_asks_for_confirmation_when_exams_hang_on_the_dates(w, exams_):
    exams_.add("2026-11-09")
    r = client(w, "admin").put(REV, json={"effectiveFrom": "2026-11-03", "dayOfWeek": 5, "reason": "День"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "EXAMS_NEED_CONFIRMATION"
    assert r.json()["detail"]["exams"] == [{"examId": "ex-1", "title": "Контрольная", "lessonDate": "2026-11-09", "status": "draft"}]
    assert revisions(w) == {} and exams_.released == []                                              # nothing written, nothing released
    out = revise(w, {"effectiveFrom": "2026-11-03", "dayOfWeek": 5, "releaseAffectedExams": True})
    assert out["releasedExamIds"] == ["ex-1"] and occurrence(w, "2026-11-13")["dayOfWeek"] == 5 and occurrence(w, "2026-11-09") is None
    assert exams_.calls == [("impact", "2026-11-03"), ("impact", "2026-11-03"), ("release", "2026-11-03")]


def test_s8_a_live_exam_refuses_a_change_of_the_day_whatever_the_flag(w, exams_):
    exams_.add("2026-11-09", can_release=False, status="active", title="Итоговая")
    r = client(w, "admin").put(REV, json={"effectiveFrom": "2026-11-03", "dayOfWeek": 5, "reason": "День", "releaseAffectedExams": True})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "LESSON_HAS_LIVE_EXAM" and "«Итоговая»" in r.json()["detail"]["message"]
    assert revisions(w) == {}


def test_s8_a_change_that_keeps_the_dates_never_asks_exam_service_not_even_the_time(w, exams_):
    """The lead's narrowing: a time-only change keeps the (lesson, date) identity, the exam binding stays - exam-service is not asked, no ``releaseAffectedExams``."""
    exams_.add("2026-11-09")
    out = revise(w, {"effectiveFrom": "2026-11-03", "room": "Каб. 5", "topic": "Тема"})
    assert exams_.calls == [] and out["releasedExamIds"] == [] and revisions(w)["2026-11-03"]["room"] == "Каб. 5"
    out = revise(w, {"effectiveFrom": "2026-11-16", "startTime": "12:00", "endTime": "13:30"})
    assert exams_.calls == [] and out["releasedExamIds"] == [] and occurrence(w, "2026-11-16")["startTime"] == "12:00"


def test_s8_exam_service_down_refuses_a_change_of_the_day_with_503_and_writes_nothing(w, exams_):
    exams_.down = True
    r = client(w, "admin").put(REV, json={"effectiveFrom": "2026-11-03", "dayOfWeek": 5, "reason": "День"})
    assert r.status_code == 503 and revisions(w) == {}
    assert revise(w, {"effectiveFrom": "2026-11-03", "room": "Каб. 5"})["room"] == "Каб. 5"             # a room does not depend on exam-service
    assert revise(w, {"effectiveFrom": "2026-11-16", "startTime": "12:00", "endTime": "13:30"})["startTime"] == "12:00"        # nor does a time


def test_s8_a_failed_release_rolls_the_revision_back(w, exams_, monkeypatch):
    exams_.add("2026-11-09")

    def boom(self, **kw):
        raise RuntimeError("Не удалось безопасно вернуть экзамены в черновики")

    monkeypatch.setattr(AuthService, "_release_exam_recurring_end", boom)
    r = client(w, "admin").put(REV, json={"effectiveFrom": "2026-11-03", "dayOfWeek": 5, "reason": "День", "releaseAffectedExams": True})
    assert r.status_code == 503 and revisions(w) == {}


# ======================================================================================================= S8 / ДЫРА-3, А-9: what hangs on the lessons that vanish
def test_s8_a_change_of_the_day_closes_the_substitutes_and_the_requests_of_the_lessons_that_vanish(w):
    assert client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-11-09", "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Болезнь"}).status_code == 200
    item = one(w, "A", "L1", "2026-11-16")
    out = revise(w, {"effectiveFrom": "2026-11-03", "dayOfWeek": 5})
    assert [x["code"] for x in out["warnings"] if x["code"] == "SUBSTITUTIONS_CLOSED"] == ["SUBSTITUTIONS_CLOSED"]
    w.db.expire_all()
    asg = w.db.query(LessonTeacherAssignment).filter_by(group_id=w.G1.id, lesson_date="2026-11-09").one()
    assert asg.cancelled_at is not None and asg.cancelled_by == w.admin.id
    assert [e.snapshot["trigger"] for e in w.db.query(LessonSubstitutionEvent).filter_by(org_id=w.org.id, action="assignment_cleared")] == ["schedule_changed"]
    closed = [r for r in requests_of(w) if str(r.id) == item["id"]][0]
    assert (closed.status, closed.closed_reason) == ("closed", "LESSON_REMOVED")
    assert [e.snapshot.get("trigger") for e in w.db.query(LessonSubstitutionEvent).filter_by(request_id=closed.id, action="closed")] == ["schedule_changed"]
    assert client(w, "D").get(f"{API}/org/substitutions/mine").json()["assignedToMe"] == []


def test_s8_a_change_of_the_time_keeps_the_substitute_and_the_request_follows_the_time(w):
    assert client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-11-09", "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Болезнь"}).status_code == 200
    item = one(w, "A", "L1", "2026-11-16")
    out = revise(w, {"effectiveFrom": "2026-11-03", "startTime": "12:00", "endTime": "13:30"})
    assert [x for x in out["warnings"] if x["code"] == "SUBSTITUTIONS_CLOSED"] == []
    w.db.expire_all()
    assert w.db.query(LessonTeacherAssignment).filter_by(group_id=w.G1.id, lesson_date="2026-11-09").one().cancelled_at is None
    pending = [r for r in requests_of(w) if str(r.id) == item["id"]][0]
    assert (pending.status, pending.start_time, pending.end_time) == ("pending", "12:00", "13:30")


# ======================================================================================================= S9: the stop
def test_s9_the_stop_closes_what_hangs_on_the_lessons_that_stop_and_removes_the_cancellations_set_in_advance(w):
    assert client(w, "admin").put(f"{API}/org/groups/{w.G1.id}/lesson-teacher", json={"lessonDate": "2026-11-23", "lessonId": "lesson-l1", "teacherId": str(w.D.id), "reason": "Болезнь"}).status_code == 200
    item = one(w, "A", "L1", "2026-11-30")
    cancel = lambda day, **kw: client(w, "admin").post(f"{API}/org/groups/{w.G1.id}/attendance", json={
        "lessonDate": day, "lessonId": "lesson-l1", "status": "cancelled", "records": {}, "cancellationReason": "Заранее", **kw})
    assert cancel("2026-11-09").status_code == 200 and cancel("2026-11-16", cancellationKind="holiday").status_code == 200
    assert client(w, "admin").post(f"{API}/org/groups/{w.G1.id}/lesson-move", json={"lessonId": "lesson-l1", "lessonDate": "2026-12-07", "newDate": "2026-12-09", "startTime": "10:00",
                                                                                   "endTime": "11:30", "reason": "Перенос"}).status_code == 200
    out = stop(w, "2026-11-12")
    assert (out["closedRequests"], out["releasedSubstitutes"], out["removedCancellations"]) == (1, 1, 1)             # the cancellation of 16.11 (11.11 and 9.11 are before)
    assert row_of(w, w.G1, "lesson-l1", "2026-11-09") is not None                                    # before the stop: stays
    assert row_of(w, w.G1, "lesson-l1", "2026-11-16") is None                                        # after it: no phantom «Отменён» column
    assert row_of(w, w.G1, "lesson-l1", "2026-12-07").status == "cancelled"                          # the cancelled ORIGINAL of a move stays (after the stop too): its copy lives on
    w.db.expire_all()
    assert w.db.query(LessonTeacherAssignment).filter_by(group_id=w.G1.id, lesson_date="2026-11-23").one().cancelled_at is not None
    closed = [r for r in requests_of(w) if str(r.id) == item["id"]][0]
    assert (closed.status, closed.closed_reason) == ("closed", "LESSON_REMOVED")
    audit = w.db.query(GroupScheduleAudit).filter_by(group_id=w.G1.id, lesson_id="lesson-l1", scope="recurring_end").one()
    assert audit.after_payload["removedCancellations"] == [{"date": "2026-11-16", "cancellationKind": "holiday", "cancellationReason": "Заранее", "dayOffId": None}]
    assert audit.after_payload["closedRequests"] == 1 and audit.after_payload["releasedSubstitutes"] == 1
    assert occurrence(w, "2026-11-30") is None


def test_s9_a_stop_with_nothing_hanging_has_zero_counts(w):
    out = stop(w, "2026-11-12")
    assert (out["closedRequests"], out["releasedSubstitutes"], out["removedCancellations"]) == (0, 0, 0) and out["ended"] is True


def test_s9_the_stop_with_exams_and_the_exam_service_down_is_as_before(w, exams_):
    exams_.add("2026-11-16")
    r = client(w, "admin").post(END, json={"effectiveFrom": "2026-11-12", "reason": "Курс закончен"})
    assert r.status_code == 409                                                                       # needs ``releaseAffectedExams`` (the old contract)
    assert stop(w, "2026-11-12", releaseAffectedExams=True)["releasedExamIds"] == ["ex-1"]


# ======================================================================================================= ДЫРА-12: two writers
def test_s6_two_stops_of_one_series_at_once_are_one_stop_and_a_refusal_never_a_500(w):
    from src.auth.schemas import EndRecurringLessonRequest

    w.db.commit()

    def end(svc):
        return svc.end_recurring_lesson(str(w.org.id), "lesson-l1", EndRecurringLessonRequest(effectiveFrom=date(2026, 11, 12), reason="Курс закончен"),
                                        actor_id=str(w.admin.id), is_org_admin=True).ended

    (a, ea), (b, eb) = run_parallel(w, end, end)
    errors = [e for e in (ea, eb) if e is not None]
    assert [a, b].count(True) == 1 and len(errors) == 1 and isinstance(errors[0], ValueError) and "уже прекращены" in str(errors[0]), (a, ea, b, eb)
    w.db.expire_all()
    assert w.db.query(GroupScheduleOverride).filter_by(group_id=w.G1.id, lesson_id="lesson-l1", scope="recurring_end").count() == 1


def test_s6_two_revisions_of_two_dates_at_once_both_apply_and_the_later_one_carries_the_earlier_field(w):
    from src.auth.schemas import ReviseRecurringLessonRequest

    w.db.commit()

    def rev(day, **kw):
        return lambda svc: svc.revise_recurring_lesson(str(w.org.id), "lesson-l1", ReviseRecurringLessonRequest(effectiveFrom=day, reason="Изменение", **kw),
                                                       actor_id=str(w.admin.id), is_org_admin=True).id

    results = run_parallel(w, rev(date(2026, 11, 16), startTime="12:00", endTime="13:30"), rev(date(2026, 11, 9), room="Каб. 5"))
    assert [e for _, e in results if e is not None] == [], results
    assert occurrence(w, "2026-11-23")["startTime"] == "12:00" and occurrence(w, "2026-11-23")["room"] == "Каб. 5"             # I2: nothing fell back, whoever won the race
