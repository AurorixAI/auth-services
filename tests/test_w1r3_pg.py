"""W1 server R3: the 17 findings of the review + F10 (the main teacher sees his substituted lessons) + F6 (the revisions of a series) (PostgreSQL).  One test (or more) per finding."""
import threading
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from src.auth.service import AuthService
from src.models.auth import (
    GroupAttendance,
    GroupLessonTypeHistory,
    LessonSubstitutionRequest as Req,
    LessonTeacherAssignment,
    OrgDayOff,
    StudentGroup,
)
from tests.b2_helpers import API, build_d2, clock, decide, events_of, needs_pg, pg_session, req, requests_of, row_of  # noqa: F401  (fixtures)
from tests.ops_helpers import client, history_row, new_group, new_slot
from tests.pg_world import give_group_price, make_client
from tests.test_ops_b2_decide_pg import Sync, one  # noqa: F401
from tests.test_w1_series_pg import Exams  # noqa: F401

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"
DAYS = f"{API}/org/days-off"
REV = f"{API}/org/schedule/lessons/lesson-l1/recurrence"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")                        # Wednesday
    return build_d2(pg_session)


@pytest.fixture
def sync(monkeypatch):
    return Sync(monkeypatch)


@pytest.fixture
def exams_(monkeypatch):
    return Exams(monkeypatch)


def sheet(w, who, group, lesson, day, reason="Преподаватель болен", expect=200, **extra):
    r = client(w, who).post(f"{API}/org/groups/{group.id}/attendance", json={"lessonDate": day, "lessonId": lesson, "status": "cancelled", "records": {}, "cancellationReason": reason, **extra})
    assert r.status_code == expect, r.text
    return r


def put_teacher(w, group, lesson, day, teacher, expect=200):
    r = client(w, "admin").put(f"{API}/org/groups/{group.id}/lesson-teacher", json={"lessonDate": day, "lessonId": lesson, "teacherId": str(teacher.id), "reason": "Болезнь"})
    assert r.status_code == expect, r.text
    return r


def kind(w, group, lesson, day, to="holiday", who="admin"):
    return client(w, who).put(f"{API}/org/groups/{group.id}/attendance/cancellation-kind", json={"lessonDate": day, "lessonId": lesson, "cancellationKind": to, "reason": "Исправление"})


def old_group(w):
    """A group with a Monday lesson valid since January: its Mondays of the past exist in the schedule (31.08 is OLDER than the window of the unclosed lessons, 07.09 is inside)."""
    g = new_group(w.db, w.org, w.ids, "Gx", "Старая", w.C, [new_slot("lesson-x", 1, "10:00", "11:00", "2026-01-01")])
    w.db.commit()
    return g


def legacy_cancel(w, group, day, kind_="centre"):
    w.db.add(GroupAttendance(org_id=w.org.id, group_id=group.id, lesson_date=day, lesson_id="lesson-x", status="cancelled", records={}, roster_snapshot=[], cancellation_reason="Давно",
                             cancellation_kind=kind_, marked_by=w.admin.id, version=1))
    w.db.commit()


# ====================================================================================================== 1, 2, 17: the day off
def test_r3_1_the_kind_of_a_date_held_by_a_live_day_off_is_not_changed(w, sync):
    sync.mode = "live"
    made = client(w, "owner").post(DAYS, json={"dateFrom": "2026-10-28", "dateTo": "2026-10-28", "reason": "Хайит"}).json()
    sync.mode = "ok"
    sheet(w, "admin", w.G1, "lesson-l2", "2026-10-28")                                      # a holiday of that day off already (W1 S2)
    r = kind(w, w.G1, "lesson-l2", "2026-10-28", to="centre")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CANCELLED_BY_DAY_OFF"
    assert r.json()["detail"]["message"] == "Дата — выходной день центра: причина отмены — праздник"
    assert row_of(w, w.G1, "lesson-l2", "2026-10-28").cancellation_kind == "holiday"
    # a legacy row of the date (no day off id) is refused the same way
    w.db.query(GroupAttendance).filter_by(group_id=w.G1.id, lesson_date="2026-10-28").update({"day_off_id": None, "cancellation_kind": "centre"})
    w.db.commit()
    assert kind(w, w.G1, "lesson-l2", "2026-10-28", to="holiday").json()["detail"]["code"] == "CANCELLED_BY_DAY_OFF"
    assert made["dayOff"]["id"]


def test_r3_1_a_converted_cancellation_belongs_to_the_day_off(w):
    sheet(w, "admin", w.G1, "lesson-l2", "2026-10-28")                                       # by hand, «по вине центра»
    made = client(w, "owner").post(DAYS, json={"dateFrom": "2026-10-28", "dateTo": "2026-10-28", "reason": "Хайит"}).json()
    assert [x["lessonId"] for x in made["converted"]] == ["lesson-l2"]
    row = row_of(w, w.G1, "lesson-l2", "2026-10-28")
    assert (row.cancellation_kind, str(row.day_off_id), row.version) == ("holiday", made["dayOff"]["id"], 2)
    out = client(w, "owner").post(f"{DAYS}/{made['dayOff']['id']}/void").json()           # the day off lists what it owns; a hand-made cancellation (version 2 now) is NOT given back
    assert out["restored"] == [] and [(x["lessonId"], x["reason"]) for x in out["kept"]] == [("lesson-l2", "Урок изменён после выходного")]
    assert row_of(w, w.G1, "lesson-l2", "2026-10-28").status == "cancelled"


def test_r3_2_a_day_off_does_not_convert_cancellations_older_than_the_window(w):
    g = old_group(w)
    legacy_cancel(w, g, "2026-08-31")                                                       # older than 01.09: a closed month
    legacy_cancel(w, g, "2026-09-07")                                                       # inside the window
    body = {"dateFrom": "2026-08-31", "dateTo": "2026-09-07"}
    plan = client(w, "owner").post(f"{DAYS}/preview", json=body).json()
    assert [x["date"] for x in plan["convertible"] if x["lessonId"] == "lesson-x"] == ["2026-09-07"]
    assert [(x["date"], x["reason"]) for x in plan["blocked"] if x["lessonId"] == "lesson-x"] == [("2026-08-31", "Отмена «по вине центра» старше окна незакрытых уроков: её вид не меняется (закрытый период не пересчитывается)")]
    made = client(w, "owner").post(DAYS, json={**body, "reason": "Задним числом"}).json()
    assert [x["date"] for x in made["converted"] if x["lessonId"] == "lesson-x"] == ["2026-09-07"]
    assert [x["date"] for x in made["blocked"] if x["lessonId"] == "lesson-x"] == ["2026-08-31"]
    w.db.expire_all()
    old = w.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_date="2026-08-31").one()
    new = w.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_date="2026-09-07").one()
    assert (old.cancellation_kind, old.day_off_id, old.version) == ("centre", None, 1)           # untouched
    assert (new.cancellation_kind, str(new.day_off_id), new.version) == ("holiday", made["dayOff"]["id"], 2)


def test_r3_2_a_row_that_changed_meanwhile_is_not_converted(w, monkeypatch):
    """The conversion locks the row and reads it again: a row that is no longer a hand-made «по вине центра» (a second writer made it a holiday) stays as it is, the version is not bumped."""
    from src.auth.days_off import DaysOffService

    g = old_group(w)
    legacy_cancel(w, g, "2026-09-07")
    row = w.db.query(GroupAttendance).filter_by(group_id=g.id, lesson_date="2026-09-07").one()
    stale = SimpleNamespace(id=row.id, **{k: getattr(row, k) for k in ("status", "cancellation_kind", "day_off_id")})
    w.db.query(GroupAttendance).filter_by(id=row.id).update({"cancellation_kind": "holiday"})
    w.db.commit()
    svc = DaysOffService(AuthService(w.db))
    assert svc._convert(stale, g, UUID(int=1), "Хайит", str(w.owner.id)) is False
    assert w.db.query(GroupAttendance).filter_by(id=row.id).one().version == 1


def test_r3_17_the_lesson_handed_to_the_other_day_off_carries_its_reason(w):
    first = client(w, "owner").post(DAYS, json={"dateFrom": "2026-10-28", "dateTo": "2026-10-29", "reason": "Первый"}).json()
    client(w, "owner").post(DAYS, json={"dateFrom": "2026-10-28", "dateTo": "2026-10-30", "reason": "Второй"})
    client(w, "owner").post(f"{DAYS}/{first['dayOff']['id']}/void")
    assert {r.cancellation_reason for r in w.db.query(GroupAttendance).filter_by(org_id=w.org.id, status="cancelled")} == {"Выходной (Второй)"}


# ====================================================================================================== 3, 4: the series edit
def rev(w, body, lesson="lesson-l1", expect=200):
    r = client(w, "admin").put(f"{API}/org/schedule/lessons/{lesson}/recurrence", json={"reason": "Изменение", **body})
    assert r.status_code == expect, r.text
    return r


def test_r3_3_a_later_revision_that_the_patch_changes_is_checked_for_the_teacher_too(w):
    rev(w, {"effectiveFrom": "2026-11-23", "dayOfWeek": 3, "startTime": "10:00", "endTime": "11:30"})            # from 23.11 Wednesday 10:00 (A is free then)
    before = {o.effective_date: dict(o.payload) for o in w.db.query(__import__("src.models.auth", fromlist=["GroupScheduleOverride"]).GroupScheduleOverride).filter_by(group_id=w.G1.id, lesson_id="lesson-l1")}
    # the patch moves the time to 16:00-17:30 from 09.11: for 09.11 on that is Monday (free), but the LATER revision becomes Wednesday 16:00 = the lesson l2 of the same teacher
    r = rev(w, {"effectiveFrom": "2026-11-09", "startTime": "16:00", "endTime": "17:30"}, expect=409)
    assert r.json()["detail"]["code"] == "TEACHER_BUSY"
    w.db.expire_all()
    after = {o.effective_date: dict(o.payload) for o in w.db.query(__import__("src.models.auth", fromlist=["GroupScheduleOverride"]).GroupScheduleOverride).filter_by(group_id=w.G1.id, lesson_id="lesson-l1")}
    assert after == before                                                                     # nothing was written


def test_r3_3_the_room_warnings_of_a_later_revision_are_in_the_answer(w):
    other = new_group(w.db, w.org, w.ids, "Gr", "Кабинет", w.C, [dict(new_slot("lesson-r", 5, "10:00", "11:30", "2026-10-12"), room="Каб. 9")])
    w.db.commit()
    rev(w, {"effectiveFrom": "2026-11-23", "dayOfWeek": 5})                                     # a later revision on Fridays
    out = rev(w, {"effectiveFrom": "2026-11-09", "room": "Каб. 9"}).json()                      # the room of the other group, on Fridays only through the LATER revision
    assert [x for x in out["warnings"] if x["code"] == "ROOM_BUSY"] and out["laterRevisions"][0]["effectiveFrom"] == "2026-11-23"


def test_r3_4_the_exam_check_starts_where_the_day_really_changes(w, exams_):
    rev(w, {"effectiveFrom": "2026-11-09", "dayOfWeek": 2, "startTime": "12:00", "endTime": "13:30"})
    rev(w, {"effectiveFrom": "2026-11-23", "dayOfWeek": 5})
    exams_.calls.clear()
    # 16.11: the state is already Tuesday (no change there), the revision of 23.11 (Friday) becomes Tuesday: the day changes from 23.11
    rev(w, {"effectiveFrom": "2026-11-16", "dayOfWeek": 2})
    assert exams_.calls[0] == ("impact", "2026-11-23")
    # a time-only change asks nobody
    exams_.calls.clear()
    rev(w, {"effectiveFrom": "2026-11-30", "startTime": "14:00", "endTime": "15:30"})
    assert exams_.calls == []


# ====================================================================================================== 5: role change
def test_r3_5_a_request_decided_meanwhile_is_not_closed_by_the_role_change(w):
    from sqlalchemy.orm import sessionmaker

    from src.auth.lesson_substitutions import on_role_changed

    put_teacher(w, w.G1, "lesson-l1", "2026-10-26", w.D)
    mine = req(w, "D", [(w.G1, "lesson-l1", "2026-10-26")]).json()["created"][0]
    w.db.commit()
    engine = w.db.get_bind()
    other = sessionmaker(engine, autoflush=False)()
    other.query(Req).filter(Req.id == UUID(mine["id"])).with_for_update().first()               # a decision is in the middle of its transaction: the row is locked
    result = {}

    def run():
        s = sessionmaker(engine, autoflush=False)()
        try:
            result["closed"] = on_role_changed(AuthService(s), w.org.id, w.D.id, w.owner.id)
            s.commit()
        finally:
            s.close()

    t = threading.Thread(target=run)
    t.start()
    t.join(timeout=1.0)
    assert t.is_alive()                                                                          # it waits for the lock instead of closing a request that is being decided
    other.query(Req).filter(Req.id == UUID(mine["id"])).update({"status": "rejected", "decided_at": datetime.utcnow(), "decided_via": "request"})
    other.commit()
    other.close()
    t.join(timeout=10)
    assert result["closed"] == 0
    w.db.expire_all()
    assert [r.status for r in requests_of(w) if str(r.id) == mine["id"]] == ["rejected"]


# ====================================================================================================== 6, 7: the rate and the estimate
def test_r3_6_the_rate_is_looked_for_by_the_type_of_the_group_on_the_day_of_the_lesson(w):
    w.db.add(GroupLessonTypeHistory(group_id=w.G1.id, valid_from=date(2026, 10, 1), lesson_type="pair"))      # since 01.10 the group is a «Парный» one; B has a rate for «group» only
    w.db.commit()
    cands = client(w, "owner").get(f"{API}/org/groups/{w.G1.id}/lesson-teacher-candidates", params={"lessonId": "lesson-l1", "lessonDate": "2026-10-26"}).json()["candidates"]
    assert {c["teacherId"]: c["rateMissing"] for c in cands}[str(w.B.id)] is True
    item = req(w, "A", [(w.G1, "lesson-l1", "2026-10-26")]).json()["created"][0]
    assert {c["teacherId"]: c["rateMissing"] for c in client(w, "admin").get(f"{SUB}/{item['id']}/candidates").json()["candidates"]}[str(w.B.id)] is True


def paid_world(w):
    """The price of the group and a payment of two of the three students that covers the lesson of 26.10 (the third is unpaid)."""
    from tests.payf_helpers import pay, set_payments

    give_group_price(w.db, w.G1, 600_000)
    set_payments(w, accounting_start_date="2026-09-01")
    for s in (w.s1, w.s2):
        pay(w, w.db.query(StudentGroup).filter_by(group_id=w.G1.id, student_id=s.id).one(), 1_800_000, date(2026, 10, 20))


def test_r3_7_the_estimate_is_the_owners_alone_and_counts_only_covered_lessons(w):
    paid_world(w)
    url = f"{API}/org/groups/{w.G1.id}/lesson-teacher-candidates"
    q = {"lessonId": "lesson-l1", "lessonDate": "2026-10-26"}
    owner = {c["teacherId"]: c for c in client(w, "owner").get(url, params=q).json()["candidates"]}
    assert (owner[str(w.B.id)]["substitutePayEstimate"], owner[str(w.B.id)]["substitutePayEstimateReason"]) == (30_000, None)      # 2 paid students x 600 000 x 30 % / 12; the unpaid one is out
    assert (owner[str(w.D.id)]["substitutePayEstimate"], owner[str(w.D.id)]["substitutePayEstimateReason"]) == (None, "RATE_MISSING")
    admin = {c["teacherId"]: c for c in client(w, "admin").get(url, params=q).json()["candidates"]}
    assert all((c["substitutePayEstimate"], c["substitutePayEstimateReason"]) == (None, "HIDDEN") for c in admin.values())
    assert admin[str(w.B.id)]["rateMissing"] is False and admin[str(w.D.id)]["rateMissing"] is True        # the yes / no stays for both
    item = req(w, "A", [(w.G1, "lesson-l1", "2026-10-26")]).json()["created"][0]
    assert {c["teacherId"]: c["substitutePayEstimate"] for c in client(w, "owner").get(f"{SUB}/{item['id']}/candidates").json()["candidates"]}[str(w.B.id)] == 30_000
    assert all(c["substitutePayEstimateReason"] == "HIDDEN" for c in client(w, "admin").get(f"{SUB}/{item['id']}/candidates").json()["candidates"])


def test_r3_7_a_package_student_counts_by_his_package_when_it_still_has_lessons(w, monkeypatch):
    """One calendar student (covered, D = the divisor 12) + one package student (a package of 8 with lessons left) = 600 000 x 30 % x (1/12 + 1/8) = 37 500; a package student with none left and an
    unpaid calendar student are out; no student at all has no estimate."""
    from src.auth import payroll_estimate
    from src.auth.payments_service import PaymentService

    give_group_price(w.db, w.G1, 600_000)
    mids = {str(s.id): w.db.query(StudentGroup).filter_by(group_id=w.G1.id, student_id=s.id).one().id for s in (w.s1, w.s2, w.s3)}
    price = SimpleNamespace(amount=600_000)
    cover = lambda paid: SimpleNamespace(paid=paid, price=price if paid else None)
    cal = SimpleNamespace(lessons_windows=(), lessons=None, segments=[], lesson_cover=lambda day, lid: cover(True))
    pack = SimpleNamespace(lessons_windows=((date(2026, 1, 1), None),), lessons=SimpleNamespace(left=3), segments=[SimpleNamespace(kind="package", nominal_days=8, price=price)])
    unpaid = SimpleNamespace(lessons_windows=(), lessons=None, segments=[], lesson_cover=lambda day, lid: cover(False))
    entries = {mids[str(w.s1.id)]: cal, mids[str(w.s2.id)]: pack, mids[str(w.s3.id)]: unpaid}
    loader = SimpleNamespace(run=lambda ids: [SimpleNamespace(membership=SimpleNamespace(id=i), ledger=entries[i]) for i in ids if i in entries], settings=SimpleNamespace(regimes=[]))
    monkeypatch.setattr(PaymentService, "_loader", lambda self, org_id, *a, **kw: loader)
    out = payroll_estimate.substitute_pay_estimates(w.svc, w.org.id, w.G1, "lesson-l1", "2026-10-26", 600, [w.B.id, w.D.id], is_owner=True)
    assert out[w.B.id] == {"amount": 37_500, "reason": None} and out[w.D.id] == {"amount": None, "reason": "RATE_MISSING"}
    entries[mids[str(w.s2.id)]] = SimpleNamespace(**{**vars(pack), "lessons": SimpleNamespace(left=0)})
    assert payroll_estimate.substitute_pay_estimates(w.svc, w.org.id, w.G1, "lesson-l1", "2026-10-26", 600, [w.B.id], is_owner=True)[w.B.id]["amount"] == 15_000
    empty = new_group(w.db, w.org, w.ids, "Ge", "Без учеников", w.C, [new_slot("lesson-e", 1, "12:00", "13:00", "2026-10-12")])
    w.db.commit()
    assert payroll_estimate.substitute_pay_estimates(w.svc, w.org.id, empty, "lesson-e", "2026-10-26", 720, [w.B.id], is_owner=True)[w.B.id] == {"amount": None, "reason": "NO_STUDENTS"}
    assert payroll_estimate.substitute_pay_estimates(w.svc, w.org.id, empty, "lesson-e", "2026-10-26", 720, [w.B.id], is_owner=False)[w.B.id] == {"amount": None, "reason": "HIDDEN"}


# ====================================================================================================== 8, 9: repeats
def test_r3_8_an_exact_repeat_of_the_teachers_cancellation_of_his_empty_started_lesson_is_a_200(w, clock):
    group = new_group(w.db, w.org, w.ids, "Gl", "Пустая", w.C, [new_slot("lesson-e", 3, "16:00", "17:30", "2026-10-14")])
    history_row(w.db, w.org, group, "lesson-e", "2026-10-21", w.C, [], status="in_progress", start="16:00")
    w.db.commit()
    clock.set("2026-10-21 17:20")
    body = {"lessonDate": "2026-10-21", "lessonId": "lesson-e", "status": "cancelled", "records": {}, "cancellationReason": "Урока не было"}
    first = client(w, "C").post(f"{API}/org/groups/{group.id}/attendance", json=body)
    again = client(w, "C").post(f"{API}/org/groups/{group.id}/attendance", json=body)
    assert first.status_code == 200 and again.status_code == 200 and again.json()["version"] == first.json()["version"]
    assert len([x for x in requests_of(w) if x.group_id == group.id]) == 1
    other = client(w, "C").post(f"{API}/org/groups/{group.id}/attendance", json={**body, "cancellationReason": "Другая"})
    assert other.status_code == 409 and other.json()["detail"]["code"] == "ALREADY_CANCELLED"


def test_r3_9_lesson_was_not_held_gives_the_same_refusals_as_the_other_roads(w, clock):
    base = {"lessonDate": "2026-10-20", "lessonId": "lesson-l3", "cancellationReason": "Урока не было"}
    url = f"{API}/org/groups/{w.G2.id}/attendance/reconcile/cancel"
    assert client(w, "admin").post(url, json=base).status_code == 200
    assert client(w, "admin").post(url, json=base).status_code == 200                           # an exact repeat
    r = client(w, "admin").post(url, json={**base, "cancellationReason": "Совсем другая"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ALREADY_CANCELLED"
    # a moved original that is past now: ALREADY_MOVED
    moved = client(w, "admin").post(f"{API}/org/groups/{w.G1.id}/lesson-move", json={"lessonId": "lesson-l1", "lessonDate": "2026-10-26", "newDate": "2026-10-28", "startTime": "12:00",
                                                                                   "endTime": "13:30", "reason": "Перенос"})
    assert moved.status_code == 200
    clock.set("2026-10-27 12:00")
    r2 = client(w, "admin").post(f"{API}/org/groups/{w.G1.id}/attendance/reconcile/cancel", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "cancellationReason": "Не было"})
    assert r2.status_code == 409 and r2.json()["detail"]["code"] == "ALREADY_MOVED"


def test_r3_9_a_repeated_decision_answers_the_real_version_and_kind(w):
    item = one(w, "A", "L1", "2026-11-02")
    first = decide(w, "admin", item["id"], decision="cancel", reason="Праздник", cancellationKind="holiday")
    again = decide(w, "admin", item["id"], decision="cancel", reason="Праздник", cancellationKind="holiday")
    assert first.json()["result"]["cancellationKind"] == again.json()["result"]["cancellationKind"] == "holiday"
    assert kind(w, w.G1, "lesson-l1", "2026-11-02", to="centre").status_code == 200              # version 2 now
    third = decide(w, "admin", item["id"], decision="cancel", reason="Праздник", cancellationKind="holiday")
    assert (third.json()["result"]["version"], third.json()["result"]["cancellationKind"]) == (2, "centre")


# ====================================================================================================== 10, 11, 12: the archive and the restore
def test_r3_10_archive_with_missed_lessons_releases_their_substitutes_and_the_return_has_no_false_busy(w):
    from src.auth.membership_periods import close_period
    from tests.ops_helpers import new_student

    group = new_group(w.db, w.org, w.ids, "Ea", "Архивная", w.C, [new_slot("lesson-e", 3, "12:00", "13:00", "2026-10-14")])
    gone = new_student(w.db, w.org, w.ids, "gone", "Ушёл", "Тест", group, joined=datetime(2026, 9, 1, 5, 0))       # he was in the class at 12:00 and left after it: the lessons are «missed», nobody is active
    membership = w.db.query(StudentGroup).filter_by(group_id=group.id, student_id=gone.id).one()
    close_period(membership, datetime(2026, 10, 21, 9, 0))
    membership.is_active = False
    w.db.commit()
    put_teacher(w, group, "lesson-e", "2026-10-21", w.D)                                         # today's lesson (12:00) has passed and nobody took it: D is named as its substitute
    busy = new_group(w.db, w.org, w.ids, "Eb", "Занятая", w.D, [new_slot("lesson-b", 3, "12:30", "13:30", "2026-10-12")])
    w.db.commit()
    r = client(w, "admin").post(f"{API}/org/groups/{group.id}/archive", json={"reason": "Пауза", "missedLessonsAction": "cancel"})
    assert r.status_code == 200, r.text
    row = w.db.query(GroupAttendance).filter_by(group_id=group.id, lesson_date="2026-10-21", lesson_id="lesson-e").one()
    assert (row.status, row.cancellation_kind, row.marked_by) == ("cancelled", None, w.admin.id)       # the kind it always had: none
    assert w.db.query(LessonTeacherAssignment).filter_by(group_id=group.id, lesson_date="2026-10-21").one().cancelled_at is not None
    assert [x for x in requests_of(w) if x.group_id == group.id and x.status == "cancelled"] == []       # no record of «Замены» (the archive has its own)
    restored = client(w, "admin").post(f"{API}/org/groups/{group.id}/restore", json={})
    assert restored.status_code == 200, restored.text
    assert busy.id


def test_r3_11_a_day_off_that_cannot_be_applied_to_a_returning_group_is_marked_and_warned(w, monkeypatch):
    from src.auth.days_off import DaysOffService

    group = new_group(w.db, w.org, w.ids, "Ea", "Архивная", w.D, [new_slot("lesson-e", 3, "12:00", "13:00", "2026-10-28")])
    w.db.commit()
    assert client(w, "admin").post(f"{API}/org/groups/{group.id}/archive", json={"reason": "Пауза"}).status_code == 200
    made = client(w, "owner").post(DAYS, json={"dateFrom": "2026-10-28", "dateTo": "2026-10-28", "reason": "Хайит"}).json()
    assert made["complete"] is True

    def boom(self, *a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(DaysOffService, "_run", boom)
    r = client(w, "admin").post(f"{API}/org/groups/{group.id}/restore", json={})
    assert r.status_code == 200, r.text
    assert [x["code"] for x in r.json()["warnings"]] == ["DAY_OFF_NOT_APPLIED"] and "Продолжить" in r.json()["warnings"][0]["message"]
    w.db.expire_all()
    assert w.db.query(OrgDayOff).filter_by(id=UUID(made["dayOff"]["id"])).one().incomplete is True
    assert [d["incomplete"] for d in client(w, "owner").get(DAYS).json()] == [True]


def test_r3_11_a_returning_group_without_trouble_has_no_warnings(w):
    group = new_group(w.db, w.org, w.ids, "Ea", "Архивная", w.D, [new_slot("lesson-e", 3, "12:00", "13:00", "2026-10-28")])
    w.db.commit()
    client(w, "admin").post(f"{API}/org/groups/{group.id}/archive", json={"reason": "Пауза"})
    client(w, "owner").post(DAYS, json={"dateFrom": "2026-10-28", "dateTo": "2026-10-28", "reason": "Хайит"})
    r = client(w, "admin").post(f"{API}/org/groups/{group.id}/restore", json={})
    assert r.status_code == 200 and r.json()["warnings"] == []
    assert row_of(w, group, "lesson-e", "2026-10-28").cancellation_kind == "holiday"


def test_r3_12_giving_a_lesson_back_says_which_substitute_does_not_come_back(w):
    put_teacher(w, w.G1, "lesson-l1", "2026-10-26", w.D)
    sheet(w, "admin", w.G1, "lesson-l1", "2026-10-26")
    r = client(w, "admin").post(f"{API}/org/groups/{w.G1.id}/lessons/restore", json={"lessonId": "lesson-l1", "lessonDate": "2026-10-26", "reason": "Ошибка"})
    assert r.status_code == 200, r.text
    assert [x["code"] for x in r.json()["warnings"]] == ["SUBSTITUTE_NOT_RESTORED"] and "назначьте её заново" in r.json()["warnings"][0]["message"]
    sheet(w, "admin", w.G1, "lesson-l1", "2026-11-02")
    assert client(w, "admin").post(f"{API}/org/groups/{w.G1.id}/lessons/restore", json={"lessonId": "lesson-l1", "lessonDate": "2026-11-02", "reason": "Ошибка"}).json()["warnings"] == []


# ====================================================================================================== 13, 14, 15, 16
def test_r3_13_the_list_revalidates_each_request_once_and_the_badge_is_bounded(w, monkeypatch):
    from src.auth import lesson_substitutions as ls

    one(w, "A", "L1", "2026-10-26")
    one(w, "A", "L2", "2026-10-28")
    calls = []
    real = ls.SubstitutionsBase.revalidate

    def counting(self, reqs, **kw):
        calls.append(sorted(str(r.id) for r in reqs))
        return real(self, reqs, **kw)

    monkeypatch.setattr(ls.SubstitutionsBase, "revalidate", counting)
    assert client(w, "admin").get(SUB, params={"status": "pending"}).status_code == 200
    assert len(calls) == 1 and len(calls[0]) == 2 and len(set(calls[0])) == 2
    calls.clear()
    assert client(w, "admin").get(f"{SUB}/summary").status_code == 200
    assert len(calls) == 1
    monkeypatch.setattr(ls, "SUMMARY_REVALIDATE_MAX", 1)
    calls.clear()
    client(w, "admin").get(f"{SUB}/summary")
    assert len(calls[0]) == 1                                                                   # bounded: the oldest request only


def test_r3_14_a_teacher_gets_no_name_or_time_of_another_group_in_student_busy(w):
    w.db.add(StudentGroup(student_id=w.s1.id, group_id=w.G4.id, is_active=True, joined_at=datetime(2026, 9, 1, 5, 0)))
    w.db.commit()
    params = {"lessonId": "lesson-l2", "lessonDate": "2026-10-28", "newDate": "2026-10-29", "startTime": "16:30", "endTime": "17:00"}
    url = f"{API}/org/groups/{w.G1.id}/lesson-move-check"
    manager = [x for x in client(w, "admin").get(url, params=params).json()["warnings"] if x["code"] == "STUDENT_BUSY"][0]
    assert "6-Г" in manager["message"] and manager["students"][0]["groupName"] == "6-Г" and manager["students"][0]["startTime"] == "16:00"
    teacher = [x for x in client(w, "A").get(url, params=params).json()["warnings"] if x["code"] == "STUDENT_BUSY"][0]
    assert teacher["message"] == "У 1 ученика в это время другой урок: Каримов А."
    assert teacher["students"] == [{"id": str(w.s1.id), "name": "Каримов А."}] and "6-Г" not in str(teacher)
    # the answer of the move itself: the same for the teacher
    moved = client(w, "A").post(f"{API}/org/groups/{w.G1.id}/lesson-move", json={"lessonId": "lesson-l2", "lessonDate": "2026-10-28", "newDate": "2026-10-29", "startTime": "16:30", "endTime": "17:00",
                                                                                "reason": "Перенос"})
    assert moved.status_code == 200, moved.text
    assert "6-Г" not in str(moved.json()["result"]["warnings"])


def test_r3_15_no_role_is_defaulted_to_admin_anywhere():
    root = Path(__file__).resolve().parents[1] / "src" / "auth"
    offenders = [f"{p.name}:{i}" for p in sorted(root.glob("*.py")) for i, line in enumerate(p.read_text(errors="ignore").splitlines(), 1) if 'or "admin"' in line]
    assert offenders == []


def test_r3_15_update_group_takes_the_role_from_the_membership(w):
    from src.auth.schemas import GroupUpdateRequest
    from src.models.auth import GroupAudit

    w.svc.update_group(str(w.org.id), str(w.G1.id), GroupUpdateRequest(name="9-А новая"), actor_id=str(w.owner.id))              # no actor_role given
    w.db.expire_all()
    audit = w.db.query(GroupAudit).filter_by(group_id=w.G1.id, action="updated").order_by(GroupAudit.created_at.desc()).first()
    assert audit.actor_role == "owner"


# ====================================================================================================== F10: the main teacher sees the lesson somebody else substitutes
def test_f10_the_main_teacher_keeps_the_substituted_lesson_in_his_schedule_but_cannot_start_it(w, clock):
    put_teacher(w, w.G1, "lesson-l1", "2026-10-26", w.D)
    occ = lambda who: [o for o in client(w, who).get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-10-26", "date_to": "2026-10-26"}).json() if o["id"] == "lesson-l1"]
    (mine,) = occ("A")
    assert (mine["isSubstitution"], mine["teacherId"], mine["defaultTeacherId"], mine["replacedBy"]["id"], mine["replacedBy"]["name"]) == (True, str(w.D.id), str(w.A.id), str(w.D.id), mine["teacherName"])
    (theirs,) = occ("D")                                                                          # the substitute sees it as before
    assert theirs["isSubstitution"] is True and theirs["teacherId"] == str(w.D.id) and theirs.get("replacedBy") is None
    assert [o.get("replacedBy") for o in occ("admin")] == [None]                                  # a manager's schedule is not filtered: no mark
    assert [o["id"] for o in client(w, "C").get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-10-26", "date_to": "2026-10-26"}).json()] == []
    # his dashboard has it too (today)
    clock.set("2026-10-26 09:00")
    dash = client(w, "A").get(f"{API}/org/teacher/dashboard/summary", params={"business_date": "2026-10-26"}).json()
    assert [o["replacedBy"]["id"] for o in dash["todayLessons"] if o["id"] == "lesson-l1"] == [str(w.D.id)]
    # but he cannot start, mark, reconcile, ask to be replaced or move it: it is the substitute's lesson
    clock.set("2026-10-26 10:05")
    base = f"{API}/org/groups/{w.G1.id}/attendance"
    assert client(w, "A").post(base + "/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"}).json()["detail"]["code"] == "NOT_LESSON_TEACHER"
    assert client(w, "A").post(base, json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1", "status": "in_progress", "records": {}}).status_code in (400, 403)
    assert client(w, "A").post(base + "/reconcile", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"}).status_code in (400, 403)
    assert req(w, "A", [(w.G1, "lesson-l1", "2026-10-26")]).status_code in (409, 403)
    my = client(w, "A").get(f"{SUB}/my-lessons", params={"dateFrom": "2026-10-26", "dateTo": "2026-10-26"}).json()["lessons"]
    assert [x for x in my if x["lessonId"] == "lesson-l1"] == []                                  # not in «Не смогу провести» either
    assert client(w, "D").post(base + "/start", json={"lessonDate": "2026-10-26", "lessonId": "lesson-l1"}).status_code == 200      # the substitute starts it


# ====================================================================================================== F6: the revisions of a series from the server
def test_f6_the_series_tells_its_planned_revisions_and_its_stop(w, clock):
    url = f"{REV}/revisions"
    assert client(w, "admin").get(url).json() == {"lessonId": "lesson-l1", "after": "2026-10-21", "revisions": [], "endsFrom": None}
    rev(w, {"effectiveFrom": "2026-11-09", "room": "Каб. 5"})
    rev(w, {"effectiveFrom": "2026-11-23", "startTime": "12:00", "endTime": "13:30", "room": "Каб. 7"})
    out = client(w, "admin").get(url).json()
    assert [(r["effectiveFrom"], r["fields"]) for r in out["revisions"]] == [("2026-11-09", ["room"]), ("2026-11-23", ["startTime", "endTime", "room"])]
    assert client(w, "admin").get(url, params={"after": "2026-11-09"}).json()["revisions"][0]["effectiveFrom"] == "2026-11-23"
    client(w, "admin").post(f"{REV}/end", json={"effectiveFrom": "2026-12-14", "reason": "Курс закончен"})
    assert client(w, "admin").get(url).json()["endsFrom"] == "2026-12-14"
    assert client(w, "A").get(url).status_code == 200                                              # the teacher of the group reads it
    assert client(w, "B").get(url).status_code == 403                                              # a stranger does not
    assert make_client(w, user=w.foreign_admin, org="other", org_role="admin", role="admin").get(url).status_code in (400, 403, 404)


# ====================================================================================================== (C) the dashboard is annotated like the schedule, (D) bulk warnings
def test_c_the_teacher_dashboard_carries_the_same_marks_as_the_schedule(w, clock):
    item = req(w, "A", [(w.G1, "lesson-l1", "2026-10-26")]).json()["created"][0]
    lid = client(w, "A").post(f"{API}/org/schedule/occurrences", json={"groupId": str(w.G1.id), "lessonDate": "2026-10-26", "startTime": "13:00", "endTime": "14:00"}).json()["id"]
    dash = client(w, "A").get(f"{API}/org/teacher/dashboard/summary", params={"business_date": "2026-10-26"}).json()
    today = {o["id"]: o for o in dash["todayLessons"]}
    assert today["lesson-l1"]["substitutionRequest"]["id"] == item["id"] and today["lesson-l1"]["substitutionRequest"]["status"] == "pending"      # the mark of «Не смогу провести» (F2)
    assert today[lid]["createdBy"]["id"] == str(w.A.id) and today[lid]["createdBy"]["role"] == "teacher"                                    # the author of a one-off (S26 / S27)
    sched = {o["id"]: o for o in client(w, "A").get(f"{API}/org/schedule/occurrences", params={"date_from": "2026-10-26", "date_to": "2026-10-26"}).json()}
    assert {k: today[k].get("substitutionRequest") for k in ("lesson-l1", lid)} == {k: sched[k].get("substitutionRequest") for k in ("lesson-l1", lid)}
    assert [o["createdBy"] for o in dash["scheduleWindow"] if o["id"] == lid] == [today[lid]["createdBy"]]
    # a lesson somebody else substitutes: the same mark as in the schedule (F10)
    put_teacher(w, w.G1, "lesson-l2", "2026-10-28", w.D)
    window = client(w, "A").get(f"{API}/org/teacher/dashboard/summary", params={"business_date": "2026-10-28"}).json()["todayLessons"]
    assert [o["replacedBy"]["id"] for o in window if o["id"] == "lesson-l2"] == [str(w.D.id)]


def test_d_a_bulk_cancel_returns_the_warnings_of_every_request(w, sync):
    sync.mode = "live"
    client(w, "owner").post(DAYS, json={"dateFrom": "2026-10-28", "dateTo": "2026-10-28", "reason": "Хайит"})
    sync.mode = "ok"
    a = one(w, "A", "L2", "2026-10-28")                                                         # the date of a live day off -> a holiday
    put_teacher(w, w.G1, "lesson-l1", "2026-11-02", w.D)
    d = req(w, "D", [(w.G1, "lesson-l1", "2026-11-02")]).json()["created"][0]                   # the substitute asks: his assignment is released by the cancellation
    free = one(w, "A", "L1", "2026-11-09")                                                      # nothing special
    r = client(w, "admin").post(f"{SUB}/decide-bulk", json={"ids": [a["id"], d["id"], free["id"]], "decision": "cancel", "reason": "Курсы"})
    assert r.status_code == 200, r.text
    out = r.json()
    by = {x["id"]: x for x in out["results"]}
    assert [x["code"] for x in by[a["id"]]["warnings"]] == ["CANCELLED_AS_HOLIDAY"] and by[a["id"]]["cancellationKind"] == "holiday"
    assert [x["code"] for x in by[d["id"]]["warnings"]] == ["SUBSTITUTE_RELEASED"] and by[d["id"]]["cancellationKind"] == "centre"
    assert by[free["id"]]["warnings"] == [] and by[free["id"]]["kind"] == "cancel"
    assert out["updated"] == 3 and out["warningsTotal"] == 2 and [x["id"] for x in out["results"]] == [i["id"] for i in out["items"]]
    # a bulk with nothing to say
    other = one(w, "A", "L1", "2026-11-16")
    quiet = client(w, "admin").post(f"{SUB}/decide-bulk", json={"ids": [other["id"]], "decision": "reject", "reason": "Нет замены"}).json()
    assert quiet["warningsTotal"] == 0 and quiet["results"] == [{"id": other["id"], "kind": "reject", "cancellationKind": None, "warnings": []}]
